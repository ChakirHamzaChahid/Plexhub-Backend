"""Builds the in-memory DAV virtual filesystem from the database.

Reuses the SAME aggregation/naming pipeline the `.strm` generator uses
(`app.plex_generator.source.DatabaseSource` + `app.plex_generator.generator`'s
public naming-resolution aliases + `app.plex_generator.naming`) so the DAV
tree is a byte-for-byte mirror of the `.strm` hierarchy — extension aside.
That parity is what lets Plex (scanning the DAV mount) and Jellyfin (scanning
the `.strm` folder) agree on the same show/movie identity for the same
underlying stream (see `docs/30-ops-plex-webdav.md`).
"""
from __future__ import annotations

from typing import TypeVar

from app.config import settings
from app.dav.vfs import DavEntry, DavTree
from app.plex_generator.generator import resolve_movie_names, resolve_series_names
from app.plex_generator.models import (
    PlexEpisode,
    PlexEpisodeVersion,
    PlexMovie,
    PlexMovieVersion,
    PlexSeries,
)
from app.plex_generator.naming import (
    movie_path,
    movie_version_path,
    series_episode_path,
    series_episode_version_path,
)
from app.plex_generator.source import DatabaseSource
from app.services.stream_service import parse_rating_key

_V = TypeVar("_V", PlexMovieVersion, PlexEpisodeVersion)


def _select_versions(
    versions: list[_V], *, require_known_size: bool, single_version: bool,
) -> list[_V]:
    """Filter/reduce a group's playable versions to what the DAV tree
    publishes.

    - `require_known_size` (default True, `DAV_REQUIRE_KNOWN_SIZE`): drop any
      version without a known `file_size` — an unknown/wrong size breaks
      rclone's VFS layer (`Content-Length` drives its read-ahead/cache).
    - `single_version` (default True, `DAV_SINGLE_VERSION`): keep exactly ONE
      version — the one with the largest known `file_size` (a proxy for
      "best quality"; unknown sizes sort last) — halving the number of files
      Plex has to ffprobe at scan time. Ties broken on `source_id` (the
      rating_key) for determinism across rebuilds.

    Order of the surviving versions is preserved from the input (already
    sorted deterministically by `aggregation_service.build_versions`, see
    `DatabaseSource._build_versions`), except when reduced to a singleton.
    """
    eligible = [v for v in versions if v.file_size is not None] if require_known_size else list(versions)
    if not eligible:
        return []
    if single_version:
        chosen = min(eligible, key=lambda v: (-(v.file_size or 0), v.source_id))
        return [chosen]
    return eligible


def _movie_sort_key(movie: PlexMovie) -> tuple[str, int, str]:
    """Deterministic ordering used to pick the `DAV_MOVIE_LIMIT` cap.

    Determinism (not cosmetic prettiness) is the point: an unstable subset
    would make Plex see items appear/disappear between rebuilds even though
    nothing actually changed upstream. Sorted on the group's (pre-naming-
    resolution) title/year/source_id — same fields available at the point
    the cap is applied, i.e. before `resolve_movie_names` runs."""
    return ((movie.title or "").strip().casefold(), movie.year or 0, movie.source_id)


def _series_sort_key(series: PlexSeries) -> tuple[str, int, str]:
    """Same rationale as `_movie_sort_key`, for the `DAV_SERIES_LIMIT` cap."""
    return ((series.title or "").strip().casefold(), series.year or 0, series.source_id)


def _swap_ext(strm_path: str, rating_key: str) -> str:
    """Swap a `.strm` path's extension for the version's real container
    extension, parsed from its `rating_key` (e.g. "vod_435071.mp4" ->
    ".mp4"). Falls back to ".ts" (Xtream's implicit default container) when
    the rating_key carries no extension — mirrors `stream_service.
    build_stream_url`'s own fallback for the same field."""
    parsed = parse_rating_key(rating_key)
    ext = parsed.get("ext") or "ts"
    base = strm_path[: -len(".strm")] if strm_path.endswith(".strm") else strm_path
    return f"{base}.{ext}"


def _ensure_dir(entries: dict[str, DavEntry], children: dict[str, list[str]], dir_path: str) -> None:
    """Ensure `dir_path` and every one of its ancestors exist in `entries`,
    each registered as a child of its parent. Idempotent — a directory
    that's already present (shared by two files, e.g. a movie folder holding
    two versions) is a no-op."""
    if dir_path in entries:
        return
    if dir_path == "":
        entries[""] = DavEntry(name="", is_dir=True)
        children.setdefault("", [])
        return
    parent, _, name = dir_path.rpartition("/")
    _ensure_dir(entries, children, parent)
    children.setdefault(parent, [])
    if name not in children[parent]:
        children[parent].append(name)
    entries[dir_path] = DavEntry(name=name, is_dir=True)


def _insert_file(
    entries: dict[str, DavEntry], children: dict[str, list[str]], path: str,
    *, size: int | None, server_id: str, rating_key: str,
) -> None:
    parent, _, name = path.rpartition("/")
    _ensure_dir(entries, children, parent)
    children.setdefault(parent, [])
    if name not in children[parent]:
        children[parent].append(name)
    entries[path] = DavEntry(
        name=name, is_dir=False, size=size, server_id=server_id, rating_key=rating_key,
    )


