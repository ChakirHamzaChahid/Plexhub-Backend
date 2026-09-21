"""Single writer of TMDB/OMDb identity + metadata + rating onto `Media` rows
(ADR 0005 D4, manual-scraper/poster-match refacto).

Pure functions only: each `build_*` helper returns a plain `dict[str, Any]`
meant for `update(Media).where(...).values(**d)` — no session, no network,
no side effects. `enrichment_worker`, the future `manual_scrape_service`
(W2) and the future batch worker (W5) all funnel through this ONE module so
there is never a second, subtly-different formula for what a TMDB/OMDb
match writes onto a row.

Two modes:

- ``"fill"`` — reproduces `enrichment_worker._apply_enrichment_results`'s
  TMDB-match branch **exactly** (verified byte-for-byte against HEAD
  `4e5b04f`, `enrichment_worker.py:504-554` + `:593-618`): a handful of
  columns are overwritten *whenever TMDB provided a value* (summary,
  genres, resolved_thumb_url, resolved_art_url, scraped_rating, year,
  cast), and a larger set of "rich" NFO-shaped columns are filled in via
  SQL `COALESCE` (never clobbers a richer NFO-imported value, nor the
  adult-tagging `content_rating="XXX"`). This is knowingly NOT an ideal
  fill-missing scheme — it is whatever the worker already did, extracted
  verbatim (ADR 0005, fact F3).

- ``"replace"`` — for a manual correction where the OLD identity was wrong
  (a mismatched-poster fix, W2+): nothing from the wrong match may survive.
  ``XTREAM_ORIGIN_COLS`` prefer a fresh TMDB value, fall back to OMDb, and
  are otherwise left untouched (never NULLed — these columns are also
  populated by the Xtream sync and re-populate themselves on the next
  content_hash change anyway). ``content_rating`` gets its own rule
  (adult-tag override first, then TMDB cert, else untouched — no OMDb
  fallback). ``resolved_thumb_url``/``resolved_art_url`` always get a
  value: the fresh TMDB image, or an explicit reset to the raw Xtream
  ``thumb_url``/``art_url`` (never left pointing at the wrong film's
  poster). ``PROVIDER_ONLY_COLS`` are direct assignments — new value or
  NULL, no COALESCE — since a wrong match may have written wrong values
  there that a NULL-out must be able to clear.

`build_rating_values` is the ratings/`display_rating` counterpart, used
alongside `build_identity_values` (identity + metadata) by every caller.
"""
from __future__ import annotations

import re
from typing import Any, Literal

from sqlalchemy import case, func
from sqlalchemy.sql.elements import ColumnElement

from app.config import settings
from app.models.database import Media
from app.services.aggregation_service import IMPLAUSIBLE_DURATION_MS
from app.services.omdb_service import OMDbData
from app.services.tmdb_service import TMDBEnrichmentData
from app.utils.rating_blend import blend_display_rating_case, blend_rating

WriteMode = Literal["fill", "replace"]

# --- mode == "replace" only -------------------------------------------------
# Columns that also come from the Xtream provider itself (re-populated on
# the next sync content_hash change) — a wrong-match correction prefers
# fresh TMDB, falls back to OMDb, but never blanks them out.
XTREAM_ORIGIN_COLS = ("summary", "genres", "cast", "year", "content_rating")
# Columns that ONLY ever come from TMDB/OMDb enrichment (never from Xtream
# nor from the manual operator directly) — a wrong-match correction may
# legitimately NULL these out (new value or NULL, no COALESCE).
PROVIDER_ONLY_COLS = (
    "original_title", "tagline", "premiered", "status", "studio", "country",
    "tvdb_id", "wikidata_id", "tmdb_rating", "tmdb_votes", "cast_json",
    "youtube_trailer", "scraped_rating",
)


def _parse_omdb_year(raw: str | None) -> int | None:
    """OMDb `Year`: "1984" (movie), "2015-2019" / "2015-" (series). Take the
    leading 4-digit year; unparseable -> None. Mirrors
    `enrichment_worker._parse_omdb_year`."""
    if not raw:
        return None
    m = re.match(r"^(\d{4})", raw)
    return int(m.group(1)) if m else None


def _apply_episode_runtime(values: dict[str, Any], data: TMDBEnrichmentData) -> None:
    """Write TMDB's typical episode length onto a SHOW row's `duration`.

    Implicitly TV-only: `episode_runtime_ms` is parsed from `episode_run_time`,
    which TMDB only returns for series.

    Deliberately NOT a COALESCE fill-missing. 14 931 show rows already carry a
    duration published by the Xtream panel, and that value IS the unreliable
    one — a COALESCE would preserve every single one of them and change
    nothing. Only a missing or implausible value is replaced, so a believable
    panel figure (more specific than a series-wide average) still wins.
    """
    if not data.episode_runtime_ms:
        return
    values["duration"] = case(
        (Media.duration.is_(None), data.episode_runtime_ms),
        (Media.duration < IMPLAUSIBLE_DURATION_MS, data.episode_runtime_ms),
        else_=Media.duration,
    )


