import asyncio
import logging
import re
from dataclasses import dataclass

import httpx
from rapidfuzz import fuzz

from sqlalchemy import func, select, update

from app.config import settings
from app.db.database import async_session_factory
from app.models.database import Media, EnrichmentQueue, XtreamAccount
from app.services import scrape_cache_service as scrape_cache
from app.services import omdb_scrape_cache_service as omdb_scrape_cache
from app.services.omdb_service import OMDbData, omdb_service
from app.services.tmdb_service import TMDBEnrichmentData, TMDBSearchOutcome, tmdb_service
from app.utils.string_normalizer import normalize_for_sorting
from app.utils.time import now_ms
from app.utils.db_retry import commit_with_retry
from app.utils.rating_blend import blend_display_rating_case, recompute_display_rating_stmt
from app.utils.unification import calculate_unification_id

logger = logging.getLogger("plexhub.enrichment")

BATCH_SIZE = 200  # Commit every N items
CONCURRENCY = 8   # Parallel TMDB requests (free tier ~4 req/s, keep headroom)
MAX_ATTEMPTS = 3  # Max enrichment attempts before permanently skipping

# --- Anti-recurrence guard (Wave 3, S5 — id-consistency validator design doc §5) --
# `_IMDB_ID_RE`: cheap shape tripwire for `TMDBEnrichmentData.imdb_id`.
_IMDB_ID_RE = re.compile(r"^tt\d+$")
# Conservative OMDb tie-break thresholds (tech-lead deviation D1 — this is the
# *primary* defense, not the doc's "Optionnel"). Both conditions must hold to
# downgrade a match: a year gap of more than 1, AND a low title similarity —
# title alone is never conclusive because OMDb frequently returns the
# original/English title for localized content.
_OMDB_YEAR_TOLERANCE = 1
_OMDB_TITLE_CONTRADICTION_SIM = 0.55

# --- OMDb-by-title fallback thresholds (D-IDENTITY, dual-provider design doc
# §Thresholds). Deliberately ASYMMETRIC vs the tie-break above: keeping an
# existing match only needs the absence of a strong contradiction, but
# asserting a NEW identity from a bare title (which OMDb often returns in
# English) needs a much higher bar — year-exact + high similarity + type
# match.
_OMDB_TITLE_DISCARD_SIM = 0.60   # below this, treat the ?t= hit as OMDb-nomatch
_OMDB_TITLE_STRONG_SIM = 0.90    # at/above this (+ year-exact + type) => identity


@dataclass
class FetchResult:
    item: EnrichmentQueue
    data: TMDBEnrichmentData | None
    confidence: float | None
    result: str            # matched | nomatch | ambiguous | skipped
    # NOTE (CR-F03): this is a count of *logical* TMDB calls attempted for this
    # item (searches + details fetch) — informational only, kept for the
    # per-batch/per-item bookkeeping below. It is NOT used to enforce
    # `ENRICHMENT_DAILY_LIMIT` anymore: `run()` budgets against
    # `tmdb_service.get_request_count()`, the count of *real* outbound HTTP
    # attempts (including every retry inside `tmdb_service._request`), since a
    # single logical call can cost up to 4 real HTTP attempts under
    # 429/5xx retries.
    api_used: int
    cache_key: str | None  # set when the outcome should be written to scrape cache
    from_cache: bool = False
    # --- OMDb (dual-provider enrichment, design C3) ------------------------
    # The SINGLE OMDb result fetched for this item in the concurrent `_resolve`
    # phase (by imdb_id when one is in hand, else by title on a fresh TMDB
    # nomatch). Serves BOTH the low-confidence contradiction tie-break AND the
    # imdb_rating/imdb_votes enrichment — never a second call (requirement 6).
    omdb: OMDbData | None = None
    # `(imdb_id, "found"|"not_found")` to persist to `omdb_scrape_cache` in the
    # apply phase (deduped there), or None on a cache-hit / skipped fetch
    # (nothing new to write).
    omdb_put: tuple[str, str] | None = None
    # True only when a FRESH OMDb-by-title produced a STRONG match (year-exact,
    # sim >= 0.90, type match) eligible for an identity write.
    omdb_identity: bool = False


