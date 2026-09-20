"""Manual scraper (TMDB + IMDb/OMDb) application service (ADR 0005 D6).

Built up over three waves and now complete: `lookup` / `apply_candidate` /
`unlock` / `clear_ids` / `load_row` (W2), `search_candidates` /
`list_catalogue` / `catalogue_stats` (W4), and the "À vérifier" queue plus
the pure `decide()` rule engine the batch worker drives (W5).

`apply_candidate` is the one function with real stakes: it is the ONLY
writer, besides `enrichment_worker`, that may set `media.match_locked`. Its
three phases are strictly separated (CLAUDE.md piège 8 / ADR 0005 D6 §3):
read (short session) -> network (zero DB access) -> write
(`write_with_retry`, fresh session per attempt, zero network). Never
interleave these — a network call inside the `write_with_retry` callback
would be retried up to 4x on a lock, hammering TMDB/OMDb for what should be
a single logical write.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Literal

import httpx
from sqlalchemy import and_, case, exists, func, not_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.database import EnrichmentQueue, Media, ScrapeReview
from app.services import (
    omdb_scrape_cache_service,
    poster_match_service,
    scrape_cache_service,
    unified_group_service,
)
from app.services.media_identity_writer import (
    build_identity_values,
    build_omdb_replace_extras,
    build_rating_values,
)
from app.services.omdb_service import OMDbData, omdb_service
from app.services.omdb_service import count_requests as omdb_count_requests
from app.services.poster_match_service import PosterComparison
from app.services.tmdb_service import (
    POSTER_W185_BASE,
    TITLE_WEIGHT,
    YEAR_WEIGHT,
    TMDBEnrichmentData,
    tmdb_service,
)
from app.services.tmdb_service import count_requests as tmdb_count_requests
from app.services.tmdb_service import title_similarity, year_score
from app.utils.db_retry import write_with_retry
from app.utils.time import now_ms
from app.utils.unification import calculate_history_group_key, calculate_unification_id

logger = logging.getLogger("plexhub.manual_scrape")

# Media.type vocabulary ("movie"/"show"); TMDB's own "movie"/"tv" kind is
# derived from it at every call site below (media_type == "movie" -> "movie",
# else -> "tv" — the show/tv naming mismatch between our schema and TMDB's
# API is intentional and confined to this translation).
MediaKind = Literal["movie", "show"]
Provider = Literal["auto", "tmdb", "omdb"]
MatchSource = Literal["manual", "batch_auto", "batch_pair"]
IdFilter = Literal[
    "all", "missing_imdb", "missing_tmdb", "missing_both", "incomplete",
    "locked", "batch", "review",
]

# Combined score weights (ADR 0005 D6) — text score dominates, the poster
# comparison refines it. A candidate with no usable poster comparison keeps
# its text score unchanged rather than being penalised: "no image evidence"
# must never rank below "image evidence says different".
TEXT_WEIGHT, IMAGE_WEIGHT = 0.6, 0.4
RULE_B_MIN_TITLE_SCORE = 0.6

# Hard cap on candidates returned to the UI / persisted to a review row.
MAX_CANDIDATES = 10


def _tmdb_kind(media_type: MediaKind) -> Literal["movie", "tv"]:
    return "movie" if media_type == "movie" else "tv"


@dataclass
class Candidate:
    """One scrape candidate surfaced to the operator (ADR 0005 D6)."""
    provider: Literal["tmdb", "omdb"]
    tmdb_id: int | None
    imdb_id: str | None
    media_type: MediaKind
    title: str
    original_title: str | None
    year: int | None
    overview: str | None
    poster_url: str | None
    title_score: float
    text_confidence: float
    poster: PosterComparison | None
    combined_score: float
    text_safe: bool
    recommended: bool

    def to_json(self) -> dict[str, Any]:
        """Frozen-key serialization (ADR 0005 D8) — consumed by the future
        `scrape_review.candidates_json` (W5). Field set fixed by the ADR."""
        badge = self.poster.badge if self.poster is not None else None
        phash_distance = self.poster.phash_distance if self.poster is not None else None
        dhash_distance = self.poster.dhash_distance if self.poster is not None else None
        image_score = self.poster.image_score if self.poster is not None else None
        return {
            "provider": self.provider,
            "tmdb_id": self.tmdb_id,
            "imdb_id": self.imdb_id,
            "media_type": self.media_type,
            "title": self.title,
            "original_title": self.original_title,
            "year": self.year,
            "overview": self.overview,
            "poster_url": self.poster_url,
            "title_score": self.title_score,
            "text_confidence": self.text_confidence,
            "combined_score": self.combined_score,
            "text_safe": self.text_safe,
            "recommended": self.recommended,
            "badge": badge,
            "phash_distance": phash_distance,
            "dhash_distance": dhash_distance,
            "image_score": image_score,
        }


@dataclass
class SearchResult:
    """Result of `search_candidates` (W4) or `lookup` (W2) — a candidate
    list plus enough context for the UI/`decide()` to reason about it."""
    query: "ScrapeQuery"
    candidates: list[Candidate]
    text_verdict: Literal["matched", "ambiguous", "nomatch"]
    xtream_poster_available: bool
    xtream_poster_generic: bool
    tmdb_calls: int
    omdb_calls: int


@dataclass
class ScrapeQuery:
    """A manual (or batch-pairing) scrape request (ADR 0005 D6). Only the
    fields `lookup` needs to populate are used in W2 — `search_candidates`
    (W4) is the primary consumer of `provider`/`language`."""
    media_type: MediaKind
    title: str
    year: int | None
    provider: Provider = "auto"
    language: str | None = None


@dataclass
class ApplyOutcome:
    status: Literal[
        "applied", "conflict", "not_found", "provider_not_found", "skipped_locked",
    ]
    server_id: str
    rating_key: str
    media_type: MediaKind
    mode: str | None = None
    old_tmdb_id: str | None = None
    old_imdb_id: str | None = None
    new_tmdb_id: str | None = None
    new_imdb_id: str | None = None
    propagated: int = 0
    conflicts: list[tuple[str, str, str | None, str | None]] = field(default_factory=list)


@dataclass
class CataloguePage:
    """One page of the admin catalogue (ADR 0005 D6). `items` holds ONE row
    per `(server_id, rating_key)` — `media`'s PK has four columns, so an item
    present in N categories exists as N rows (ADR 0005 F1)."""
    items: list[Media]
    total: int
    offset: int
    review_keys: set[tuple[str, str]] = field(default_factory=set)


@dataclass(frozen=True)
class TypeStats:
    """Per-media-type identity coverage, counted on DISTINCT items (not on
    `media` rows — the old `count_movies_missing_external` counted
    category-variant rows and is deliberately not reused, ADR 0005 D6)."""
    total: int
    missing_imdb: int
    missing_tmdb: int
    with_both: int
    locked: int
    review_pending: int = 0


# ─── search_candidates — text search + poster re-ranking (ADR 0005 D6) ──────


async def _safe_tmdb_search(
    kind: Literal["movie", "tv"], title: str, year: int | None,
    *, language: str | None, summary: str | None,
):
    """`tmdb_service.search_candidates`, never raising.

    NOTE (ADR 0005 F2): the TMDB client injects `api_key` as a query param on
    every request and `httpx.HTTPStatusError.__str__` embeds the full request
    URL — so the raw exception text is NEVER logged here, only its type (and
    the HTTP status when there is one)."""
    from app.services.tmdb_service import CandidateSearch, TMDBSearchOutcome

    try:
        return await tmdb_service.search_candidates(
            kind, title, year, language=language, summary=summary,
        )
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "manual_scrape: TMDB search failed for %s (HTTP %s)",
            kind, exc.response.status_code,
        )
    except Exception as exc:
        logger.warning(
            "manual_scrape: TMDB search failed for %s (%s)", kind, type(exc).__name__,
        )
    return CandidateSearch(verdict=TMDBSearchOutcome("nomatch"), candidates=[])


async def _safe_match_extras(tmdb_id: int, kind: Literal["movie", "tv"]):
    """`get_match_extras` already returns None on failure; this only guards
    against an unexpected raise (same no-`str(exc)` rule as above)."""
    try:
        return await tmdb_service.get_match_extras(tmdb_id, kind)
    except Exception as exc:
        logger.warning(
            "manual_scrape: TMDB extras failed for %s/%s (%s)",
            kind, tmdb_id, type(exc).__name__,
        )
        return None


async def _safe_omdb_search(title: str, year: int | None, media_type: MediaKind) -> list:
    """`omdb_service.search_list`, never raising. Same secret-safety rule:
    OMDb's key rides on the URL as `apikey`, so no `str(exc)` either."""
    try:
        return await omdb_service.search_list(title, year, media_type)
    except Exception as exc:
        logger.warning("manual_scrape: OMDb search failed (%s)", type(exc).__name__)
        return []