def build_identity_values(
    data: TMDBEnrichmentData,
    *,
    confidence: float | None,
    omdb: OMDbData | None,
    mode: WriteMode = "fill",
    is_adult: bool = False,
) -> dict[str, Any]:
    """Build the `update(Media).values(**d)` dict for identity + rich
    metadata from a TMDB detail fetch (`data`), optionally cross-checked
    against an OMDb result (`omdb`, only consulted in `"replace"` mode —
    `"fill"` mode ignores it, matching the worker's current call site,
    which never passes OMDb data into this branch).

    Identity fields (`tmdb_id`, `imdb_id`, `unification_id`,
    `history_group_key`, `tmdb_match_confidence`) are written the same way
    in both modes. Deliberately NEVER written here (ADR 0005 D12/D4):
    `updated_at`, `match_locked`, `match_source`, `title` — those are the
    caller's responsibility (the manual-scrape apply path, W2)."""
    tmdb_id = data.tmdb_id
    imdb_id = data.imdb_id
    new_unif = f"imdb://{imdb_id}" if imdb_id else f"tmdb://{tmdb_id}"

    values: dict[str, Any] = {
        "tmdb_id": str(tmdb_id),
        "imdb_id": imdb_id,
        "unification_id": new_unif,
        "history_group_key": new_unif,
        "tmdb_match_confidence": confidence,
    }

    if mode == "fill":
        if data.overview:
            values["summary"] = data.overview
        if data.genres:
            values["genres"] = data.genres
        if data.poster_url:
            values["resolved_thumb_url"] = data.poster_url
        if data.backdrop_url:
            values["resolved_art_url"] = data.backdrop_url
        if data.vote_average:
            # scraped_rating stays = raw TMDB vote_average (durable record).
            # display_rating is handled separately by `build_rating_values`.
            values["scraped_rating"] = data.vote_average
        if data.year:
            values["year"] = data.year
        if data.cast:
            values["cast"] = data.cast

        rich = (
            ("content_rating", data.content_rating),
            ("original_title", data.original_title),
            ("tagline", data.tagline),
            ("premiered", data.premiered),
            ("status", data.status),
            ("studio", data.studio),
            ("country", data.country),
            ("tvdb_id", data.tvdb_id),
            ("wikidata_id", data.wikidata_id),
            ("tmdb_rating", data.tmdb_rating),
            ("tmdb_votes", data.tmdb_votes),
            ("cast_json", data.cast_json),
            ("youtube_trailer", data.youtube_trailer),
        )
        for col, value in rich:
            if value is not None:
                values[col] = func.coalesce(getattr(Media, col), value)

        _apply_episode_runtime(values, data)
        return values

    # --- mode == "replace" ---
    omdb_year = _parse_omdb_year(omdb.year) if omdb is not None else None
    xtream_repl: dict[str, tuple[Any, Any]] = {
        "summary": (data.overview, omdb.plot if omdb is not None else None),
        "genres": (data.genres, omdb.genre if omdb is not None else None),
        "cast": (data.cast, omdb.actors if omdb is not None else None),
        "year": (data.year, omdb_year),
    }
    for col, (tmdb_value, omdb_value) in xtream_repl.items():
        chosen = tmdb_value if tmdb_value is not None else omdb_value
        if chosen is not None:
            values[col] = chosen
        # else: leave the column untouched (never NULLed, F3/XTREAM_ORIGIN_COLS).

    if is_adult:
        values["content_rating"] = settings.ADULT_CONTENT_RATING
    elif data.content_rating is not None:
        values["content_rating"] = data.content_rating
    # else: leave content_rating untouched.

    # Always assign — either the fresh TMDB image, or an explicit reset to
    # the raw Xtream image (never left pointing at the WRONG film's poster).
    values["resolved_thumb_url"] = data.poster_url if data.poster_url else Media.thumb_url
    values["resolved_art_url"] = data.backdrop_url if data.backdrop_url else Media.art_url

    provider_only = {
        "original_title": data.original_title,
        "tagline": data.tagline,
        "premiered": data.premiered,
        "status": data.status,
        "studio": data.studio,
        "country": data.country,
        "tvdb_id": data.tvdb_id,
        "wikidata_id": data.wikidata_id,
        "tmdb_rating": data.tmdb_rating,
        "tmdb_votes": data.tmdb_votes,
        "cast_json": data.cast_json,
        "youtube_trailer": data.youtube_trailer,
        "scraped_rating": data.vote_average,
    }
    for col, value in provider_only.items():
        values[col] = value  # new value or NULL — direct assignment, no COALESCE.

    return values


