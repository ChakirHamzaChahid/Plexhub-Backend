"""PH-DL-0X: Plex-sourced download enqueue + worker-time URL resolution
(feature "Télécharger Plex", `docs/10-prd-media-download.md`, ticket C5).

Mirrors `app.services.download_service`'s enqueue shape but reads from the
Plex-only tables (`plex_server`/`plex_media_item`) instead of `media`/
`XtreamAccount` — the two catalogues never cross. This module has exactly
two entry points:

  - `enqueue_plex_selection`: turns an already-STRUCTURED operator selection
    (server_id/rating_key tuples — router C6 is responsible for parsing raw
    form strings into these) into 1..N persisted `DownloadJob` rows, reusing
    `download_service.compute_dest_path`/`find_non_terminal_job`/
    `EnqueueResult` so the destination-path convention and idempotent-enqueue
    dedup stay identical across both sources. Every field written to
    `DownloadJob`/`DownloadBatch` is derived from `plex_media_item` — never
    from client input (same F-007 spirit as the Xtream path, even though
    path confinement itself is proven later, at write time, by
    `download_service.resolve_confined`).

  - `resolve_job_url`: called from `download_worker._run_job` at transfer
    time (parallel to `stream_service.build_stream_url` for Xtream jobs) to
    re-derive the Plex direct-download URL for a `plex_*`-sourced job. The
    URL embeds `X-Plex-Token` — a secret — and this function, like its
    Xtream counterpart, NEVER logs or persists it; only the caller's
    `dest`/`job_id`/`title` may appear in log lines.

Dispatch lives in `download_worker._run_job`::

    if is_plex_server_id(job.server_id):
        url = await plex_download_service.resolve_job_url(session_factory, job)
    else:
        ...  # existing Xtream path (build_stream_url / _load_account), unchanged
"""
from __future__ import annotations

import logging
import uuid
from typing import Callable, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.database import DownloadBatch, DownloadJob, PlexMediaItem, PlexServer
from app.services.download_service import (
    NON_TERMINAL_STATES,
    EnqueueResult,
    compute_dest_path,
    find_non_terminal_job,
)
from app.utils.db_retry import run_with_retry
from app.utils.server_id import is_plex_server_id, parse_plex_server_id
from app.utils.time import now_ms

logger = logging.getLogger("plexhub.download.plex")

_DEFAULT_EXT = "mkv"

# Referenced for parity with download_service's dedup contract (a job in one
# of these states blocks a re-enqueue of the same (server_id, rating_key));
# not used directly here (find_non_terminal_job already encapsulates it), but
# kept as an explicit import so a future change to the tuple can't silently
# drift between the two enqueue paths.
_ = NON_TERMINAL_STATES


# --- Shared helpers ------------------------------------------------------

async def _load_plex_item(
    db: AsyncSession, server_id: str, rating_key: str, *, expected_type: Optional[str] = None,
) -> Optional[PlexMediaItem]:
    item = await db.get(PlexMediaItem, (server_id, rating_key))
    if item is None:
        return None
    if expected_type is not None and item.type != expected_type:
        return None
    return item


def _ext_for(item: PlexMediaItem) -> str:
    return (item.container or _DEFAULT_EXT).lstrip(".")


def _build_episode_job(
    batch_id: str, ep: PlexMediaItem, show_title: str, unification_id: Optional[str],
) -> DownloadJob:
    dest_path = compute_dest_path(
        media_type="episode", title=show_title, year=None,
        season=ep.parent_index, episode=ep.index, ext=_ext_for(ep),
    )
    return DownloadJob(
        id=uuid.uuid4().hex,
        batch_id=batch_id,
        server_id=ep.server_id,
        rating_key=ep.rating_key,
        media_type="episode",
        unification_id=unification_id or None,
        title=ep.title or show_title,
        season=ep.parent_index,
        episode=ep.index,
        dest_path=dest_path,
        state="queued",
        bytes_total=None,
        bytes_done=0,
        attempts=0,
        created_at=now_ms(),
        updated_at=now_ms(),
    )