async def _safe_compare_posters(
    xtream_poster_url: str | None, variants: list[str],
) -> PosterComparison | None:
    """`poster_match_service.compare_posters`, never raising — a poster
    comparison is an ADVISORY signal, a failure must degrade the candidate
    card to "no image evidence", never fail the whole search. Never logs a
    URL (an Xtream poster URL can embed account credentials)."""
    try:
        return await poster_match_service.compare_posters(xtream_poster_url, variants)
    except Exception as exc:
        logger.warning("manual_scrape: poster compare failed (%s)", type(exc).__name__)
        return None


async def search_candidates(
    query: ScrapeQuery,
    *,
    xtream_poster_url: str | None,
    summary: str | None = None,
    poster_top_n: int | None = None,
) -> SearchResult:
    """Full manual-scrape candidate search (ADR 0005 D6).

    1. TMDB (unless `provider == "omdb"`): with the year, then without it,
       then in `en-US` — the same widening chain
       `enrichment_worker._search_with_fallback` uses, minus `/search/multi`
       (a manual search shows candidates, it doesn't need a last-resort
       auto-match). Results are merged and deduped by `tmdb_id`, keeping the
       best confidence seen for each.
    2. OMDb `?s=` when `provider == "omdb"`, or when `provider == "auto"`
       produced zero TMDB candidates. `provider == "tmdb"` never calls OMDb.
    3. Poster comparison on the `poster_top_n` best candidates BY TEXT SCORE
       (default `SCRAPE_INTERACTIVE_POSTER_CANDIDATES`), via
       `get_match_extras` (which also resolves each candidate's imdb_id).
    4. `combined_score = 0.6*text + 0.4*image`, sorted, at most one
       `recommended`.
    """
    kind = _tmdb_kind(query.media_type)
    language = query.language or settings.TMDB_LANGUAGE
    top_n = (
        poster_top_n if poster_top_n is not None
        else settings.SCRAPE_INTERACTIVE_POSTER_CANDIDATES
    )

    text_verdict: Literal["matched", "ambiguous", "nomatch"] = "nomatch"
    candidates: list[Candidate] = []

    with tmdb_count_requests() as tmdb_tally, omdb_count_requests() as omdb_tally:
        best_by_id: dict[int, Any] = {}
        matched_ids: set[int] = set()

        if query.provider != "omdb":
            attempts = [await _safe_tmdb_search(
                kind, query.title, query.year, language=language, summary=summary,
            )]
            if attempts[-1].verdict.result != "matched" and query.year is not None:
                attempts.append(await _safe_tmdb_search(
                    kind, query.title, None, language=language, summary=summary,
                ))
            if attempts[-1].verdict.result != "matched":
                attempts.append(await _safe_tmdb_search(
                    kind, query.title, query.year, language="en-US", summary=summary,
                ))

            for attempt in attempts:
                for scored in attempt.candidates:
                    previous = best_by_id.get(scored.tmdb_id)
                    if previous is None or scored.confidence > previous.confidence:
                        best_by_id[scored.tmdb_id] = scored

            for attempt in attempts:
                if attempt.verdict.result == "matched":
                    text_verdict = "matched"
                    if attempt.verdict.match is not None:
                        matched_ids.add(attempt.verdict.match.tmdb_id)
                    break
            else:
                if any(a.verdict.result == "ambiguous" for a in attempts):
                    text_verdict = "ambiguous"

            ordered = sorted(
                best_by_id.values(),
                key=lambda sc: (sc.confidence, sc.vote_count), reverse=True,
            )[:MAX_CANDIDATES]
            candidates = [
                Candidate(
                    provider="tmdb",
                    tmdb_id=sc.tmdb_id,
                    imdb_id=None,
                    media_type=query.media_type,
                    title=sc.title,
                    original_title=sc.original_title,
                    year=sc.year,
                    overview=sc.overview,
                    poster_url=(
                        f"{POSTER_W185_BASE}{sc.poster_path}" if sc.poster_path else None
                    ),
                    title_score=sc.title_score,
                    text_confidence=sc.confidence,
                    poster=None,
                    combined_score=sc.confidence,
                    # `text_safe` means "the auto-matcher would have picked
                    # THIS candidate on its own" — not merely "some attempt
                    # matched something".
                    text_safe=sc.tmdb_id in matched_ids,
                    recommended=False,
                )
                for sc in ordered
            ]

        if query.provider == "omdb" or (query.provider == "auto" and not candidates):
            for hit in (await _safe_omdb_search(
                query.title, query.year, query.media_type,
            ))[:MAX_CANDIDATES]:
                hit_title_score = title_similarity(query.title, hit.title)
                confidence = (
                    TITLE_WEIGHT * hit_title_score
                    + YEAR_WEIGHT * year_score(query.year, hit.year)
                )
                candidates.append(Candidate(
                    provider="omdb",
                    tmdb_id=None,
                    imdb_id=hit.imdb_id,
                    media_type=query.media_type,
                    title=hit.title,
                    original_title=None,
                    year=hit.year,
                    overview=None,
                    poster_url=hit.poster_url,
                    title_score=hit_title_score,
                    text_confidence=confidence,
                    poster=None,
                    combined_score=confidence,
                    text_safe=False,
                    recommended=False,
                ))

        # --- poster comparison on the top-N by TEXT score -------------------
        xtream_poster_generic = False
        for candidate in sorted(
            candidates, key=lambda c: c.text_confidence, reverse=True,
        )[:max(0, top_n)]:
            variants: list[str] = []
            if candidate.provider == "tmdb" and candidate.tmdb_id is not None:
                extras = await _safe_match_extras(candidate.tmdb_id, kind)
                if extras is not None:
                    candidate.imdb_id = extras.imdb_id or candidate.imdb_id
                    variants = list(extras.poster_urls)
                    if not candidate.poster_url and extras.poster_urls:
                        candidate.poster_url = extras.poster_urls[0]
            elif candidate.poster_url:
                # An OMDb hit has exactly one poster (ADR 0005 D6).
                variants = [candidate.poster_url]

            if not xtream_poster_url or not variants:
                # No Xtream reference poster (or no candidate poster at all):
                # skip the download entirely rather than fetch images whose
                # comparison could only ever be "unknown".
                continue
            comparison = await _safe_compare_posters(xtream_poster_url, variants)
            if comparison is None:
                continue
            candidate.poster = comparison
            xtream_poster_generic = xtream_poster_generic or comparison.xtream_generic

        tmdb_calls = tmdb_tally.count
        omdb_calls = omdb_tally.count

    for candidate in candidates:
        image_score = (
            candidate.poster.image_score
            if candidate.poster is not None and candidate.poster.badge != "unknown"
            else None
        )
        candidate.combined_score = (
            TEXT_WEIGHT * candidate.text_confidence + IMAGE_WEIGHT * image_score
            if image_score is not None
            else candidate.text_confidence
        )

    candidates.sort(key=lambda c: (c.combined_score, c.text_confidence), reverse=True)
    candidates = candidates[:MAX_CANDIDATES]
    if candidates:
        top = candidates[0]
        # At most one recommendation, and only when there is a real reason:
        # the text auto-matcher chose it, or its poster is the same image.
        top.recommended = bool(
            top.text_safe or (top.poster is not None and top.poster.badge == "identical")
        )

    return SearchResult(
        query=query,
        candidates=candidates,
        text_verdict=text_verdict,
        xtream_poster_available=bool(xtream_poster_url),
        xtream_poster_generic=xtream_poster_generic,
        tmdb_calls=tmdb_calls,
        omdb_calls=omdb_calls,
    )