async def _search_with_fallback(
    media_type: str, title: str, year: int | None, summary: str | None,
) -> tuple[TMDBSearchOutcome, int]:
    """Try progressively looser searches; return (outcome, network_searches).

    Order (plan §3.3): default → without year → en-US → /search/multi. Stops at
    the first auto-match; otherwise returns the highest-scoring attempt so the
    best score is still recorded."""
    search = tmdb_service.search_movie if media_type == "movie" else tmdb_service.search_tv
    multi_kind = "movie" if media_type == "movie" else "tv"
    attempts: list[TMDBSearchOutcome] = []

    o = await search(title, year, summary=summary)
    attempts.append(o)
    if o.result != "matched" and year is not None:
        o = await search(title, None, summary=summary)
        attempts.append(o)
    if attempts[-1].result != "matched":
        o = await search(title, year, summary=summary, language="en-US")
        attempts.append(o)
    if attempts[-1].result != "matched":
        o = await tmdb_service.search_multi(title, multi_kind)
        attempts.append(o)

    for o in attempts:
        if o.result == "matched":
            return o, len(attempts)
    # No match — surface the best-scoring attempt (for recording / metrics).
    best = max(attempts, key=lambda a: a.confidence)
    return best, len(attempts)


async def _resolve_tmdb(item, media_type: str, get_details) -> FetchResult:
    """Resolve one queue item to TMDB data (no OMDb), using the persistent
    scrape cache first and the fallback search chain on a miss. OMDb enrichment
    is attached separately by `_attach_omdb` so the network fetch stays inside
    the concurrent `_resolve` semaphore."""
    existing_tmdb = item.existing_tmdb_id
    existing_imdb = item.existing_imdb_id

    # Scenario 2: TMDB id known, IMDB missing — fetch details only.
    if existing_tmdb and not existing_imdb:
        tmdb_id = int(existing_tmdb) if str(existing_tmdb).isdigit() else None
        if tmdb_id:
            details = await get_details(tmdb_id)
            return FetchResult(item, details, 1.0, "matched", 1, None)
        return FetchResult(item, None, None, "skipped", 0, None)

    # Scenario 3: IMDB known, TMDB missing — keep IMDB, skip the TMDB search.
    # OMDb ratings are still fetched by that imdb_id in `_attach_omdb`.
    if existing_imdb and not existing_tmdb:
        return FetchResult(item, None, None, "skipped", 0, None)

    if not tmdb_service.is_configured:
        return FetchResult(item, None, None, "skipped", 0, None)

    # Scenario 4: both absent — persistent cache first, then search.
    cache_key = scrape_cache.make_key(media_type, item.title, item.year)
    async with async_session_factory() as cdb:
        hit = await scrape_cache.get(cdb, cache_key, now_ms())
    if hit is not None:
        return FetchResult(item, hit.data, hit.confidence, hit.result, 0, None, from_cache=True)

    outcome, n_search = await _search_with_fallback(
        media_type, item.title, item.year, item.existing_summary,
    )
    if outcome.result == "matched" and outcome.match:
        details = await get_details(outcome.match.tmdb_id)
        return FetchResult(
            item, details, outcome.match.confidence, "matched",
            n_search + 1, cache_key,
        )
    return FetchResult(item, None, outcome.confidence, outcome.result, n_search, cache_key)


async def _fetch_omdb_by_id(imdb_id: str) -> tuple[OMDbData | None, tuple[str, str] | None]:
    """Cache-first, budget-gated OMDb lookup by imdb_id.

    Returns `(omdb_data, omdb_put)` where `omdb_put` is `(imdb_id, result)` to
    persist on a FRESH HTTP call, or None on a cache-hit / budget-skip
    (nothing new to write). Mirrors the tie-break read/gate order that
    `_omdb_contradicts` used before this refacto: budget guard first (a spent
    daily budget disables OMDb entirely for the rest of the run), then the
    persistent cache, then the network. Fail-open: unconfigured / over budget
    -> `(None, None)`; `get_by_imdb_id` itself is graceful-None on error."""
    if not imdb_id or not omdb_service.is_configured:
        return None, None
    if omdb_service.get_request_count() >= settings.OMDB_DAILY_LIMIT:
        return None, None
    ts = now_ms()
    async with async_session_factory() as cdb:
        cached = await omdb_scrape_cache.get(cdb, imdb_id, ts)
    if cached is not None:
        return cached, None
    omdb_data = await omdb_service.get_by_imdb_id(imdb_id)
    return omdb_data, (imdb_id, "found" if omdb_data is not None else "not_found")