def build_omdb_replace_extras(
    omdb: OMDbData | None, *, is_adult: bool = False,
) -> dict[str, Any]:
    """Metadata slice for a `"replace"` correction resolved through OMDb ALONE
    (no TMDB match at all — ADR 0005 D6 "identité imdb seule").

    Same contract as `build_identity_values(..., mode="replace")` minus the
    identity keys, which that path computes itself: nothing from the wrong
    match may survive. `PROVIDER_ONLY_COLS` are NULLed (OMDb supplies none of
    them, and the WRONG film's values are sitting in them — including
    `tmdb_rating`/`scraped_rating`, which `recompute_display_rating_stmt()`
    would otherwise blend back into `display_rating` on the next enrichment
    pass). The images are reset to the raw Xtream ones rather than left
    pointing at the wrong film's poster — the poster being wrong is usually
    the very reason the operator is correcting this row. `XTREAM_ORIGIN_COLS`
    take what OMDb has and are otherwise left untouched (never NULLed)."""
    values: dict[str, Any] = {}

    if omdb is not None:
        xtream_repl = {
            "summary": omdb.plot,
            "genres": omdb.genre,
            "cast": omdb.actors,
            "year": _parse_omdb_year(omdb.year),
        }
        for col, value in xtream_repl.items():
            if value is not None:
                values[col] = value

    if is_adult:
        values["content_rating"] = settings.ADULT_CONTENT_RATING
    # else: leave content_rating untouched (OMDb's `Rated` is not mapped).

    values["resolved_thumb_url"] = Media.thumb_url
    values["resolved_art_url"] = Media.art_url

    for col in PROVIDER_ONLY_COLS:
        values[col] = None

    return values


def build_rating_values(
    *,
    omdb_imdb_rating: float | None,
    omdb_imdb_votes: int | None,
    tmdb_rating: float | None,
    mode: WriteMode = "fill",
) -> dict[str, Any]:
    """Build the `imdb_rating`/`imdb_votes`/`display_rating` slice of the
    `update(Media).values(**d)` dict.

    `"fill"` reproduces `enrichment_worker._apply_enrichment_results`'s
    rating block exactly (`enrichment_worker.py:593-618`): COALESCE
    fill-missing on `imdb_rating`/`imdb_votes`, and `display_rating`
    computed via `blend_display_rating_case` from the POST-write columns
    (COALESCE of the pre-update value with the value written this pass) so
    it stays reproducible in SQL / self-healing. The caller decides WHETHER
    to call this at all (the worker's own condition,
    `enrichment_data is not None or have_omdb` — see ADR 0005 D4): when
    neither a fresh OMDb rating nor a fresh TMDB rating is in hand, this
    function is simply not invoked and nothing rating-related is written.

    `"replace"` computes `display_rating` in Python (`blend_rating`) rather
    than SQL, since a correction overwrites `imdb_rating`/`imdb_votes`
    outright (new OMDb value or NULL) rather than coalescing — there is no
    "pre-update value to preserve" logic left to express in SQL. When the
    blend yields `None` (both ratings absent), `display_rating` is reset to
    `0.0` (ADR 0005 W0-review follow-up (a) — NOT the old `Media.
    display_rating` no-op reference this used to fall back to, and NOT a
    literal SQL NULL either: the column is `NOT NULL`, and `0.0` is the
    SAME "no rating" sentinel `blend_rating`'s own `<= 0` rule already
    treats as absent everywhere else). A correction exists precisely
    because the OLD identity was wrong, so its `display_rating` is the
    WRONG film's rating — carrying it forward would silently keep
    displaying a stale, mismatched score instead of an honest "no rating
    yet" until the next enrichment pass finds one."""
    if mode == "fill":
        values: dict[str, Any] = {}
        if omdb_imdb_rating is not None:
            values["imdb_rating"] = func.coalesce(Media.imdb_rating, omdb_imdb_rating)
        if omdb_imdb_votes is not None:
            values["imdb_votes"] = func.coalesce(Media.imdb_votes, omdb_imdb_votes)

        imdb_operand: ColumnElement | float | None = (
            func.coalesce(Media.imdb_rating, omdb_imdb_rating)
            if omdb_imdb_rating is not None else Media.imdb_rating
        )
        tmdb_operand: ColumnElement | float | None = (
            func.coalesce(Media.tmdb_rating, tmdb_rating)
            if tmdb_rating is not None else Media.tmdb_rating
        )
        values["display_rating"] = blend_display_rating_case(
            imdb_operand, tmdb_operand, Media.display_rating,
        )
        return values

    # --- mode == "replace" ---
    values = {
        "imdb_rating": omdb_imdb_rating,
        "imdb_votes": omdb_imdb_votes,
    }
    blended = blend_rating(omdb_imdb_rating, tmdb_rating)
    values["display_rating"] = blended if blended is not None else 0.0
    return values