# ─── lookup — id/URL -> single candidate, no poster compare (ADR 0005 D6) ───

_TT_RE = re.compile(r"^tt\d+$")
_TMDB_INT_RE = re.compile(r"^\d+$")
_IMDB_URL_RE = re.compile(r"imdb\.com/title/(tt\d+)", re.IGNORECASE)
_TMDB_URL_RE = re.compile(r"themoviedb\.org/(movie|tv)/(\d+)", re.IGNORECASE)


def _parse_raw_id(raw_id: str) -> tuple[str | None, int | None]:
    """Parse an operator-typed id/URL into (imdb_id, tmdb_id) — at most one
    is set. Raises ValueError on anything unrecognized (ADR 0005 D6:
    "Non parsable -> ValueError (route -> 422)")."""
    raw = (raw_id or "").strip()
    if not raw:
        raise ValueError("empty id")

    m = _IMDB_URL_RE.search(raw)
    if m:
        return m.group(1), None
    if _TT_RE.match(raw):
        return raw, None

    m = _TMDB_URL_RE.search(raw)
    if m:
        return None, int(m.group(2))
    if _TMDB_INT_RE.match(raw):
        return None, int(raw)

    raise ValueError(f"unrecognized scrape id/url: {raw_id!r}")


async def lookup(
    raw_id: str, *, media_type: MediaKind, xtream_poster_url: str | None,
) -> SearchResult:
    """Resolve an operator-typed id/URL to a single preview candidate
    (ADR 0005 D6). Accepts a bare ``tt\\d+``, an integer TMDB id, an IMDb
    title URL, or a TMDB movie/tv URL. imdb -> `find_by_imdb_id` then
    `get_match_extras`; on failure (or given directly as a tmdb id) ->
    `get_match_extras` directly; a bare imdb id that TMDB can't resolve at
    all falls back to an OMDb-only card (ADR: "sinon OMDb `get_or_fetch`
    (carte OMDb seule)"). Always at most ONE candidate, `recommended=False`
    (ADR: "1 candidat, recommended=False") — the operator confirms via
    Appliquer, this is a preview, not an auto-match."""
    imdb_id, tmdb_id = _parse_raw_id(raw_id)
    kind = _tmdb_kind(media_type)

    candidates: list[Candidate] = []
    tmdb_calls = 0
    omdb_calls = 0

    resolved_tmdb_id = tmdb_id
    if resolved_tmdb_id is None and imdb_id is not None:
        resolved_tmdb_id = await tmdb_service.find_by_imdb_id(imdb_id, kind)
        tmdb_calls += 1

    if resolved_tmdb_id is not None:
        extras = await tmdb_service.get_match_extras(resolved_tmdb_id, kind)
        tmdb_calls += 1
        if extras is not None:
            candidates.append(Candidate(
                provider="tmdb",
                tmdb_id=extras.tmdb_id,
                imdb_id=extras.imdb_id or imdb_id,
                media_type=media_type,
                title=extras.title,
                original_title=extras.original_title,
                year=extras.year,
                overview=extras.overview,
                poster_url=extras.poster_urls[0] if extras.poster_urls else None,
                title_score=1.0,
                text_confidence=1.0,
                poster=None,
                combined_score=1.0,
                text_safe=False,
                recommended=False,
            ))

    if not candidates and imdb_id is not None:
        # Bare imdb id TMDB couldn't resolve at all -> OMDb-only preview card.
        from app.db.database import async_session_factory

        omdb_data, _pending_put = await omdb_scrape_cache_service.get_or_fetch(
            imdb_id, session_factory=async_session_factory, client=omdb_service,
        )
        omdb_calls += 1
        if omdb_data is not None:
            year = None
            m = re.match(r"^(\d{4})", omdb_data.year or "")
            if m:
                year = int(m.group(1))
            candidates.append(Candidate(
                provider="omdb",
                tmdb_id=None,
                imdb_id=omdb_data.imdb_id or imdb_id,
                media_type=media_type,
                title=omdb_data.title,
                original_title=None,
                year=year,
                overview=omdb_data.plot,
                poster_url=None,
                title_score=1.0,
                text_confidence=1.0,
                poster=None,
                combined_score=1.0,
                text_safe=False,
                recommended=False,
            ))

    first = candidates[0] if candidates else None
    return SearchResult(
        query=ScrapeQuery(
            media_type=media_type,
            title=first.title if first else raw_id,
            year=first.year if first else None,
        ),
        candidates=candidates,
        text_verdict="matched" if candidates else "nomatch",
        xtream_poster_available=bool(xtream_poster_url),
        xtream_poster_generic=False,
        tmdb_calls=tmdb_calls,
        omdb_calls=omdb_calls,
    )


# ─── apply_candidate — the write path (ADR 0005 D6 §3) ──────────────────────


