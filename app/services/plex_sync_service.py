"""PH-PLEX-03: catalogue sync orchestration for the Plex download source
(docs/10-prd-media-download.md — feature "Télécharger Plex").

Mirrors the master-only / mark-and-sweep conventions already used by
`app.workers.download_worker` and `app.services.unified_group_service`:
every DB write goes through `run_with_retry` on a FRESH session per attempt
(never a long-lived transaction spanning network calls), and the singleton
`plex_sync_status` row is claimed with a conditional
`UPDATE ... WHERE id=1 AND state='idle'` so two callers racing to start a
sync can't both win.

Secrets invariant: `PlexResourceDTO.access_token` / the account token are
only ever handed to `plex_api_service` (header-only, see its module
docstring) — never persisted anywhere except the encrypted
`PlexServer.access_token` column, never logged, never embedded in
`PlexSyncStatus.error` / `PlexServer.last_sync_error` (those carry only
`_safe_error()`-bounded, secret-free messages).

Identity rule (Android contract, house-law piège 15's sibling for Plex):
`calculate_plex_unification_id` NEVER falls back to a title+year key — an
item with neither an `imdb://` nor a `tmdb://` guid gets a per-source key
(`plexsrc://{server_id}/{rating_key}`) that can never accidentally merge two
different titles.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_upsert

from app.config import settings
from app.models.database import PlexMediaItem, PlexServer, PlexSyncStatus
from app.services.plex_api_service import (
    PlexConnectionDTO,
    PlexResourceDTO,
    best_media,
    parse_guids,
    plex_api_service,
)
from app.utils.db_retry import run_with_retry
from app.utils.server_id import build_plex_server_id, parse_plex_server_id
from app.utils.time import now_ms

logger = logging.getLogger("plexhub.plex_sync")

# Bounds how many per-show fetches (seasons -> episodes) run concurrently
# against one PMS per sync run, across all reachable servers.
_SHOW_CONCURRENCY = 4
# Same bound applied to the per-resource connection-probing fan-out.
_PROBE_CONCURRENCY = 4
# `PlexSyncReport.errors` / `plex_sync_status.error` never grow unbounded.
_MAX_ERRORS = 20
_UPSERT_CHUNK = 200


@dataclass
class PlexSyncReport:
    status: str = "ok"  # "ok" | "disabled" | "already_running" | "error"
    servers_total: int = 0
    servers_reachable: int = 0
    movies: int = 0
    shows: int = 0
    episodes: int = 0
    errors: list[str] = field(default_factory=list)
    duration_s: float = 0.0


# --- Identity ----------------------------------------------------------------


def calculate_plex_unification_id(
    imdb_id: Optional[str],
    tmdb_id: Optional[str],
    server_id: str,
    rating_key: str,
) -> str:
    """Priority: imdb > tmdb > per-source fallback.

    Unlike `app.utils.unification.calculate_unification_id`, there is
    deliberately NO title+year fallback branch: two unrelated Plex items
    with the same title/year on different servers must NEVER converge into
    the same unification group by accident (Android rule). Absent any guid,
    each item gets its own unique, per-source key.
    """
    if imdb_id:
        normalized = imdb_id if imdb_id.startswith("tt") else f"tt{imdb_id}"
        return f"imdb://{normalized}"
    if tmdb_id:
        return f"tmdb://{tmdb_id}"
    return f"plexsrc://{server_id}/{rating_key}"


# --- Sync status claim/release -----------------------------------------------


async def _ensure_status_row(db) -> None:
    stmt = (
        sqlite_upsert(PlexSyncStatus)
        .values(id=1, state="idle", started_at=None, finished_at=None, error=None)
        .on_conflict_do_nothing(index_elements=["id"])
    )
    await db.execute(stmt)


async def _claim_sync(session_factory) -> bool:
    """Conditional `UPDATE ... WHERE id=1 AND state='idle'`. Returns `True`
    only if THIS call won the claim (rowcount == 1)."""

    async def _do() -> bool:
        async with session_factory() as db:
            await _ensure_status_row(db)
            result = await db.execute(
                update(PlexSyncStatus)
                .where(PlexSyncStatus.id == 1, PlexSyncStatus.state == "idle")
                .values(state="running", started_at=now_ms(), finished_at=None, error=None)
            )
            await db.commit()
            return result.rowcount == 1

    return await run_with_retry(_do, op="plex_claim_sync")


async def _release_sync(session_factory, error: Optional[str] = None) -> None:
    async def _do() -> None:
        async with session_factory() as db:
            await _ensure_status_row(db)
            await db.execute(
                update(PlexSyncStatus)
                .where(PlexSyncStatus.id == 1)
                .values(state="idle", finished_at=now_ms(), error=error)
            )
            await db.commit()

    await run_with_retry(_do, op="plex_release_sync")


async def reap_sync_status(session_factory) -> None:
    """Boot-time (master only): a status left `running` belonged to a
    previous process instance that is definitely dead — reap it to `idle`."""

    async def _do() -> None:
        async with session_factory() as db:
            await _ensure_status_row(db)
            await db.execute(
                update(PlexSyncStatus)
                .where(PlexSyncStatus.id == 1, PlexSyncStatus.state == "running")
                .values(
                    state="idle",
                    finished_at=now_ms(),
                    error="reaped at boot (stale running state)",
                )
            )
            await db.commit()

    await run_with_retry(_do, op="plex_reap_sync_status")


# --- Small pure helpers --------------------------------------------------


def _safe_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _epoch_s_to_ms(value: Any) -> Optional[int]:
    v = _safe_int(value)
    return v * 1000 if v is not None else None


def _safe_error(exc: BaseException) -> str:
    """Bound an exception to a short, secret-free message for
    `plex_sync_status.error` / `plex_server.last_sync_error`.

    Safe by construction: every exception raised by `plex_api_service` is a
    `PlexApiError` whose message this codebase built (operation name +
    error class/status — see its docstring), never the raw httpx exception
    text (which can embed a request URL, though never a token — tokens are
    header-only). Any other exception type is bounded defensively too.
    """
    message = str(exc).strip() or exc.__class__.__name__
    return message[:200]


def _probe_order(resource: PlexResourceDTO) -> list[PlexConnectionDTO]:
    """Owned: local -> public -> relay. Shared: public -> local -> relay."""
    local = [c for c in resource.connections if c.local and not c.relay]
    relay = [c for c in resource.connections if c.relay]
    public = [c for c in resource.connections if not c.local and not c.relay]
    if resource.owned:
        return local + public + relay
    return public + local + relay


# --- Row mapping (PlexMediaItem) ---------------------------------------------


def _map_movie(item: dict, server_id: str, synced_at: int) -> dict:
    rating_key = str(item.get("ratingKey"))
    guids = parse_guids(item)
    media = best_media(item) or {}
    return {
        "server_id": server_id,
        "rating_key": rating_key,
        "type": "movie",
        "title": item.get("title") or "Unknown",
        "year": _safe_int(item.get("year")),
        "parent_rating_key": None,
        "grandparent_rating_key": None,
        "parent_index": None,
        "index": None,
        "imdb_id": guids["imdb_id"],
        "tmdb_id": guids["tmdb_id"],
        "tvdb_id": guids["tvdb_id"],
        "unification_id": calculate_plex_unification_id(
            guids["imdb_id"], guids["tmdb_id"], server_id, rating_key
        ),
        "thumb_url": item.get("thumb"),
        "added_at": _epoch_s_to_ms(item.get("addedAt")),
        "height": media.get("height"),
        "width": media.get("width"),
        "video_codec": media.get("video_codec"),
        "audio_codec": media.get("audio_codec"),
        "container": media.get("container"),
        "bitrate": media.get("bitrate"),
        "part_key": media.get("part_key"),
        "part_size": media.get("part_size"),
        "duration_ms": _safe_int(item.get("duration")),
        "synced_at": synced_at,
    }


def _map_show(item: dict, server_id: str, synced_at: int) -> dict:
    rating_key = str(item.get("ratingKey"))
    guids = parse_guids(item)
    return {
        "server_id": server_id,
        "rating_key": rating_key,
        "type": "show",
        "title": item.get("title") or "Unknown",
        "year": _safe_int(item.get("year")),
        "parent_rating_key": None,
        "grandparent_rating_key": None,
        "parent_index": None,
        "index": None,
        "imdb_id": guids["imdb_id"],
        "tmdb_id": guids["tmdb_id"],
        "tvdb_id": guids["tvdb_id"],
        "unification_id": calculate_plex_unification_id(
            guids["imdb_id"], guids["tmdb_id"], server_id, rating_key
        ),
        "thumb_url": item.get("thumb"),
        "added_at": _epoch_s_to_ms(item.get("addedAt")),
        "height": None,
        "width": None,
        "video_codec": None,
        "audio_codec": None,
        "container": None,
        "bitrate": None,
        "part_key": None,
        "part_size": None,
        "duration_ms": None,
        "synced_at": synced_at,
    }


def _map_episode(
    item: dict,
    server_id: str,
    show_rating_key: str,
    season_rating_key: str,
    season_num: Optional[int],
    synced_at: int,
) -> dict:
    rating_key = str(item.get("ratingKey"))
    guids = parse_guids(item)
    media = best_media(item) or {}
    return {
        "server_id": server_id,
        "rating_key": rating_key,
        "type": "episode",
        "title": item.get("title") or f"Episode {item.get('index')}",
        "year": _safe_int(item.get("year")),
        "parent_rating_key": season_rating_key,
        "grandparent_rating_key": show_rating_key,
        "parent_index": season_num,
        "index": _safe_int(item.get("index")),
        "imdb_id": guids["imdb_id"],
        "tmdb_id": guids["tmdb_id"],
        "tvdb_id": guids["tvdb_id"],
        # Episodes are never unification group anchors (Android rule) —
        # a whole-series download resolves episodes by parent show instead.
        "unification_id": None,
        "thumb_url": item.get("thumb"),
        "added_at": _epoch_s_to_ms(item.get("addedAt")),
        "height": media.get("height"),
        "width": media.get("width"),
        "video_codec": media.get("video_codec"),
        "audio_codec": media.get("audio_codec"),
        "container": media.get("container"),
        "bitrate": media.get("bitrate"),
        "part_key": media.get("part_key"),
        "part_size": media.get("part_size"),
        "duration_ms": _safe_int(item.get("duration")),
        "synced_at": synced_at,
    }


_ITEM_UPDATE_COLUMNS = [
    c.name for c in PlexMediaItem.__table__.columns if c.name not in ("server_id", "rating_key")
]


async def _upsert_plex_items(session_factory, rows: list[dict]) -> None:
    if not rows:
        return
    for i in range(0, len(rows), _UPSERT_CHUNK):
        chunk = rows[i : i + _UPSERT_CHUNK]

        async def _do(chunk: list[dict] = chunk) -> None:
            async with session_factory() as db:
                stmt = sqlite_upsert(PlexMediaItem).values(chunk)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["server_id", "rating_key"],
                    set_={k: stmt.excluded[k] for k in _ITEM_UPDATE_COLUMNS},
                )
                await db.execute(stmt)
                await db.commit()

        await run_with_retry(_do, op="plex_upsert_items")


# --- Server discovery / reachability -----------------------------------------


async def _resolve_connections(
    resources: list[PlexResourceDTO],
) -> list[tuple[PlexResourceDTO, Optional[str], bool]]:
    """For each resource, probe its connections in the house-mandated order
    and return the first that answers 200. Bounded concurrency across
    resources; `plex_api_service.probe` itself never raises."""
    sem = asyncio.Semaphore(_PROBE_CONCURRENCY)

    async def _resolve_one(resource: PlexResourceDTO) -> tuple[PlexResourceDTO, Optional[str], bool]:
        async with sem:
            for conn in _probe_order(resource):
                if not conn.uri:
                    continue
                ok = await plex_api_service.probe(conn.uri, resource.access_token)
                if ok:
                    return resource, conn.uri, True
            return resource, None, False

    return list(await asyncio.gather(*[_resolve_one(r) for r in resources]))


async def _upsert_servers(
    session_factory, resolved: list[tuple[PlexResourceDTO, Optional[str], bool]],
) -> None:
    """Persist identity + reachability (NOT `last_synced_at`/`last_sync_error`
    — those are only ever written by `_finalize_servers`, after an actual
    catalogue-sync attempt)."""
    if not resolved:
        return
    ts = now_ms()
    rows = [
        {
            "client_identifier": resource.client_identifier,
            "name": resource.name,
            "owner_title": resource.owner_title,
            "owned": resource.owned,
            "access_token": resource.access_token,
            "base_uri": base_uri,
            "is_reachable": ok,
            "created_at": ts,
            "updated_at": ts,
        }
        for resource, base_uri, ok in resolved
    ]
    update_cols = ["name", "owner_title", "owned", "access_token", "base_uri", "is_reachable", "updated_at"]

    async def _do() -> None:
        async with session_factory() as db:
            stmt = sqlite_upsert(PlexServer).values(rows)
            stmt = stmt.on_conflict_do_update(
                index_elements=["client_identifier"],
                set_={k: stmt.excluded[k] for k in update_cols},
            )
            await db.execute(stmt)
            await db.commit()

    await run_with_retry(_do, op="plex_upsert_servers")


async def _finalize_servers(
    session_factory, synced_server_ids: list[str], server_errors: dict[str, str], run_ts: int,
) -> None:
    """After the catalogue-sync attempt: bump `last_synced_at` on success,
    record a bounded `last_sync_error` on failure (leaving the previous
    `last_synced_at` untouched)."""
    if not synced_server_ids:
        return

    async def _do() -> None:
        async with session_factory() as db:
            for server_id in synced_server_ids:
                client_identifier = parse_plex_server_id(server_id)
                if not client_identifier:
                    continue
                error = server_errors.get(server_id)
                values: dict[str, Any] = {"updated_at": now_ms(), "last_sync_error": error}
                if error is None:
                    values["last_synced_at"] = run_ts
                await db.execute(
                    update(PlexServer)
                    .where(PlexServer.client_identifier == client_identifier)
                    .values(**values)
                )
            await db.commit()

    await run_with_retry(_do, op="plex_finalize_servers")


# --- Per-server catalogue sync ------------------------------------------------


async def _sync_show_episodes(
    base_uri: str, access_token: str, server_id: str, show_item: dict, run_ts: int,
) -> list[dict]:
    show_rating_key = str(show_item["ratingKey"])
    seasons = await plex_api_service.list_children(base_uri, access_token, show_rating_key)
    episode_rows: list[dict] = []
    for season in seasons:
        season_rating_key = season.get("ratingKey")
        if not season_rating_key:
            continue
        season_num = _safe_int(season.get("index"))
        episodes = await plex_api_service.list_children(base_uri, access_token, str(season_rating_key))
        for ep in episodes:
            if not ep.get("ratingKey"):
                continue
            episode_rows.append(
                _map_episode(ep, server_id, show_rating_key, str(season_rating_key), season_num, run_ts)
            )
    return episode_rows


async def _sync_one_server(
    session_factory,
    server_id: str,
    base_uri: str,
    access_token: str,
    run_ts: int,
    show_semaphore: asyncio.Semaphore,
) -> dict[str, int]:
    counts = {"movies": 0, "shows": 0, "episodes": 0}
    sections = await plex_api_service.list_sections(base_uri, access_token)

    for section in sections:
        section_type = section.get("type")
        section_key = section.get("key")
        if not section_key:
            continue
        items = await plex_api_service.list_section_items(base_uri, access_token, str(section_key))
        items = [it for it in items if it.get("ratingKey")]

        if section_type == "movie":
            rows = [_map_movie(item, server_id, run_ts) for item in items]
            await _upsert_plex_items(session_factory, rows)
            counts["movies"] += len(rows)

        elif section_type == "show":
            show_rows = [_map_show(item, server_id, run_ts) for item in items]
            await _upsert_plex_items(session_factory, show_rows)
            counts["shows"] += len(show_rows)

            async def _bounded_episodes(show_item: dict) -> list[dict]:
                async with show_semaphore:
                    return await _sync_show_episodes(base_uri, access_token, server_id, show_item, run_ts)

            episode_batches = await asyncio.gather(
                *[_bounded_episodes(item) for item in items], return_exceptions=True,
            )
            for batch in episode_batches:
                if isinstance(batch, BaseException):
                    logger.warning(
                        "Plex sync: episode fetch failed for a show on server %s (%s)",
                        server_id, batch.__class__.__name__,
                    )
                    continue
                if batch:
                    await _upsert_plex_items(session_factory, batch)
                    counts["episodes"] += len(batch)

    return counts


# --- Post-pass: tmdb -> imdb bridge -------------------------------------------


async def _bridge_tmdb_to_imdb(session_factory) -> int:
    """Two servers can list the SAME title with different guid coverage (one
    has both imdb+tmdb, the other only tmdb) — without this pass they'd get
    two different `unification_id`s and never converge. Builds a
    `tmdb_id -> imdb_id` map from items that carry both, then rewrites the
    `unification_id` of any tmdb-only item whose tmdb_id is in that map."""

    async def _load() -> list[tuple[str, str, Optional[str], Optional[str]]]:
        async with session_factory() as db:
            result = await db.execute(
                select(
                    PlexMediaItem.server_id, PlexMediaItem.rating_key,
                    PlexMediaItem.imdb_id, PlexMediaItem.tmdb_id,
                )
                .where(PlexMediaItem.type.in_(("movie", "show")))
                # Deterministic ordering so the "first-wins" tmdb->imdb map
                # below picks the same imdb_id every run when a tmdb_id maps to
                # two different imdb_ids across servers (rare/anomalous, but
                # otherwise order-of-query dependent — same class as CR-F09).
                .order_by(PlexMediaItem.server_id, PlexMediaItem.rating_key)
            )
            return list(result.all())

    rows = await run_with_retry(_load, op="plex_bridge_load")

    tmdb_to_imdb: dict[str, str] = {}
    for _sid, _rk, imdb_id, tmdb_id in rows:
        if imdb_id and tmdb_id and tmdb_id not in tmdb_to_imdb:
            tmdb_to_imdb[tmdb_id] = imdb_id

    if not tmdb_to_imdb:
        return 0

    targets = [
        (sid, rk, tmdb_id)
        for sid, rk, imdb_id, tmdb_id in rows
        if not imdb_id and tmdb_id and tmdb_id in tmdb_to_imdb
    ]
    if not targets:
        return 0

    async def _apply() -> int:
        async with session_factory() as db:
            for sid, rk, tmdb_id in targets:
                bridged = tmdb_to_imdb[tmdb_id]
                normalized = bridged if bridged.startswith("tt") else f"tt{bridged}"
                await db.execute(
                    update(PlexMediaItem)
                    .where(PlexMediaItem.server_id == sid, PlexMediaItem.rating_key == rk)
                    .values(unification_id=f"imdb://{normalized}")
                )
            await db.commit()
            return len(targets)

    updated = await run_with_retry(_apply, op="plex_bridge_apply")
    if updated:
        logger.info("Plex sync: bridged %d tmdb-only item(s) to an imdb-based unification_id", updated)
    return updated


# --- Sweep ---------------------------------------------------------------


async def _sweep_server(session_factory, server_id: str, run_ts: int) -> int:
    async def _do() -> int:
        async with session_factory() as db:
            result = await db.execute(
                delete(PlexMediaItem).where(
                    PlexMediaItem.server_id == server_id, PlexMediaItem.synced_at < run_ts,
                )
            )
            await db.commit()
            return result.rowcount or 0

    removed = await run_with_retry(_do, op="plex_sweep")
    if removed:
        logger.info("Plex sync: removed %d stale item(s) from %s", removed, server_id)
    return removed


# --- Entry point ---------------------------------------------------------


async def run_full_sync(session_factory) -> PlexSyncReport:
    """Full discover -> probe -> catalogue-sync -> bridge -> sweep pass.

    No-op (returns a `status="disabled"` report) when `PLEX_ACCOUNT_TOKEN`
    is unset. Returns `status="already_running"` without doing any work if
    another sync currently holds the claim. Always releases the claim
    (`finally`), even on an unexpected exception.
    """
    if not settings.PLEX_ACCOUNT_TOKEN:
        return PlexSyncReport(status="disabled")

    if not await _claim_sync(session_factory):
        return PlexSyncReport(status="already_running")

    report = PlexSyncReport()
    t0 = time.monotonic()
    release_error: Optional[str] = None

    try:
        resources = await plex_api_service.discover_servers(settings.PLEX_ACCOUNT_TOKEN)
        report.servers_total = len(resources)

        resolved = await _resolve_connections(resources)
        await _upsert_servers(session_factory, resolved)

        run_ts = now_ms()
        reachable = [(r, base_uri) for r, base_uri, ok in resolved if ok and base_uri]
        report.servers_reachable = len(reachable)

        show_semaphore = asyncio.Semaphore(_SHOW_CONCURRENCY)
        synced_server_ids: list[str] = []
        server_errors: dict[str, str] = {}

        for resource, base_uri in reachable:
            server_id = build_plex_server_id(resource.client_identifier)
            try:
                counts = await _sync_one_server(
                    session_factory, server_id, base_uri, resource.access_token, run_ts, show_semaphore,
                )
                report.movies += counts["movies"]
                report.shows += counts["shows"]
                report.episodes += counts["episodes"]
                synced_server_ids.append(server_id)
            except Exception as exc:
                message = _safe_error(exc)
                logger.error("Plex sync: server %s failed: %s", resource.client_identifier, message)
                if len(report.errors) < _MAX_ERRORS:
                    report.errors.append(message)
                synced_server_ids.append(server_id)
                server_errors[server_id] = message

        await _bridge_tmdb_to_imdb(session_factory)

        for server_id in synced_server_ids:
            if server_id not in server_errors:
                await _sweep_server(session_factory, server_id, run_ts)

        await _finalize_servers(session_factory, synced_server_ids, server_errors, run_ts)

        logger.info(
            "Plex sync complete: %d/%d server(s) reachable, %d movies, %d shows, %d episodes",
            report.servers_reachable, report.servers_total, report.movies, report.shows, report.episodes,
        )

    except Exception as exc:
        release_error = _safe_error(exc)
        report.status = "error"
        if len(report.errors) < _MAX_ERRORS:
            report.errors.append(release_error)
        logger.error("Plex sync failed: %s", release_error)
    finally:
        report.duration_s = time.monotonic() - t0
        await _release_sync(session_factory, error=release_error)

    return report
