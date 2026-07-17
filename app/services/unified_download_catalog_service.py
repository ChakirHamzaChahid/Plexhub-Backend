"""Unified read over BOTH download catalogues — Xtream (`media`) and Plex
(`plex_media_item`) — for the merged "Téléchargements" admin screen (feature
"écran de téléchargement unifié", Vague W2).

The two catalogues are otherwise isolated, but both already emit a
byte-identical ``unification_id`` for id-resolved titles — Xtream
(`app.utils.unification.calculate_unification_id`) and Plex
(`app.services.plex_sync_service.calculate_plex_unification_id`) BOTH produce
``imdb://tt{id}`` (both tt-prefixed) and ``tmdb://{id}``. So cross-source dedup
is a plain merge on that string:

  - Two rows sharing an id-based key (``imdb://``/``tmdb://``) collapse into ONE
    card carrying both origins.
  - Fallback keys never collide across sources (Xtream ``title_…`` vs Plex
    ``plexsrc://…``) — a title present in both but with NO shared id stays two
    cards. That is the SAFE outcome (never a false merge on title+year alone),
    the same conservative rule both catalogues already follow internally.

Pure reads, secret-free: reuses `media_service.get_unified_list` (Xtream) and
`plex_catalog_service.list_unified` (Plex, never exposes access_token/base_uri).
Merge + sort happen in memory (this is a cold admin path, not the app API) over
a bounded window per source (`cap`) — a truncation flag is returned so the UI
can say so honestly rather than silently dropping titles.

Enqueue is NOT here: the caller routes a chosen source by its `server_id`
prefix (`is_plex_server_id`) to the existing `download_service` /
`plex_download_service` enqueue paths — no new enqueue/worker logic.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services import plex_catalog_service
from app.services.aggregation_service import canonical_title_year
from app.services.media_service import media_service

# Per-source ceiling for the in-memory merge (declared in config.py). Beyond
# this, a source's window is truncated and `truncated=True` is surfaced (never
# a silent cap). Overridable via `UNIFIED_DOWNLOAD_MERGE_CAP`.
DEFAULT_MERGE_CAP = settings.UNIFIED_DOWNLOAD_MERGE_CAP or 5000

ORIGIN_XTREAM = "xtream"
ORIGIN_PLEX = "plex"


@dataclass
class UnifiedCard:
    unification_id: str
    type: str                       # 'movie' | 'show'
    title: str
    year: int | None
    origins: list[str]              # sorted subset of ["plex", "xtream"]
    source_count: int               # total versions across BOTH origins


def _merge_card(
    merged: dict[str, UnifiedCard],
    *,
    unification_id: str,
    media_type: str,
    title: str,
    year: int | None,
    origin: str,
    source_count: int,
) -> None:
    """Fold one source's group into the merged map, keyed by unification_id."""
    card = merged.get(unification_id)
    if card is None:
        merged[unification_id] = UnifiedCard(
            unification_id=unification_id,
            type=media_type,
            title=title,
            year=year,
            origins=[origin],
            source_count=source_count,
        )
        return
    if origin not in card.origins:
        card.origins.append(origin)
    card.source_count += source_count
    # Xtream is folded first, so its title/year win as representative; only
    # fill a gap the first origin left (e.g. Plex year when Xtream had none).
    if card.year is None and year is not None:
        card.year = year


async def list_unified(
    db: AsyncSession,
    *,
    media_type: str,
    search: str | None = None,
    genre: str | None = None,
    limit: int = 24,
    offset: int = 0,
    cap: int = DEFAULT_MERGE_CAP,
) -> tuple[list[UnifiedCard], int, bool]:
    """Merged, cross-source-deduplicated browse page for *media_type*
    ('movie'|'show').

    Returns ``(page, total, truncated)`` where ``total`` is the merged group
    count within the fetched window and ``truncated`` is ``True`` if either
    source had more than ``cap`` groups (so the operator knows to narrow the
    search/genre). Sorted by title (case-insensitive) then unification_id for a
    stable, browsable order across both origins.
    """
    x_groups, x_total = await media_service.get_unified_list(
        db, media_type=media_type, limit=cap, offset=0, search=search, genre=genre,
    )
    p_items, p_total = await plex_catalog_service.list_unified(
        db, media_type, search, cap, 0, genre=genre,
    )

    merged: dict[str, UnifiedCard] = {}

    # Xtream first (its title/year become the card's representative).
    for g in x_groups:
        title, year = canonical_title_year(g.best)
        _merge_card(
            merged,
            unification_id=g.key,
            media_type=media_type,
            title=title,
            year=year,
            origin=ORIGIN_XTREAM,
            source_count=len(g.members),
        )

    for it in p_items:
        # (Plex thumb needs the per-server token, so it's never exposed anyway.)
        _merge_card(
            merged,
            unification_id=it.unification_id,
            media_type=media_type,
            title=it.title,
            year=it.year,
            origin=ORIGIN_PLEX,
            source_count=it.source_count,
        )

    cards = list(merged.values())
    for c in cards:
        c.origins.sort()
    cards.sort(key=lambda c: ((c.title or "").casefold(), c.unification_id))

    total = len(cards)
    truncated = x_total > cap or p_total > cap
    return cards[offset:offset + limit], total, truncated