async def _fetch_tmdb_details(
    tmdb_id: int, media_type: MediaKind,
) -> TMDBEnrichmentData | None:
    """`get_movie_details`/`get_tv_details` for `tmdb_id`. Returns None on a
    404 or any transport failure (never raises) — the caller maps that to
    `provider_not_found`."""
    try:
        if media_type == "movie":
            return await tmdb_service.get_movie_details(tmdb_id)
        return await tmdb_service.get_tv_details(tmdb_id)
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "manual_scrape: TMDB details fetch failed for %s/%s (HTTP %s)",
            media_type, tmdb_id, exc.response.status_code,
        )
        return None
    except Exception as exc:
        logger.warning(
            "manual_scrape: TMDB details fetch failed for %s/%s (%s)",
            media_type, tmdb_id, type(exc).__name__,
        )
        return None


def _is_safe_title_key(title: Any) -> bool:
    """True when `title` yields a title-based unification key specific enough
    to fan an identity out over (ADR 0005 D6, `propagate`).

    `calculate_unification_id` returns `""` for "Unknown" and a bare
    `title__<year>` for a title whose characters all normalize away (Arabic,
    Cyrillic, CJK…) — keys shared by every such title of that year. Mirrors
    `aggregation_service._absorb_title_groups`'s guard."""
    base = calculate_unification_id(title or "", None)  # 'title_<norm>' / ''
    if not base.startswith("title_"):
        return False
    return any(c.isalnum() for c in base[len("title_"):])


def _normalize_imdb(raw: str | None) -> str | None:
    if not raw:
        return None
    return raw if raw.startswith("tt") else f"tt{raw}"