async def _persist_batch_and_jobs(
    db: AsyncSession,
    *,
    media_type: str,
    unification_id: str,
    scope: str,
    server_id: str,
    rating_key: str,
    batch_title: str,
    episodes: list[PlexMediaItem],
    show_title_for: Callable[[PlexMediaItem], Optional[str]],
    op: str,
) -> EnqueueResult:
    """Create one `DownloadBatch` + one `DownloadJob` per episode (deduped
    against any existing non-terminal job for the same (server_id,
    rating_key)). Shared by the series_all/seasons/episodes scopes — they
    differ only in how `episodes`/`batch_title`/`show_title_for` were
    resolved by the caller."""
    batch = DownloadBatch(
        id=uuid.uuid4().hex,
        media_type=media_type or "show",
        unification_id=unification_id or None,
        title=batch_title,
        server_id=server_id,
        rating_key=rating_key,
        scope=scope,
        total_jobs=0,
        created_at=now_ms(),
    )
    db.add(batch)

    jobs: list[DownloadJob] = []
    new_job_count = 0
    for ep in episodes:
        existing = await find_non_terminal_job(db, ep.server_id, ep.rating_key)
        if existing is not None:
            jobs.append(existing)
            continue

        show_title = show_title_for(ep) or "Unknown"
        job = _build_episode_job(batch.id, ep, show_title, unification_id)
        db.add(job)
        jobs.append(job)
        new_job_count += 1

    # total_jobs counts only jobs actually linked to THIS batch — a reused
    # (deduped) job may still point at an earlier batch_id (mirrors
    # download_service.enqueue_selection's series_all/series_seasons).
    batch.total_jobs = new_job_count

    async def _commit() -> None:
        await db.commit()

    await run_with_retry(_commit, op=op)
    logger.info(
        "Plex download enqueued: batch=%s title=%r jobs=%d (new=%d)",
        batch.id, batch.title, len(jobs), new_job_count,
    )
    return EnqueueResult(jobs=jobs, batch_id=batch.id, error=None)


# --- Scope handlers --------------------------------------------------------

async def _enqueue_movie(
    db: AsyncSession, *, unification_id: str, server_id: Optional[str], rating_key: Optional[str],
) -> EnqueueResult:
    if not server_id or not rating_key:
        return EnqueueResult(jobs=[], batch_id=None, error="sélection invalide")

    item = await _load_plex_item(db, server_id, rating_key, expected_type="movie")
    if item is None:
        return EnqueueResult(jobs=[], batch_id=None, error="Média Plex introuvable")

    existing = await find_non_terminal_job(db, server_id, rating_key)
    if existing is not None:
        return EnqueueResult(jobs=[existing], batch_id=existing.batch_id, error=None)

    dest_path = compute_dest_path(
        media_type="movie", title=item.title, year=item.year,
        season=None, episode=None, ext=_ext_for(item),
    )
    job = DownloadJob(
        id=uuid.uuid4().hex,
        batch_id=None,
        server_id=server_id,
        rating_key=rating_key,
        media_type="movie",
        unification_id=unification_id or None,
        title=item.title,
        season=None,
        episode=None,
        dest_path=dest_path,
        state="queued",
        bytes_total=None,
        bytes_done=0,
        attempts=0,
        created_at=now_ms(),
        updated_at=now_ms(),
    )
    db.add(job)

    async def _commit() -> None:
        await db.commit()

    await run_with_retry(_commit, op="enqueue_plex_movie")
    logger.info("Plex download enqueued: movie job=%s title=%r", job.id, job.title)
    return EnqueueResult(jobs=[job], batch_id=None, error=None)


async def _enqueue_series_all(
    db: AsyncSession, *, media_type: str, unification_id: str,
    server_id: Optional[str], rating_key: Optional[str],
) -> EnqueueResult:
    if not server_id or not rating_key:
        return EnqueueResult(jobs=[], batch_id=None, error="sélection invalide")

    show = await _load_plex_item(db, server_id, rating_key, expected_type="show")
    if show is None:
        return EnqueueResult(jobs=[], batch_id=None, error="Série Plex introuvable")

    episodes = list((await db.execute(
        select(PlexMediaItem).where(
            PlexMediaItem.server_id == server_id,
            PlexMediaItem.grandparent_rating_key == rating_key,
            PlexMediaItem.type == "episode",
        )
    )).scalars().all())
    if not episodes:
        return EnqueueResult(jobs=[], batch_id=None, error="aucun épisode disponible")

    show_title = show.title

    return await _persist_batch_and_jobs(
        db, media_type=media_type, unification_id=unification_id,
        scope="series_all", server_id=server_id, rating_key=rating_key,
        batch_title=show_title, episodes=episodes,
        show_title_for=lambda _ep: show_title,
        op="enqueue_plex_series_all",
    )