def _omdb_type_matches(omdb_type: str | None, media_type: str) -> bool:
    """OMDb `Type` ("movie"/"series") vs our media_type ("movie"/"show")."""
    want = {"movie": "movie", "show": "series"}.get(media_type)
    return bool(want) and (omdb_type or "").strip().lower() == want


def _omdb_title_sim(query: str, candidate: str) -> float:
    """Normalized title similarity in 0..1 (same discipline as `_best_match`)."""
    query_norm = normalize_for_sorting(query)
    cand_norm = normalize_for_sorting(candidate)
    if not query_norm or not cand_norm:
        return 0.0
    return max(
        fuzz.ratio(query_norm, cand_norm),
        fuzz.token_set_ratio(query_norm, cand_norm),
    ) / 100.0


def _classify_omdb_title(item, media_type: str, omdb_data: OMDbData | None) -> str:
    """Classify an OMDb-by-title hit per D-IDENTITY thresholds.

    Returns "strong" (identity write allowed), "weak" (metadata/ratings only,
    no identity) or "discard" (treat as OMDb-nomatch, write nothing). STRONG
    requires the year to be EXACT (0 tolerance — OMDb often returns the English
    title, so a bare title is never conclusive), sim >= 0.90 AND a type match."""
    if omdb_data is None:
        return "discard"
    sim = _omdb_title_sim(item.title, omdb_data.title)
    if sim < _OMDB_TITLE_DISCARD_SIM:
        return "discard"
    omdb_year = _parse_omdb_year(omdb_data.year)
    year_exact = item.year is not None and omdb_year is not None and omdb_year == item.year
    if year_exact and sim >= _OMDB_TITLE_STRONG_SIM and _omdb_type_matches(omdb_data.type, media_type):
        return "strong"
    return "weak"


async def _attach_omdb(fr: FetchResult, item, media_type: str) -> None:
    """Single OMDb fetch per item, inside the concurrent `_resolve` semaphore.

    - imdb_id in hand (TMDB match with external imdb, or scenario 3 existing
      imdb) -> `get_by_imdb_id` (cache-first, budget-gated). This one fetch
      feeds BOTH the contradiction tie-break and the rating enrichment.
    - FRESH TMDB nomatch (`from_cache is False`) -> `search_by_title`, then
      classify strong/weak/discard. A cached nomatch is skipped (the title-miss
      is already negatively cached at the TMDB layer — design §negative-cache)."""
    # imdb_id in hand: from the TMDB details, or scenario 3 (existing imdb, no
    # existing tmdb -> TMDB search skipped, but ratings still wanted).
    imdb_in_hand = None
    if fr.data is not None and fr.data.imdb_id:
        imdb_in_hand = fr.data.imdb_id
    elif item.existing_imdb_id and not item.existing_tmdb_id:
        imdb_in_hand = item.existing_imdb_id

    if imdb_in_hand:
        fr.omdb, fr.omdb_put = await _fetch_omdb_by_id(imdb_in_hand)
        return

    # OMDb-by-title fallback — only on a FRESH TMDB nomatch.
    if fr.result != "nomatch" or fr.from_cache:
        return
    if not omdb_service.is_configured:
        return
    if omdb_service.get_request_count() >= settings.OMDB_DAILY_LIMIT:
        return

    omdb_data = await omdb_service.search_by_title(item.title, item.year, media_type)
    klass = _classify_omdb_title(item, media_type, omdb_data)
    if klass == "discard":
        return  # write nothing; no cache put (a title-miss has no id to key on)

    fr.omdb = omdb_data
    fr.omdb_identity = klass == "strong"
    # A ?t= HIT does have an imdb_id -> cache it positively under that id so a
    # later by-id lookup is free (the negative case relies on the TMDB nomatch
    # cache instead — design §negative-cache).
    if omdb_data is not None and omdb_data.imdb_id:
        fr.omdb_put = (omdb_data.imdb_id, "found")