async def apply_candidate(
    *,
    server_id: str,
    rating_key: str,
    media_type: MediaKind,
    tmdb_id: int | None,
    imdb_id: str | None,
    source: MatchSource = "manual",
    confidence: float | None = None,
    force: bool = False,
    propagate: bool = False,
    schedule_rebuild: bool = True,
    session_factory: Callable[[], AsyncSession],
) -> ApplyOutcome:
    """Apply a chosen TMDB/IMDb identity to one media row (ADR 0005 D6 §3).

    Three strictly separated phases: read (short session), network (zero DB
    access), write (`write_with_retry`, fresh session per attempt, zero
    network — CLAUDE.md piège 8)."""
    if tmdb_id is None and imdb_id is None:
        raise ValueError("apply_candidate requires tmdb_id or imdb_id")

    imdb_id = _normalize_imdb(imdb_id) if imdb_id else None

    # --- Phase 1: read (short session) ---------------------------------
    async with session_factory() as db:
        current = (await db.execute(
            select(Media).where(
                Media.rating_key == rating_key, Media.server_id == server_id,
            ).limit(1)
        )).scalars().first()
    if current is None:
        return ApplyOutcome(
            status="not_found", server_id=server_id, rating_key=rating_key,
            media_type=media_type,
        )

    old_tmdb_id = current.tmdb_id or None
    old_imdb_id = current.imdb_id or None
    is_adult = bool(current.is_adult)
    # `media_type` is derived from the row itself, never trusted from the
    # caller: the row form posts a hardcoded type, and a batch/UI mismatch
    # would search TMDB's /movie endpoint for a show (or vice versa) and
    # then write that identity onto the row anyway.
    media_type = "movie" if (current.type or "") == "movie" else "show"
    # Title-only fallback key ("title_..." or ""), used to find un-identified
    # twins for `propagate=True` — independent of this row's OWN new ids.
    title_key = calculate_unification_id(current.title or "", current.year)
    if propagate and not _is_safe_title_key(current.title):
        # Degenerate title (empty/"Unknown", or non-Latin normalizing to a
        # bare `title__YYYY`): EVERY such title of that year shares the key,
        # so propagating would stamp this film's identity — and a lock —
        # onto unrelated films. Same guard as
        # `aggregation_service._absorb_title_groups` (aggregation_service.py
        # :236-238), for exactly the same false-merge reason.
        logger.info(
            "manual_scrape: propagate disabled for %s/%s (degenerate title key)",
            server_id, rating_key,
        )
        propagate = False

    # --- Phase 2: network (zero DB access) ------------------------------
    details: TMDBEnrichmentData | None = None
    final_tmdb: str | None = None
    final_imdb: str | None = imdb_id
    self_mismatch = False

    if tmdb_id is not None:
        details = await _fetch_tmdb_details(tmdb_id, media_type)
        if details is None:
            return ApplyOutcome(
                status="provider_not_found", server_id=server_id,
                rating_key=rating_key, media_type=media_type,
                old_tmdb_id=old_tmdb_id, old_imdb_id=old_imdb_id,
            )
        final_tmdb = str(details.tmdb_id)
        if details.imdb_id:
            if imdb_id and imdb_id != details.imdb_id:
                self_mismatch = True
            final_imdb = details.imdb_id
    else:
        assert imdb_id is not None
        kind = _tmdb_kind(media_type)
        resolved_tmdb_id = await tmdb_service.find_by_imdb_id(imdb_id, kind)
        if resolved_tmdb_id is not None:
            details = await _fetch_tmdb_details(resolved_tmdb_id, media_type)
        if details is not None:
            final_tmdb = str(details.tmdb_id)
            final_imdb = details.imdb_id or imdb_id

    # OMDb fetch for ratings/replies — always attempted once we have a final
    # imdb id in hand (identity-only path already needs it to even proceed).
    omdb_data: OMDbData | None = None
    omdb_pending_put: tuple[str, str] | None = None
    if final_imdb:
        omdb_data, omdb_pending_put = await omdb_scrape_cache_service.get_or_fetch(
            final_imdb, session_factory=session_factory, client=omdb_service,
        )

    if details is None:
        # "identité imdb seule" (ADR 0005 D6): no TMDB match at all —
        # nothing to apply unless OMDb resolved something for this imdb id.
        if omdb_data is None:
            return ApplyOutcome(
                status="provider_not_found", server_id=server_id,
                rating_key=rating_key, media_type=media_type,
                old_tmdb_id=old_tmdb_id, old_imdb_id=old_imdb_id,
            )
        final_imdb = omdb_data.imdb_id or final_imdb

    # --- Conflict control (read, hors transaction d'écriture) -----------
    conflicts: list[tuple[str, str, str | None, str | None]] = []
    conflict_conditions = []
    if final_tmdb:
        conflict_conditions.append(and_(
            Media.tmdb_id == final_tmdb,
            Media.imdb_id.notin_(["", final_imdb or ""]),
        ))
    if final_imdb:
        conflict_conditions.append(and_(
            Media.imdb_id == final_imdb,
            Media.tmdb_id.notin_(["", final_tmdb or ""]),
        ))
    if conflict_conditions:
        async with session_factory() as db:
            rows = (await db.execute(
                select(Media.server_id, Media.rating_key, Media.tmdb_id, Media.imdb_id)
                .where(
                    Media.type == media_type,
                    or_(*conflict_conditions),
                    not_(and_(
                        Media.rating_key == rating_key, Media.server_id == server_id,
                    )),
                )
                .distinct()
                .limit(10)
            )).all()
        conflicts = [(r.server_id, r.rating_key, r.tmdb_id, r.imdb_id) for r in rows]

    if (self_mismatch or conflicts) and not force:
        return ApplyOutcome(
            status="conflict", server_id=server_id, rating_key=rating_key,
            media_type=media_type,
            old_tmdb_id=old_tmdb_id, old_imdb_id=old_imdb_id,
            new_tmdb_id=final_tmdb, new_imdb_id=final_imdb,
            conflicts=conflicts,
        )

    confidence_final = 1.0 if source in ("manual", "batch_pair") else confidence
    mode = "replace" if (
        (old_tmdb_id and old_tmdb_id != final_tmdb)
        or (old_imdb_id and old_imdb_id != final_imdb)
    ) else "fill"

    def _identity_values(write_mode: str) -> dict[str, Any]:
        if details is not None:
            # `details.imdb_id` is None whenever TMDB has no external id for
            # this title (common on TV and obscure films). Writing that None
            # would silently DROP the imdb id the operator typed — or the one
            # `find_by_imdb_id` just resolved this identity from — and leave
            # the row on a `tmdb://` key although an imdb id is in hand
            # (`calculate_unification_id` prioritizes imdb). Carry `final_imdb`
            # into the builder so DB, unification key and ApplyOutcome agree.
            data = details if details.imdb_id == final_imdb else replace(
                details, imdb_id=final_imdb,
            )
            return build_identity_values(
                data, confidence=confidence_final,
                omdb=omdb_data if write_mode == "replace" else None,
                mode=write_mode, is_adult=is_adult,
            )
        # OMDb-only identity (no TMDB match found at all) — always a direct
        # identity assignment, TMDB id explicitly cleared (a stale/wrong
        # tmdb_id must not survive a correction that found no TMDB match).
        new_unif = calculate_unification_id(
            current.title or "", current.year, imdb_id=final_imdb,
        )
        values: dict[str, Any] = {
            "tmdb_id": None,
            "imdb_id": final_imdb,
            "unification_id": new_unif,
            "history_group_key": new_unif,
            "tmdb_match_confidence": confidence_final,
        }
        if write_mode == "replace":
            # A correction must not leave the WRONG film's poster, summary or
            # tmdb_rating behind just because the new identity resolved
            # through OMDb instead of TMDB (ADR 0005 D4 "replace").
            values.update(
                build_omdb_replace_extras(omdb_data, is_adult=is_adult)
            )
        return values

    def _rating_values(write_mode: str) -> dict[str, Any]:
        return build_rating_values(
            omdb_imdb_rating=omdb_data.imdb_rating if omdb_data else None,
            omdb_imdb_votes=omdb_data.imdb_votes if omdb_data else None,
            tmdb_rating=details.tmdb_rating if details else None,
            mode=write_mode,
        )

    id_values = _identity_values(mode)
    rating_values = _rating_values(mode)
    ts = now_ms()
    target_values = {
        **id_values, **rating_values,
        "match_locked": True, "match_source": source, "updated_at": ts,
    }

    # --- Phase 3: write (write_with_retry, fresh session per attempt) ---
    async def work(session: AsyncSession) -> int | None:
        """Returns the propagated-row count (0 if `propagate` was False or
        nothing matched) on success, or `None` if the target row's own
        UPDATE matched zero rows (`skipped_locked`: a `source != "manual"`
        write lost a race against an operator's manual lock)."""
        q = update(Media).where(
            Media.rating_key == rating_key, Media.server_id == server_id,
        )
        if source != "manual":
            # A batch write must never clobber a row an operator already
            # locked by hand — a manual apply always may (re-locking is
            # exactly what "override" means for a human operator).
            q = q.where(Media.match_locked == False)  # noqa: E712
        result = await session.execute(q.values(**target_values))
        if result.rowcount == 0:
            await session.commit()
            return None

        # TMDB title-cache is only meaningful when we actually have a TMDB
        # result to cache under it (the OMDb-only identity path has none).
        if details is not None:
            cache_key = scrape_cache_service.make_key(
                media_type, current.title or "", current.year,
            )
            await scrape_cache_service.put(
                session, cache_key, media_type, "matched",
                confidence_final, details, ts,
            )
        if omdb_pending_put is not None:
            pending_imdb, pending_result = omdb_pending_put
            await omdb_scrape_cache_service.put(
                session, pending_imdb, pending_result, omdb_data, ts,
            )

        await session.execute(
            update(EnrichmentQueue)
            .where(
                EnrichmentQueue.rating_key == rating_key,
                EnrichmentQueue.server_id == server_id,
            )
            .values(status="done", processed_at=ts)
        )
        # Close the review row (if any) in the SAME transaction as the write
        # that settles the identity (ADR 0005 D6 §3) — a separate statement
        # afterwards could commit the media write and then lose the review
        # update to a crash/lock, leaving the operator an already-fixed item
        # still sitting in the "À vérifier" queue forever.
        await session.execute(
            update(ScrapeReview)
            .where(
                ScrapeReview.rating_key == rating_key,
                ScrapeReview.server_id == server_id,
                ScrapeReview.status == "pending",
            )
            .values(status="resolved", resolved_at=ts)
        )

        propagated = 0
        if propagate:
            prop_id_values = _identity_values("fill")
            prop_rating_values = _rating_values("fill")
            prop_values = {
                **prop_id_values, **prop_rating_values,
                "match_locked": True, "match_source": source, "updated_at": ts,
            }
            prop_where = (
                Media.type == media_type,
                Media.unification_id == title_key,
                Media.match_locked == False,  # noqa: E712
                func.coalesce(Media.imdb_id, "") == "",
                func.coalesce(Media.tmdb_id, "") == "",
                not_(and_(
                    Media.rating_key == rating_key, Media.server_id == server_id,
                )),
            )
            prop_rows = (await session.execute(
                select(Media.server_id, Media.rating_key).where(*prop_where).distinct()
            )).all()
            if prop_rows:
                await session.execute(
                    update(Media).where(*prop_where).values(**prop_values)
                )
            propagated = len(prop_rows)

        await session.commit()
        return propagated

    outcome = await write_with_retry(
        work, session_factory=session_factory, op="manual_scrape.apply",
    )
    if outcome is None:
        # A manual apply never filters on `match_locked`, so a zero rowcount
        # there can only mean the row vanished between phase 1 and phase 3.
        return ApplyOutcome(
            status="skipped_locked" if source != "manual" else "not_found",
            server_id=server_id, rating_key=rating_key,
            media_type=media_type,
            old_tmdb_id=old_tmdb_id, old_imdb_id=old_imdb_id,
            new_tmdb_id=final_tmdb, new_imdb_id=final_imdb,
        )
    propagated = outcome

    if schedule_rebuild:
        unified_group_service.schedule_rebuild(media_type, session_factory=session_factory)

    return ApplyOutcome(
        status="applied", server_id=server_id, rating_key=rating_key,
        media_type=media_type, mode=mode,
        old_tmdb_id=old_tmdb_id, old_imdb_id=old_imdb_id,
        new_tmdb_id=final_tmdb, new_imdb_id=final_imdb,
        propagated=propagated,
    )


# ─── unlock / clear_ids / load_row ──────────────────────────────────────────


