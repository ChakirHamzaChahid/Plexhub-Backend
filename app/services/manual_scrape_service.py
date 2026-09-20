"""Manual scraper (TMDB + IMDb/OMDb) application service (ADR 0005 D6).

W2 scope only: `lookup` (id/URL -> a single candidate, no poster compare —
that is W3/W4's job via `search_candidates`), `apply_candidate` (the write
path), `unlock`, `clear_ids`, `load_row`. `search_candidates` (full
text-search + poster-compare candidate list), `list_catalogue`,
`catalogue_stats`, `list_reviews`/`upsert_review`/`dismiss_review` and
`decide()` are later waves (W4/W5) and are NOT implemented here — this
module is extended in place by those waves, not replaced.

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

import logging
import re
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Literal

import httpx
from sqlalchemy import and_, func, not_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import EnrichmentQueue, Media
from app.services import omdb_scrape_cache_service, scrape_cache_service, unified_group_service
from app.services.media_identity_writer import (
    build_identity_values,
    build_omdb_replace_extras,
    build_rating_values,
)
from app.services.omdb_service import OMDbData, omdb_service
from app.services.poster_match_service import PosterComparison
from app.services.tmdb_service import TMDBEnrichmentData, tmdb_service
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
        # `scrape_review` (migration 027) doesn't exist yet at this point in
        # the rollout (ADR 0005 D8/W5) — resolving it here is left to W5.

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


__all__ = [
    "MediaKind", "Provider", "MatchSource",
    "Candidate", "SearchResult", "ScrapeQuery", "ApplyOutcome",
    "lookup", "apply_candidate", "unlock", "clear_ids", "load_row",
]