async def _resolve(item, media_type: str, semaphore) -> FetchResult:
    """Resolve one queue item: TMDB (cache/search) then a single OMDb fetch,
    all under the shared concurrency semaphore."""
    get_details = (
        tmdb_service.get_movie_details if media_type == "movie"
        else tmdb_service.get_tv_details
    )
    async with semaphore:
        try:
            fr = await _resolve_tmdb(item, media_type, get_details)
        except httpx.HTTPStatusError as e:
            # A 404 means the tmdb_id supplied by the provider no longer exists
            # on TMDB — an expected, recoverable miss, not a code fault. Log it
            # quietly without a traceback; let real HTTP errors surface loudly.
            if e.response.status_code == 404:
                logger.info(f"Enrichment skipped for {item.rating_key}: TMDB 404 (stale id)")
                return FetchResult(item, None, None, "not_found", 0, None)
            logger.warning(f"Enrichment fetch failed for {item.rating_key}: {e}", exc_info=True)
            return FetchResult(item, None, None, "skipped", 0, None)
        except Exception as e:
            logger.warning(f"Enrichment fetch failed for {item.rating_key}: {e}", exc_info=True)
            return FetchResult(item, None, None, "skipped", 0, None)

        # Single OMDb fetch — fail-open, never fails the TMDB result.
        try:
            await _attach_omdb(fr, item, media_type)
        except Exception as e:
            logger.warning(
                "Enrichment: OMDb attach failed for %s (%s) — keeping TMDB result",
                item.rating_key, type(e).__name__,
            )
        return fr


async def _fetch_movie_data(item, semaphore):
    return await _resolve(item, "movie", semaphore)


async def _fetch_series_data(item, semaphore):
    return await _resolve(item, "show", semaphore)


def _shape_invalid(data: TMDBEnrichmentData) -> str | None:
    """Cheap intra-record tripwire on a single `get_details()` result.

    `tmdb_id`/`imdb_id` both come from the SAME TMDB response
    (`external_ids`, see `tmdb_service._parse_details`) so they are
    consistent by construction — this only bounds the root-cause search if
    object-level corruption is ever observed in prod. Returns a short reason
    string on failure, else None."""
    if data.tmdb_id is None or data.tmdb_id <= 0:
        return f"non-positive tmdb_id ({data.tmdb_id!r})"
    if data.imdb_id is not None and not _IMDB_ID_RE.match(data.imdb_id):
        return f"malformed imdb_id shape ({data.imdb_id!r})"
    return None


def _parse_omdb_year(raw: str | None) -> int | None:
    """OMDb `Year`: "1984" (movie), "2015–2019" / "2015-" (series, en-dash or
    open-ended). Take the leading 4-digit year; unparseable -> None."""
    if not raw:
        return None
    m = re.match(r"^(\d{4})", raw)
    return int(m.group(1)) if m else None


def _omdb_contradicts(
    item: EnrichmentQueue, omdb_data: OMDbData | None,
) -> bool:
    """Cross-check a low-confidence TMDB match against a PRE-FETCHED OMDb result.

    The OMDb fetch already happened once in `_resolve` (`fr.omdb`) — this
    function performs NO network call (requirement 6: no double-call). Same
    downgrade logic as before: fail-open on every non-conclusive path (no OMDb
    data, missing year on either side, unparseable OMDb year, or any exception)
    -> False (keep the match). Downgrades ONLY when BOTH a year gap > 1 AND a
    low title similarity hold — OMDb frequently returns the original/English
    title for localized content, so title alone is never conclusive."""
    if omdb_data is None or not item.year:
        return False
    try:
        omdb_year = _parse_omdb_year(omdb_data.year)
        if omdb_year is None:
            return False
        if abs(omdb_year - item.year) <= _OMDB_YEAR_TOLERANCE:
            return False

        sim = _omdb_title_sim(item.title, omdb_data.title)
        if sim == 0.0 or sim >= _OMDB_TITLE_CONTRADICTION_SIM:
            return False

        logger.warning(
            "Enrichment guard: OMDb contradicts match for rating_key=%s "
            "(item year=%s, omdb year=%s, title similarity=%.2f) — downgrading to ambiguous",
            item.rating_key, item.year, omdb_year, sim,
        )
        return True
    except Exception as e:
        logger.warning(
            "Enrichment guard: OMDb tie-break failed for rating_key=%s (%s) — keeping match",
            item.rating_key, type(e).__name__,
        )
        return False