async def unlock(server_id: str, rating_key: str, *, session_factory) -> bool:
    """Clear `match_locked`/`match_source`; ids are left untouched (ADR 0005
    D6)."""

    async def work(session: AsyncSession) -> bool:
        result = await session.execute(
            update(Media)
            .where(Media.rating_key == rating_key, Media.server_id == server_id)
            .values(match_locked=False, match_source=None, updated_at=now_ms())
        )
        await session.commit()
        return result.rowcount > 0

    return await write_with_retry(work, session_factory=session_factory, op="manual_scrape.unlock")


async def clear_ids(
    server_id: str, rating_key: str, *, lock: bool = False, session_factory,
) -> bool:
    """Null out `imdb_id`/`tmdb_id`, recompute the title-based
    `unification_id`/`history_group_key`, optionally re-lock (ADR 0005 D6)."""

    async def work(session: AsyncSession) -> str | None:
        row = (await session.execute(
            select(Media).where(
                Media.rating_key == rating_key, Media.server_id == server_id,
            ).limit(1)
        )).scalars().first()
        if row is None:
            return None
        new_unif = calculate_unification_id(row.title or "", row.year)
        new_hist = calculate_history_group_key(new_unif, rating_key, server_id)
        await session.execute(
            update(Media)
            .where(Media.rating_key == rating_key, Media.server_id == server_id)
            .values(
                imdb_id=None, tmdb_id=None,
                unification_id=new_unif, history_group_key=new_hist,
                tmdb_match_confidence=None,
                match_locked=lock, match_source=("manual" if lock else None),
                updated_at=now_ms(),
            )
        )
        await session.commit()
        return row.type

    media_type = await write_with_retry(
        work, session_factory=session_factory, op="manual_scrape.clear_ids",
    )
    if media_type is None:
        return False
    unified_group_service.schedule_rebuild(media_type, session_factory=session_factory)
    return True


async def load_row(server_id: str, rating_key: str, *, session_factory) -> Media | None:
    """Re-load a row on a FRESH session (ADR 0005 D6) — a caller that just
    wrote via `apply_candidate`/`unlock`/`clear_ids` (each on their own
    fresh session per `write_with_retry` attempt) must not re-read through
    its OWN request-scoped `get_db` session, which may still be pinned to a
    pre-write WAL snapshot."""
    async with session_factory() as db:
        return (await db.execute(
            select(Media).where(
                Media.rating_key == rating_key, Media.server_id == server_id,
            ).limit(1)
        )).scalars().first()


# ─── catalogue listing / stats (ADR 0005 D6, W4) ────────────────────────────

_SORTS = {
    "added_desc": lambda: Media.added_at.desc(),
    "added_asc": lambda: Media.added_at.asc(),
    "title_asc": lambda: Media.title.asc(),
    "title_desc": lambda: Media.title.desc(),
    "year_desc": lambda: Media.year.desc(),
}

_IMDB_MISSING = or_(Media.imdb_id.is_(None), Media.imdb_id == "")
_TMDB_MISSING = or_(Media.tmdb_id.is_(None), Media.tmdb_id == "")


def _pending_review_exists():
    """Correlated `EXISTS scrape_review(status='pending')` for the current
    `media` row (ADR 0005 D8/F1).

    EXISTS, never a JOIN: `media`'s PK has four columns, so an item present
    in N categories is N rows — joining `scrape_review` (keyed on the item)
    would be correct row-wise but is the exact shape that produced duplicate
    rows elsewhere; and unlike a JOIN an EXISTS can never multiply the
    catalogue page."""
    return exists(
        select(ScrapeReview.rating_key)
        .where(
            ScrapeReview.rating_key == Media.rating_key,
            ScrapeReview.server_id == Media.server_id,
            ScrapeReview.status == "pending",
        )
        .correlate(Media)
    )


def _id_filter_clause(id_filter: IdFilter):
    """WHERE fragment for an `IdFilter` (None = no restriction)."""
    if id_filter == "missing_imdb":
        return _IMDB_MISSING
    if id_filter == "missing_tmdb":
        return _TMDB_MISSING
    if id_filter == "missing_both":
        return and_(_IMDB_MISSING, _TMDB_MISSING)
    if id_filter == "incomplete":
        return or_(_IMDB_MISSING, _TMDB_MISSING)
    if id_filter == "locked":
        return Media.match_locked == True  # noqa: E712
    if id_filter == "batch":
        return Media.match_source.in_(("batch_auto", "batch_pair"))
    if id_filter == "review":
        return _pending_review_exists()
    return None


def _catalogue_where(media_type: MediaKind, id_filter: IdFilter, search: str | None):
    # No outage mask (CLAUDE.md piège 20): the operator's diagnostic view
    # must show exactly what a provider outage hides from the app.
    clauses = [
        Media.type == media_type,
        Media.is_in_allowed_categories == True,  # noqa: E712
    ]
    if search:
        safe = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        clauses.append(Media.title.ilike(f"%{safe}%", escape="\\"))
    extra = _id_filter_clause(id_filter)
    if extra is not None:
        clauses.append(extra)
    return clauses


async def list_catalogue(
    db: AsyncSession,
    *,
    media_type: MediaKind,
    id_filter: IdFilter = "incomplete",
    search: str | None = None,
    sort: str = "added_desc",
    page: int = 1,
    page_size: int = 50,
) -> CataloguePage:
    """One page of items of `media_type`, deduplicated to one row per
    `(server_id, rating_key)` (ADR 0005 F1: `media`'s PK carries `filter`
    and `sort_order`, so an item in N categories is N rows)."""
    clauses = _catalogue_where(media_type, id_filter, search)
    offset = max(0, (max(1, page) - 1) * page_size)

    distinct_items = (
        select(Media.server_id, Media.rating_key)
        .where(*clauses)
        .group_by(Media.server_id, Media.rating_key)
    )
    total = (await db.execute(
        select(func.count()).select_from(distinct_items.subquery())
    )).scalar() or 0

    order_by = _SORTS.get(sort, _SORTS["added_desc"])()
    rows = (await db.execute(
        select(Media)
        .where(*clauses)
        .group_by(Media.server_id, Media.rating_key)
        .order_by(order_by)
        .limit(page_size)
        .offset(offset)
    )).scalars().all()

    # Which of THIS page's items sit in the review queue — one extra query
    # scoped to the page's keys (never a join against `media`, ADR 0005 F1).
    items = list(rows)
    review_keys: set[tuple[str, str]] = set()
    if items:
        keys = {(it.server_id, it.rating_key) for it in items}
        pending = (await db.execute(
            select(ScrapeReview.server_id, ScrapeReview.rating_key).where(
                ScrapeReview.status == "pending",
                ScrapeReview.server_id.in_({k[0] for k in keys}),
                ScrapeReview.rating_key.in_({k[1] for k in keys}),
            )
        )).all()
        review_keys = {(r.server_id, r.rating_key) for r in pending} & keys

    return CataloguePage(
        items=items, total=total, offset=offset, review_keys=review_keys,
    )


async def _distinct_count(db: AsyncSession, clauses, extra=None) -> int:
    where = list(clauses) if extra is None else [*clauses, extra]
    sub = (
        select(Media.server_id, Media.rating_key)
        .where(*where)
        .group_by(Media.server_id, Media.rating_key)
        .subquery()
    )
    return (await db.execute(select(func.count()).select_from(sub))).scalar() or 0