def _insert_movie(entries: dict[str, DavEntry], children: dict[str, list[str]], movie: PlexMovie, name) -> None:
    multi = len(movie.versions) > 1
    for v in movie.versions:
        if multi:
            strm_path = movie_version_path(
                name.clean_title, name.year, v.label or v.source_id,
                suffix=name.suffix, fallback_id=name.fallback_id,
            )
        else:
            strm_path = movie_path(
                name.clean_title, name.year, suffix=name.suffix, fallback_id=name.fallback_id,
            )
        dav_path = _swap_ext(strm_path, v.source_id)
        _insert_file(entries, children, dav_path, size=v.file_size, server_id=v.server_id, rating_key=v.source_id)


def _insert_episode(
    entries: dict[str, DavEntry], children: dict[str, list[str]], ep: PlexEpisode, name,
) -> None:
    multi = len(ep.versions) > 1
    for v in ep.versions:
        if multi:
            strm_path = series_episode_version_path(
                name.clean_title, ep.season_num, ep.episode_num, v.label or v.source_id,
                year=name.year, suffix=name.suffix, fallback_id=name.fallback_id,
            )
        else:
            strm_path = series_episode_path(
                name.clean_title, ep.season_num, ep.episode_num,
                year=name.year, suffix=name.suffix, fallback_id=name.fallback_id,
            )
        dav_path = _swap_ext(strm_path, v.source_id)
        _insert_file(entries, children, dav_path, size=v.file_size, server_id=v.server_id, rating_key=v.source_id)


async def build_dav_tree() -> DavTree:
    """Build a fresh `DavTree` snapshot from the current DB state.

    Steps (mirrors the plan/ticket, in order):
    1. Load movies/series via `DatabaseSource` (same aggregation as the
       `.strm` generator — `DAV_ACCOUNT_IDS` empty = every active account,
       matching `DatabaseSource`'s own default semantics).
    2. Filter: adult movies excluded unless `DAV_INCLUDE_ADULT` (only
       `PlexMovie` carries `is_adult` — `PlexSeries`/`PlexEpisode` don't, so
       series are never adult-filtered here); per group/episode-slot, reduce
       versions via `_select_versions` (`DAV_REQUIRE_KNOWN_SIZE` /
       `DAV_SINGLE_VERSION`); drop movies/episodes left with zero versions,
       then shows left with zero episodes.
    3. Cap: deterministic sort (`_movie_sort_key`/`_series_sort_key`) then
       truncate to `DAV_MOVIE_LIMIT`/`DAV_SERIES_LIMIT` (0 = unlimited) — a
       stable subset so Plex never sees items churn between rebuilds absent
       an actual catalogue change.
    4. Name resolution: `resolve_movie_names`/`resolve_series_names` (the
       generator's own collision-disambiguation) run on the POST-cap lists,
       so DAV paths are byte-identical to what the `.strm` generator would
       produce for that same list, extension aside (`_swap_ext`).
    5. Insert every version as a file (with its intermediate directories) into
       the flat `entries`/`children` maps, then sort each directory's
       children once for deterministic PROPFIND/rclone listings.
    """
    account_ids = settings.DAV_ACCOUNT_IDS or None
    source = DatabaseSource(account_ids=account_ids)

    movies = await source.get_movies()
    series_list = await source.get_series()

    filtered_movies: list[PlexMovie] = []
    for movie in movies:
        if movie.is_adult and not settings.DAV_INCLUDE_ADULT:
            continue
        selected = _select_versions(
            movie.versions,
            require_known_size=settings.DAV_REQUIRE_KNOWN_SIZE,
            single_version=settings.DAV_SINGLE_VERSION,
        )
        if not selected:
            continue
        movie.versions = selected
        filtered_movies.append(movie)

    filtered_series: list[PlexSeries] = []
    for series in series_list:
        new_episodes: list[PlexEpisode] = []
        for ep in series.episodes:
            selected = _select_versions(
                ep.versions,
                require_known_size=settings.DAV_REQUIRE_KNOWN_SIZE,
                single_version=settings.DAV_SINGLE_VERSION,
            )
            if not selected:
                continue
            ep.versions = selected
            new_episodes.append(ep)
        if not new_episodes:
            continue
        series.episodes = new_episodes
        filtered_series.append(series)

    filtered_movies.sort(key=_movie_sort_key)
    filtered_series.sort(key=_series_sort_key)

    if settings.DAV_MOVIE_LIMIT > 0:
        filtered_movies = filtered_movies[: settings.DAV_MOVIE_LIMIT]
    if settings.DAV_SERIES_LIMIT > 0:
        filtered_series = filtered_series[: settings.DAV_SERIES_LIMIT]

    movie_names = resolve_movie_names(filtered_movies)
    series_names = resolve_series_names(filtered_series)

    entries: dict[str, DavEntry] = {}
    children: dict[str, list[str]] = {}
    _ensure_dir(entries, children, "")

    for movie in filtered_movies:
        _insert_movie(entries, children, movie, movie_names[movie.source_id])

    for series in filtered_series:
        name = series_names[series.source_id]
        for ep in series.episodes:
            _insert_episode(entries, children, ep, name)

    for names in children.values():
        names.sort()

    return DavTree(entries=entries, children=children)