def _apply_omdb_metadata(update_values: dict, omdb: OMDbData) -> None:
    """Fill-missing metadata from an OMDb-by-title result (weak/strong title
    fallback only — the by-id path already has TMDB metadata and takes ratings
    only). Ratings + display_rating are handled uniformly by the caller. Uses
    `setdefault` so it never overrides a value the identity branch already set."""
    if omdb.plot:
        update_values.setdefault("summary", func.coalesce(Media.summary, omdb.plot))
    if omdb.genre:
        update_values.setdefault("genres", func.coalesce(Media.genres, omdb.genre))
    if omdb.actors:
        update_values.setdefault("cast", func.coalesce(Media.cast, omdb.actors))
    omdb_year = _parse_omdb_year(omdb.year)
    if omdb_year is not None:
        update_values.setdefault("year", func.coalesce(Media.year, omdb_year))


async def _apply_enrichment_results(db, results: list[FetchResult]):
    """Apply enrichment results to DB — IDs + rich metadata + ratings (TMDB +
    OMDb) + scrape caches + metric."""
    from app.utils.metrics import tmdb_match_total

    batch_used = 0
    ts = now_ms()
    # Defer ORM flushes of the dirty EnrichmentQueue items to the single commit
    # in run(). Without this, every in-loop query autoflushes them, and if that
    # write hits the WAL writer lock (held for hours by stream validation) it
    # raises mid-loop and crashes the whole batch instead of being retried at
    # commit time. Paired with the 60s busy_timeout on the engine connection.
    with db.no_autoflush:
        # Dedupe cache writes within the batch: several items can share one
        # cache_key (same normalized title+year across versions/accounts). Under
        # `no_autoflush` a second `scrape_cache.put` for the same key can't see
        # the first's still-pending INSERT, so it would add a second row with the
        # same primary key → `UNIQUE constraint failed: tmdb_scrape_cache.cache_key`
        # crashes the whole batch at commit. One put per key is enough — the id is
        # still applied to every media row below regardless.
        put_keys: set[str] = set()
        # Same rationale for the always-fetch OMDb path: several items can share
        # one imdb_id (same film synced from two Xtream accounts). One
        # `omdb_scrape_cache.put` per imdb_id per batch — a second would add a
        # second row with the same primary key → `UNIQUE constraint failed:
        # omdb_scrape_cache.imdb_id` crashes the whole batch. This is the single
        # authoritative OMDb-cache dedup (the tie-break's old in-loop
        # get/call/put + `omdb_batch_cache` are gone — the fetch now happens
        # once per item in `_resolve`).
        omdb_put_keys: set[str] = set()
        for fr in results:
            item = fr.item
            enrichment_data = fr.data
            downgraded = False

            # --- Anti-recurrence guard (Wave 3, S5 + dual-provider tie-break) ---
            # Runs BEFORE the outcome metric / scrape-cache write below so a
            # downgrade to "ambiguous" is reflected everywhere fr.result feeds
            # into (the plexhub_tmdb_match_total metric, the persisted scrape
            # cache resolution, and the id-write gate further down). The OMDb
            # tie-break uses the PRE-FETCHED `fr.omdb` — no network call here.
            if enrichment_data:
                shape_issue = _shape_invalid(enrichment_data)
                if shape_issue:
                    logger.warning(
                        "Enrichment guard: %s for rating_key=%s — downgrading to ambiguous",
                        shape_issue, item.rating_key,
                    )
                    fr.result = "ambiguous"
                    enrichment_data = None
                    downgraded = True
                elif (
                    fr.confidence is not None and fr.confidence < 1.0
                    and enrichment_data.imdb_id
                    and _omdb_contradicts(item, fr.omdb)
                ):
                    fr.result = "ambiguous"
                    enrichment_data = None
                    downgraded = True
            # ---------------------------------------------------------------------

            # Outcome metric (only the genuine matching attempts — skip scenario 2/3).
            if fr.result in ("matched", "nomatch", "ambiguous"):
                metric_type = "movie" if item.media_type == "movie" else "tv"
                tmdb_match_total.labels(media_type=metric_type, result=fr.result).inc()

            # Persist the TMDB resolution so the same title is never re-queried.
            if fr.cache_key and not fr.from_cache and fr.cache_key not in put_keys:
                put_keys.add(fr.cache_key)
                await scrape_cache.put(
                    db, fr.cache_key, item.media_type, fr.result,
                    fr.confidence, enrichment_data, ts,
                )

            # Persist the OMDb resolution (found/not_found), deduped by imdb_id.
            # Independent of the match verdict — the cache reflects the OMDb
            # fetch outcome for that id, not whether we trusted the TMDB match.
            if fr.omdb_put is not None:
                omdb_imdb_id, omdb_result = fr.omdb_put
                if omdb_imdb_id not in omdb_put_keys:
                    omdb_put_keys.add(omdb_imdb_id)
                    await omdb_scrape_cache.put(db, omdb_imdb_id, omdb_result, fr.omdb, ts)

            # OMDb ratings are trusted only when the match was NOT downgraded.
            have_omdb = fr.omdb is not None and not downgraded
            is_scenario3 = bool(item.existing_imdb_id and not item.existing_tmdb_id)

            update_values: dict = {}

            if enrichment_data:
                # --- TMDB match: identity + rich metadata ---
                tmdb_id = enrichment_data.tmdb_id
                imdb_id = enrichment_data.imdb_id
                new_unif = f"imdb://{imdb_id}" if imdb_id else f"tmdb://{tmdb_id}"

                update_values.update({
                    "tmdb_id": str(tmdb_id),
                    "imdb_id": imdb_id,
                    "unification_id": new_unif,
                    "history_group_key": new_unif,
                    "tmdb_match_confidence": fr.confidence,
                })
                if enrichment_data.overview:
                    update_values["summary"] = enrichment_data.overview
                if enrichment_data.genres:
                    update_values["genres"] = enrichment_data.genres
                if enrichment_data.poster_url:
                    update_values["resolved_thumb_url"] = enrichment_data.poster_url
                if enrichment_data.backdrop_url:
                    update_values["resolved_art_url"] = enrichment_data.backdrop_url
                if enrichment_data.vote_average:
                    # scraped_rating stays = raw TMDB vote_average (durable
                    # record). display_rating is NO LONGER this — it is the
                    # blend computed below.
                    update_values["scraped_rating"] = enrichment_data.vote_average
                if enrichment_data.year:
                    update_values["year"] = enrichment_data.year
                if enrichment_data.cast:
                    update_values["cast"] = enrichment_data.cast

                # Rich metadata mirroring the NFO columns. Fill-missing-only via
                # COALESCE so we never clobber richer data already imported from a
                # tvshow.nfo / movie.nfo, nor the adult tagging's content_rating
                # ("XXX"). `imdb_rating`/`imdb_votes` come from OMDb below.
                # (col, value) pairs; skipped when TMDB gave nothing.
                rich = (
                    ("content_rating", enrichment_data.content_rating),
                    ("original_title", enrichment_data.original_title),
                    ("tagline", enrichment_data.tagline),
                    ("premiered", enrichment_data.premiered),
                    ("status", enrichment_data.status),
                    ("studio", enrichment_data.studio),
                    ("country", enrichment_data.country),
                    ("tvdb_id", enrichment_data.tvdb_id),
                    ("wikidata_id", enrichment_data.wikidata_id),
                    ("tmdb_rating", enrichment_data.tmdb_rating),
                    ("tmdb_votes", enrichment_data.tmdb_votes),
                    ("cast_json", enrichment_data.cast_json),
                )
                for col, value in rich:
                    if value is not None:
                        update_values[col] = func.coalesce(getattr(Media, col), value)
                item.status = "done"

            elif fr.omdb_identity and have_omdb and fr.omdb.imdb_id:
                # --- STRONG OMDb-by-title: identity from OMDb + metadata (both
                # fill-missing). ---
                new_unif = calculate_unification_id(
                    item.title, item.year, imdb_id=fr.omdb.imdb_id,
                )
                update_values.update({
                    "imdb_id": fr.omdb.imdb_id,
                    "unification_id": new_unif,
                    "history_group_key": new_unif,
                })
                if fr.confidence is not None:
                    update_values["tmdb_match_confidence"] = fr.confidence
                _apply_omdb_metadata(update_values, fr.omdb)
                item.status = "done"

            else:
                # --- No identity write this pass (nomatch, weak title, or
                # scenario-3 by-id ratings). Record best score if we have one. ---
                if fr.confidence is not None:
                    update_values["tmdb_match_confidence"] = fr.confidence
                # Weak OMDb-title -> metadata fill-missing (NOT the scenario-3
                # by-id path, which already had provider metadata: ratings only).
                if have_omdb and not is_scenario3:
                    _apply_omdb_metadata(update_values, fr.omdb)
                # Scenario 3 with OMDb ratings applied = fully enriched (identity
                # already present via the existing imdb_id); otherwise skipped so
                # it stays retryable.
                if is_scenario3 and have_omdb:
                    item.status = "done"
                else:
                    item.status = "skipped"

            # --- OMDb ratings (COALESCE fill-missing) + display_rating blend ---
            # imdb_rating/imdb_votes never clobber a richer NFO value. Applied
            # to every non-downgraded path with OMDb ratings (by-id AND title).
            new_imdb = fr.omdb.imdb_rating if have_omdb else None
            new_votes = fr.omdb.imdb_votes if have_omdb else None
            new_tmdb = enrichment_data.tmdb_rating if enrichment_data is not None else None

            if new_imdb is not None:
                update_values["imdb_rating"] = func.coalesce(Media.imdb_rating, new_imdb)
            if new_votes is not None:
                update_values["imdb_votes"] = func.coalesce(Media.imdb_votes, new_votes)

            # display_rating = blend(imdb, tmdb) computed from the POST-WRITE
            # persisted columns (COALESCE of the pre-update value with the value
            # written this pass), so it stays reproducible in SQL. The CASE
            # `else_` keeps the current value when BOTH sides are absent — safe
            # to emit on any enrichment write (TMDB match or any OMDb result).
            if enrichment_data is not None or have_omdb:
                imdb_operand = (
                    func.coalesce(Media.imdb_rating, new_imdb)
                    if new_imdb is not None else Media.imdb_rating
                )
                tmdb_operand = (
                    func.coalesce(Media.tmdb_rating, new_tmdb)
                    if new_tmdb is not None else Media.tmdb_rating
                )
                update_values["display_rating"] = blend_display_rating_case(
                    imdb_operand, tmdb_operand, Media.display_rating,
                )

            if update_values:
                await db.execute(
                    update(Media)
                    .where(
                        Media.rating_key == item.rating_key,
                        Media.server_id == item.server_id,
                    )
                    .values(**update_values)
                )

            item.attempts += 1
            item.processed_at = ts
            batch_used += fr.api_used
    return batch_used