def _flag(condition) -> Any:
    """1/0 marker for one `media` ROW, aggregated per item below."""
    return case((condition, 1), else_=0)


async def catalogue_stats(db: AsyncSession) -> dict[str, TypeStats]:
    """Identity coverage per media type, on DISTINCT items in allowed
    categories (ADR 0005 D6).

    ONE pass over `media` for the five id/lock counters, instead of the
    twelve `GROUP BY` scans this used to run (6 counters x 2 types). That
    mattered: on the production catalogue (867k rows / 44.5k items) the old
    shape took 1.7-2.9 s uncontended and **22 s while the pipeline was
    writing** — and `/admin` recomputes it on load AND on every
    `refresh-stats` (so after each apply/save/rescrape/unlock/clear).
    Measured on that same database: **0.12-0.16 s, x18**, with all twelve
    counters byte-identical.

    The per-item aggregation reproduces the old row-level semantics exactly,
    which is why it is NOT a uniform `MAX`: a `media` item is one row per
    category (4-column PK, ADR 0005 F1) and those rows can disagree.
    `missing_*` used to count an item as soon as ONE of its rows lacked the
    id (`MIN`), while `with_both`/`locked` counted it as soon as ONE row
    qualified (`MAX`). Real production data contains such an item — a plain
    `MAX` everywhere shifted `missing_imdb` by one."""
    imdb_row_ok = _flag(and_(Media.imdb_id.isnot(None), Media.imdb_id != ""))
    tmdb_row_ok = _flag(and_(Media.tmdb_id.isnot(None), Media.tmdb_id != ""))
    per_item = (
        select(
            Media.type.label("type"),
            func.min(imdb_row_ok).label("imdb_ok"),
            func.min(tmdb_row_ok).label("tmdb_ok"),
            func.max(
                _flag(and_(not_(_IMDB_MISSING), not_(_TMDB_MISSING)))
            ).label("both_ok"),
            func.max(_flag(Media.match_locked == True)).label("locked"),  # noqa: E712
        )
        .where(
            Media.type.in_(("movie", "show")),
            Media.is_in_allowed_categories == True,  # noqa: E712
        )
        .group_by(Media.rating_key, Media.server_id, Media.type)
        .subquery()
    )
    rows = (await db.execute(
        select(
            per_item.c.type,
            func.count(),
            func.sum(_flag(per_item.c.imdb_ok == 0)),
            func.sum(_flag(per_item.c.tmdb_ok == 0)),
            func.sum(per_item.c.both_ok),
            func.sum(per_item.c.locked),
        ).group_by(per_item.c.type)
    )).all()
    by_type = {r[0]: r for r in rows}

    # Pending reviews come from their own (small) table rather than a sixth
    # pass over `media`; the correlated EXISTS keeps the old rule that a
    # review whose item no longer exists — or left the allowed categories —
    # is not counted.
    review_rows = (await db.execute(
        select(ScrapeReview.media_type, func.count())
        .where(
            ScrapeReview.status == "pending",
            exists(
                select(Media.rating_key).where(
                    Media.rating_key == ScrapeReview.rating_key,
                    Media.server_id == ScrapeReview.server_id,
                    Media.type == ScrapeReview.media_type,
                    Media.is_in_allowed_categories == True,  # noqa: E712
                )
            ),
        )
        .group_by(ScrapeReview.media_type)
    )).all()
    reviews = {r[0]: r[1] for r in review_rows}

    stats: dict[str, TypeStats] = {}
    for media_type in ("movie", "show"):
        row = by_type.get(media_type)
        stats[media_type] = TypeStats(
            total=row[1] if row else 0,
            missing_imdb=(row[2] or 0) if row else 0,
            missing_tmdb=(row[3] or 0) if row else 0,
            with_both=(row[4] or 0) if row else 0,
            locked=(row[5] or 0) if row else 0,
            review_pending=reviews.get(media_type, 0),
        )
    return stats


# ─── decide() — the batch auto-apply rule engine (ADR 0005 D6) ──────────────


@dataclass(frozen=True)
class Decision:
    """What the batch worker should do with one `SearchResult`."""
    action: Literal["apply", "review"]
    rule: Literal["A", "B"] | None
    candidate: Candidate | None
    reason: Literal[
        "text_safe_and_identical", "poster_only_identical", "no_candidates",
        "no_xtream_poster", "xtream_poster_generic", "ambiguous_posters",
        "no_identical_poster", "text_safe_no_poster_match",
        # Identical poster(s) existed but no rule acted on them — the reason
        # must say which, since the operator reads it in the review queue.
        "poster_only_auto_disabled", "identical_poster_weak_text",
    ]


def _year_compatible(query_year: int | None, candidate_year: int | None) -> bool:
    """Rule B's year gate: unknown on either side is NOT evidence against
    (Xtream years are frequently missing or wrong), a known pair must agree
    within one year (release/production-year drift across providers)."""
    if query_year is None or candidate_year is None:
        return True
    return abs(query_year - candidate_year) <= 1


def decide(result: SearchResult, *, poster_only_auto: bool) -> Decision:
    """Decide whether a batch item can be auto-applied (ADR 0005 D6).

    PURE — no I/O, no DB, no clock — so the whole rule table is unit-testable
    and an operator can reason about exactly why an item landed in review.
    The evaluation ORDER is part of the contract (it is what makes "rule A
    beats a generic Xtream poster" true), so do not reorder these branches.

    Two ways in:
    - **A** — a candidate the text auto-matcher would have picked on its own
      (`text_safe`) AND whose poster is the same image. Two independent
      signals agreeing on the SAME candidate; safe with `poster_only_auto`
      off.
    - **B** — exactly one candidate with an identical poster and no text
      backing, behind `SCRAPE_BATCH_POSTER_ONLY_AUTO` (off by default until a
      dry-run histogram has calibrated the thresholds on real data).

    Everything else goes to review, with the reason that explains it.
    """
    candidates = result.candidates
    if not candidates:
        return Decision("review", None, None, "no_candidates")

    has_text_safe = any(c.text_safe for c in candidates)

    if not result.xtream_poster_available:
        # Nothing to corroborate a text match against. A text-safe candidate
        # is still reported distinctly: it is the population an operator may
        # later decide to bulk-accept, which "no poster at all" is not.
        return Decision(
            "review", None, None,
            "text_safe_no_poster_match" if has_text_safe else "no_xtream_poster",
        )

    # Rule A — both signals on the SAME candidate.
    for candidate in candidates:
        if (
            candidate.text_safe
            and candidate.poster is not None
            and candidate.poster.badge == "identical"
        ):
            return Decision("apply", "A", candidate, "text_safe_and_identical")

    if result.xtream_poster_generic:
        # A placeholder poster matches every other title carrying the same
        # placeholder — image evidence is worthless here, whatever the badge.
        return Decision("review", None, None, "xtream_poster_generic")

    identical = [
        c for c in candidates if c.poster is not None and c.poster.badge == "identical"
    ]

    if poster_only_auto:
        if len(identical) > 1:
            return Decision("review", None, None, "ambiguous_posters")
        if len(identical) == 1:
            candidate = identical[0]
            if (
                candidate.title_score >= RULE_B_MIN_TITLE_SCORE
                and _year_compatible(result.query.year, candidate.year)
            ):
                return Decision("apply", "B", candidate, "poster_only_identical")

    # The reason is stored on the review row and shown to the operator, so it
    # must say what actually happened — "no_identical_poster" would be a lie
    # whenever identical posters DID exist but a rule declined to act on them.
    if has_text_safe:
        # A text-safe candidate exists but rule A did not fire, so its own
        # poster is not the identical one: the two signals disagree.
        reason = "text_safe_no_poster_match"
    elif identical:
        if not poster_only_auto:
            reason = "poster_only_auto_disabled"
        elif len(identical) > 1:
            reason = "ambiguous_posters"
        else:
            reason = "identical_poster_weak_text"
    else:
        reason = "no_identical_poster"
    return Decision("review", None, None, reason)