async def _enqueue_seasons(
    db: AsyncSession, *, media_type: str, unification_id: str,
    season_picks: Optional[list[tuple[int, str, str]]],
) -> EnqueueResult:
    if not season_picks:
        return EnqueueResult(jobs=[], batch_id=None, error="aucune sélection")

    show_title_cache: dict[tuple[str, str], Optional[str]] = {}
    episodes: list[PlexMediaItem] = []
    seen_ep_pk: set[tuple[str, str]] = set()

    for season, srv, show_rk in season_picks:
        cache_key = (srv, show_rk)
        if cache_key not in show_title_cache:
            show = await _load_plex_item(db, srv, show_rk, expected_type="show")
            show_title_cache[cache_key] = show.title if show is not None else None

        rows = list((await db.execute(
            select(PlexMediaItem).where(
                PlexMediaItem.server_id == srv,
                PlexMediaItem.grandparent_rating_key == show_rk,
                PlexMediaItem.type == "episode",
                PlexMediaItem.parent_index == season,
            )
        )).scalars().all())
        for ep in rows:
            pk = (ep.server_id, ep.rating_key)
            if pk in seen_ep_pk:
                continue
            seen_ep_pk.add(pk)
            episodes.append(ep)

    if not episodes:
        return EnqueueResult(
            jobs=[], batch_id=None, error="aucun épisode pour les saisons sélectionnées",
        )

    # Batch server_id/rating_key: the first pick, for back-nav only.
    _first_season, first_srv, first_show_rk = season_picks[0]
    batch_title = show_title_cache.get((first_srv, first_show_rk)) or "Unknown"

    def _show_title_for(ep: PlexMediaItem) -> Optional[str]:
        return show_title_cache.get((ep.server_id, ep.grandparent_rating_key))

    return await _persist_batch_and_jobs(
        db, media_type=media_type, unification_id=unification_id,
        scope="seasons", server_id=first_srv, rating_key=first_show_rk,
        batch_title=batch_title, episodes=episodes,
        show_title_for=_show_title_for,
        op="enqueue_plex_seasons",
    )


async def _enqueue_episodes(
    db: AsyncSession, *, media_type: str, unification_id: str,
    episode_picks: Optional[list[tuple[str, str]]],
) -> EnqueueResult:
    if not episode_picks:
        return EnqueueResult(jobs=[], batch_id=None, error="aucune sélection")

    show_title_cache: dict[tuple[str, str], Optional[str]] = {}
    episodes: list[PlexMediaItem] = []
    seen_ep_pk: set[tuple[str, str]] = set()

    for srv, ep_rk in episode_picks:
        pk = (srv, ep_rk)
        if pk in seen_ep_pk:
            continue
        ep = await _load_plex_item(db, srv, ep_rk, expected_type="episode")
        if ep is None:
            continue
        seen_ep_pk.add(pk)
        episodes.append(ep)

        show_key = (ep.server_id, ep.grandparent_rating_key)
        if show_key not in show_title_cache and ep.grandparent_rating_key:
            show = await _load_plex_item(
                db, ep.server_id, ep.grandparent_rating_key, expected_type="show",
            )
            show_title_cache[show_key] = show.title if show is not None else None

    if not episodes:
        return EnqueueResult(jobs=[], batch_id=None, error="aucun épisode disponible")

    # Batch server_id/rating_key: the first pick, for back-nav only.
    first_srv, first_ep_rk = episode_picks[0]

    def _show_title_for(ep: PlexMediaItem) -> Optional[str]:
        return show_title_cache.get((ep.server_id, ep.grandparent_rating_key))

    batch_title = _show_title_for(episodes[0]) or "Unknown"

    return await _persist_batch_and_jobs(
        db, media_type=media_type, unification_id=unification_id,
        scope="episodes", server_id=first_srv, rating_key=first_ep_rk,
        batch_title=batch_title, episodes=episodes,
        show_title_for=_show_title_for,
        op="enqueue_plex_episodes",
    )