async def run():
    """Run enrichment batch with parallel TMDB + OMDb fetching."""
    daily_limit = settings.ENRICHMENT_DAILY_LIMIT
    semaphore = asyncio.Semaphore(CONCURRENCY)

    # Budget against REAL TMDB HTTP calls (search + details + every retry
    # inside tmdb_service._request), not logical queue items — a single item
    # can cost up to 4 real HTTP attempts (1 initial + up to 3 retries) when
    # TMDB rate-limits (429) or 5xx's, so counting logical items/searches
    # under-counted real spend by 2-4x (CR-F03). `tmdb_service` exposes a
    # live counter incremented on every real outbound attempt; reset it here
    # so `used()` reflects only this run's spend (in-process only — see
    # `reset_request_count` docstring for the persisted-daily-quota residual).
    tmdb_service.reset_request_count()
    # Same for the OMDb budget (dual-provider enrichment): reset the per-run
    # counter so `OMDB_DAILY_LIMIT` gates only this run's OMDb spend.
    omdb_service.reset_request_count()

    def used() -> int:
        return tmdb_service.get_request_count()

    logger.info(f"Starting enrichment batch (daily limit: {daily_limit}, concurrency: {CONCURRENCY})")

    async with async_session_factory() as db:
        # Phase 1: VOD movies (pending + retryable skipped items)
        from sqlalchemy import or_
        result = await db.execute(
            select(EnrichmentQueue)
            .where(
                or_(
                    EnrichmentQueue.status == "pending",
                    # Retry previously skipped items if under max attempts
                    (EnrichmentQueue.status == "skipped") & (EnrichmentQueue.attempts < MAX_ATTEMPTS),
                ),
                EnrichmentQueue.media_type == "movie",
            )
            .order_by(EnrichmentQueue.created_at)
            .limit(daily_limit)
        )
        pending_vod = list(result.scalars().all())
        logger.info(f"Enrichment Phase 1: {len(pending_vod)} pending movies")

        # Process in batches with parallel HTTP, sequential DB writes
        for batch_start in range(0, len(pending_vod), BATCH_SIZE):
            if used() >= daily_limit:
                logger.info(f"Enrichment: real TMDB call budget ({daily_limit}) reached, stopping VOD phase")
                break
            batch_end = min(batch_start + BATCH_SIZE, len(pending_vod))
            batch = pending_vod[batch_start:batch_end]

            tasks = [
                _fetch_movie_data(item, semaphore)
                for item in batch
            ]
            results = await asyncio.gather(*tasks)

            before = used()
            await _apply_enrichment_results(db, results)
            await commit_with_retry(db)

            logger.info(f"Enrichment VOD {batch_end}/{len(pending_vod)} "
                       f"({used() - before} real TMDB calls this batch, {used()} total)")

        # Phase 2: Series
        remaining = daily_limit - used()
        if remaining > 0:
            result = await db.execute(
                select(EnrichmentQueue)
                .where(
                    or_(
                        EnrichmentQueue.status == "pending",
                        (EnrichmentQueue.status == "skipped") & (EnrichmentQueue.attempts < MAX_ATTEMPTS),
                    ),
                    EnrichmentQueue.media_type == "show",
                )
                .order_by(EnrichmentQueue.created_at)
                .limit(remaining)
            )
            pending_series = list(result.scalars().all())
            logger.info(f"Enrichment Phase 2: {len(pending_series)} pending series")

            for batch_start in range(0, len(pending_series), BATCH_SIZE):
                if used() >= daily_limit:
                    logger.info(f"Enrichment: real TMDB call budget ({daily_limit}) reached, stopping series phase")
                    break
                batch_end = min(batch_start + BATCH_SIZE, len(pending_series))
                batch = pending_series[batch_start:batch_end]

                tasks = [_fetch_series_data(item, semaphore) for item in batch]
                results = await asyncio.gather(*tasks)

                before = used()
                await _apply_enrichment_results(db, results)
                await commit_with_retry(db)

                logger.info(f"Enrichment Series {batch_end}/{len(pending_series)} "
                           f"({used() - before} real TMDB calls this batch, {used()} total)")

    logger.info(
        f"Enrichment batch complete: {used()} real TMDB HTTP calls, "
        f"{omdb_service.get_request_count()} real OMDb HTTP calls used"
    )

    # Heal display_rating from the durable imdb_rating/tmdb_rating columns
    # before the downstream generation + unified_group rebuild. A provider
    # content_hash flip can clobber display_rating back to the raw provider
    # rating (sync_worker), and the blend is recomputable from the persisted
    # columns — so this SQL-only pass restores it. Defensive: a failure here
    # must never crash the whole run.
    try:
        async with async_session_factory() as db:
            await db.execute(recompute_display_rating_stmt())
            await commit_with_retry(db)
    except Exception as e:
        logger.warning(
            "Enrichment: display_rating recompute failed (%s) — skipping heal",
            type(e).__name__,
        )

    # Update queue-size gauges so Prometheus reflects post-batch state.
    from app.utils.metrics import enrichment_queue_size
    from sqlalchemy import func as _sql_func
    async with async_session_factory() as db:
        rows = await db.execute(
            select(EnrichmentQueue.status, _sql_func.count())
            .group_by(EnrichmentQueue.status)
        )
        for status, count in rows.all():
            enrichment_queue_size.labels(status=status).set(count)