# ─── "A verifier" review queue (ADR 0005 D8) ────────────────────────────────

MAX_REVIEW_CANDIDATES = 5


def _media_exists_for_review():
    """Correlated `EXISTS media`, used to hide orphan review rows (an item
    whose `media` rows vanished between the batch run and the read). EXISTS,
    never an INNER JOIN: `media`'s 4-column PK would multiply the review row
    once per category variant (ADR 0005 F1/D8)."""
    return exists(
        select(Media.rating_key)
        .where(
            Media.rating_key == ScrapeReview.rating_key,
            Media.server_id == ScrapeReview.server_id,
        )
        .correlate(ScrapeReview)
    )


async def list_reviews(
    db: AsyncSession,
    *,
    media_type: MediaKind | None = None,
    page: int = 1,
    page_size: int = 25,
) -> tuple[list[ScrapeReview], int]:
    """One page of PENDING reviews, newest first, orphans masked."""
    clauses = [ScrapeReview.status == "pending", _media_exists_for_review()]
    if media_type is not None:
        clauses.append(ScrapeReview.media_type == media_type)

    total = (await db.execute(
        select(func.count()).select_from(
            select(ScrapeReview.rating_key, ScrapeReview.server_id)
            .where(*clauses).subquery()
        )
    )).scalar() or 0

    offset = max(0, (max(1, page) - 1) * page_size)
    rows = (await db.execute(
        select(ScrapeReview)
        .where(*clauses)
        .order_by(ScrapeReview.created_at.desc())
        .limit(page_size)
        .offset(offset)
    )).scalars().all()
    return list(rows), total


def review_candidates(row: ScrapeReview) -> list[dict[str, Any]]:
    """Decode `candidates_json` for rendering. Never raises: a malformed or
    truncated payload degrades to "no candidates" (the operator still gets
    the row, its title and the "Autres..." escape hatch into the live scrape
    panel) rather than 500-ing the whole review page."""
    try:
        data = json.loads(row.candidates_json or "[]")
    except (ValueError, TypeError):
        logger.warning(
            "manual_scrape: unreadable candidates_json for %s/%s",
            row.server_id, row.rating_key,
        )
        return []
    return [c for c in data if isinstance(c, dict)] if isinstance(data, list) else []


async def upsert_review(
    *,
    server_id: str,
    rating_key: str,
    media_type: MediaKind,
    title: str | None,
    year: int | None,
    reason: str,
    candidates: list[Candidate],
    session_factory: Callable[[], AsyncSession],
) -> None:
    """Queue (or re-open) an item for manual review (ADR 0005 D8).

    Re-running a batch over an item whose review was `resolved`/`dismissed`
    puts it back to `pending` with fresh candidates — the batch selection
    query already excludes both states, so this only happens when the
    operator deliberately re-queues it."""
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    top = candidates[:MAX_REVIEW_CANDIDATES]
    payload = json.dumps([c.to_json() for c in top], ensure_ascii=False)
    best_confidence = max((c.text_confidence for c in top), default=None)
    image_scores = [
        c.poster.image_score for c in top
        if c.poster is not None and c.poster.image_score is not None
    ]
    best_image_score = max(image_scores) if image_scores else None
    ts = now_ms()

    async def work(session: AsyncSession) -> None:
        stmt = sqlite_insert(ScrapeReview).values(
            rating_key=rating_key, server_id=server_id, media_type=media_type,
            title=title, year=year, reason=reason, candidates_json=payload,
            best_confidence=best_confidence, best_image_score=best_image_score,
            status="pending", created_at=ts, resolved_at=None,
        )
        await session.execute(stmt.on_conflict_do_update(
            index_elements=["rating_key", "server_id"],
            set_={
                "media_type": media_type, "title": title, "year": year,
                "reason": reason, "candidates_json": payload,
                "best_confidence": best_confidence,
                "best_image_score": best_image_score,
                "status": "pending", "created_at": ts, "resolved_at": None,
            },
        ))
        await session.commit()

    await write_with_retry(
        work, session_factory=session_factory, op="manual_scrape.upsert_review",
    )


async def _set_review_status(
    server_id: str, rating_key: str, status: str, *, session_factory,
) -> bool:
    async def work(session: AsyncSession) -> bool:
        result = await session.execute(
            update(ScrapeReview)
            .where(
                ScrapeReview.rating_key == rating_key,
                ScrapeReview.server_id == server_id,
                ScrapeReview.status == "pending",
            )
            .values(status=status, resolved_at=now_ms())
        )
        await session.commit()
        return result.rowcount > 0

    return await write_with_retry(
        work, session_factory=session_factory, op=f"manual_scrape.review_{status}",
    )


async def dismiss_review(server_id: str, rating_key: str, *, session_factory) -> bool:
    """Operator says "leave this one alone": the item stays un-identified but
    drops out of the queue AND out of the batch's selection (the worker skips
    `pending` and `dismissed` alike), so a later run won't re-queue it."""
    return await _set_review_status(
        server_id, rating_key, "dismissed", session_factory=session_factory,
    )


async def resolve_review(server_id: str, rating_key: str, *, session_factory) -> bool:
    """Mark a review settled. Normally NOT called directly: `apply_candidate`
    resolves the row inside its own write transaction (ADR 0005 D6 §3). Kept
    for the paths that settle an item without going through it."""
    return await _set_review_status(
        server_id, rating_key, "resolved", session_factory=session_factory,
    )


async def load_review(
    server_id: str, rating_key: str, *, session_factory,
) -> ScrapeReview | None:
    """Read one review row on a FRESH session (same reason as `load_row`)."""
    async with session_factory() as db:
        return (await db.execute(
            select(ScrapeReview).where(
                ScrapeReview.rating_key == rating_key,
                ScrapeReview.server_id == server_id,
            ).limit(1)
        )).scalars().first()


__all__ = [
    "MediaKind", "Provider", "MatchSource", "IdFilter",
    "Candidate", "SearchResult", "ScrapeQuery", "ApplyOutcome",
    "CataloguePage", "TypeStats", "Decision",
    "search_candidates", "lookup", "apply_candidate", "unlock", "clear_ids",
    "load_row", "list_catalogue", "catalogue_stats",
    "decide", "list_reviews", "upsert_review", "dismiss_review",
    "resolve_review", "load_review", "review_candidates",
]