# --- Public entry point (spec §5.4, ticket C5) ------------------------------

async def enqueue_plex_selection(
    db: AsyncSession,
    *,
    media_type: str,            # 'movie' | 'show'
    unification_id: str,
    scope: str,                 # 'movie' | 'series_all' | 'seasons' | 'episodes'
    server_id: Optional[str] = None,     # movie / series_all
    rating_key: Optional[str] = None,    # movie / series_all (movie item rk, or show rk)
    season_picks: Optional[list[tuple[int, str, str]]] = None,  # (season, server_id, show_rating_key)
    episode_picks: Optional[list[tuple[str, str]]] = None,      # (server_id, episode_rating_key)
) -> EnqueueResult:
    """Resolve an ALREADY-STRUCTURED operator selection into 1..N persisted
    `DownloadJob` rows sourced from `plex_media_item` (never `media`).

    Router C6 owns parsing raw HTML-form strings into the structured
    `server_id`/`rating_key`/`season_picks`/`episode_picks` this function
    takes — this module never touches a request body.

    Every destination field (`title`/`year`/`season`/`episode`/`ext`) is
    read straight off `plex_media_item` — never client-supplied. Never
    raises for a "normal" failure mode (feature disabled, missing
    server/item, empty selection, no episodes) — those come back as
    `EnqueueResult(jobs=[], batch_id=None, error=...)`, mirroring
    `download_service.enqueue_selection`'s contract exactly.
    """
    if not settings.DOWNLOAD_DIR:
        return EnqueueResult(jobs=[], batch_id=None, error="DOWNLOAD_DIR n'est pas défini")

    if scope == "movie":
        return await _enqueue_movie(
            db, unification_id=unification_id, server_id=server_id, rating_key=rating_key,
        )

    if scope == "series_all":
        return await _enqueue_series_all(
            db, media_type=media_type, unification_id=unification_id,
            server_id=server_id, rating_key=rating_key,
        )

    if scope == "seasons":
        return await _enqueue_seasons(
            db, media_type=media_type, unification_id=unification_id, season_picks=season_picks,
        )

    if scope == "episodes":
        return await _enqueue_episodes(
            db, media_type=media_type, unification_id=unification_id, episode_picks=episode_picks,
        )

    return EnqueueResult(jobs=[], batch_id=None, error=f"scope inconnu: {scope!r}")


# --- Worker-time URL resolution (ticket C5) ---------------------------------

async def resolve_job_url(session_factory, job) -> Optional[str]:
    """Re-derive the Plex direct-download URL for a job at worker time.

    Loads `PlexServer` (`base_uri` + decrypted `access_token`) +
    `PlexMediaItem` (`part_key`) via a fresh session opened from
    `session_factory` (same pattern as `download_worker._load_account`).
    Returns `None` if the server/item is missing, or if `base_uri`,
    `access_token`, or `part_key` is empty.

    The returned URL contains the token — the caller MUST NOT log/persist
    it (only `job_id`/`title`/`dest` may appear in log lines, same secrets
    invariant as the Xtream stream URL).
    """
    if not is_plex_server_id(job.server_id):
        return None
    client_identifier = parse_plex_server_id(job.server_id)
    if not client_identifier:
        return None

    async def _do() -> tuple[Optional[PlexServer], Optional[PlexMediaItem]]:
        async with session_factory() as db:
            server = await db.get(PlexServer, client_identifier)
            item = await db.get(PlexMediaItem, (job.server_id, job.rating_key))
            return server, item

    server, item = await run_with_retry(_do, op="resolve_plex_job_url")

    if server is None or item is None:
        return None
    if not server.base_uri or not server.access_token or not item.part_key:
        return None

    base = server.base_uri.rstrip("/")
    # Plex Part.key is always "/library/parts/..." today; normalize defensively
    # so a future relative part_key can't produce a malformed URL.
    part_key = item.part_key if item.part_key.startswith("/") else f"/{item.part_key}"
    return f"{base}{part_key}?download=1&X-Plex-Token={server.access_token}"
