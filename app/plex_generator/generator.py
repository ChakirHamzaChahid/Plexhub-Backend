import logging
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.plex_generator.mapping import MappingStore
from app.plex_generator.models import PlexMovie, PlexSeries, SyncReport
from app.plex_generator.naming import (
    movie_path,
    movie_version_path,
    movie_nfo_path,
    movie_poster_path,
    movie_fanart_path,
    series_episode_path,
    series_episode_version_path,
    series_nfo_path,
    series_poster_path,
    series_fanart_path,
)
from app.plex_generator.nfo_builder import build_movie_nfo, build_tvshow_nfo, build_episode_nfo
from app.plex_generator.source import MediaSource
from app.plex_generator.storage import LibraryStorage
from app.models.schemas import apply_adult_prefix
from app.utils.string_normalizer import parse_title_year_and_suffix

logger = logging.getLogger("plexhub.plex_generator")


def _short_id(rating_key: str, length: int = 6) -> str:
    """Pick a stable short fragment of a rating_key, used as last-resort
    disambiguator when two media collide on (title, year) and neither has
    a qualifier in its source title."""
    safe = rating_key or ""
    # Strip common Xtream extensions so the fragment looks cleaner.
    for ext in (".mkv", ".mp4", ".avi", ".ts"):
        if safe.endswith(ext):
            safe = safe[: -len(ext)]
            break
    return safe[:length] or "x"


@dataclass
class _NameResolution:
    """The folder/file disambiguation chosen for a single media."""
    clean_title: str
    year: int | None
    suffix: str | None
    fallback_id: str | None


def _resolve_movie_names(movies) -> dict[str, _NameResolution]:
    """For each movie, decide whether the canonical "Title (Year)" is unique
    enough or whether we need to attach the source-title suffix (or a
    rating_key fragment) to keep collisions distinct.

    Strategy: group movies by canonical (clean_title, year). Singletons get
    the bare canonical name. Groups of 2+ keep their original suffix; if
    several share the same suffix (or none), the rating_key fragment kicks
    in as a last resort.
    """
    parsed: dict[str, tuple[str, int | None, str | None]] = {}
    groups: dict[tuple[str, int | None], list[str]] = {}
    for movie in movies:
        clean, parsed_year, suffix = parse_title_year_and_suffix(movie.title or "")
        # Re-apply the "[XXX] " tag AFTER parsing: parse_title_year_and_suffix
        # strips a leading [...] bracket, so prefixing earlier would be lost. The
        # tagged title becomes the grouping/path basis → folders + .strm files
        # carry the tag (an adult movie and a non-adult homonym land in distinct
        # folders). Idempotent guard lives in apply_adult_prefix.
        clean = apply_adult_prefix(clean, getattr(movie, "is_adult", False))
        year = movie.year if movie.year is not None else parsed_year
        parsed[movie.source_id] = (clean, year, suffix)
        groups.setdefault((clean, year), []).append(movie.source_id)

    resolutions: dict[str, _NameResolution] = {}
    for (clean, year), source_ids in groups.items():
        if len(source_ids) == 1:
            sid = source_ids[0]
            resolutions[sid] = _NameResolution(clean, year, None, None)
            continue
        # Collision — prefer suffix; if duplicates of suffix remain, add fallback id.
        suffix_buckets: dict[str | None, list[str]] = {}
        for sid in source_ids:
            _, _, suffix = parsed[sid]
            suffix_buckets.setdefault(suffix, []).append(sid)
        for suffix, sids in suffix_buckets.items():
            if len(sids) == 1:
                resolutions[sids[0]] = _NameResolution(clean, year, suffix, None)
            else:
                for sid in sids:
                    resolutions[sid] = _NameResolution(
                        clean, year, suffix, _short_id(sid),
                    )
    return resolutions


def _resolve_series_names(series_list) -> dict[str, _NameResolution]:
    """Same idea as _resolve_movie_names, but for shows. The canonical key
    is (clean_title, year) too — with year=None for shows whose title has
    no year embedded (most of them)."""
    parsed: dict[str, tuple[str, int | None, str | None]] = {}
    groups: dict[tuple[str, int | None], list[str]] = {}
    for show in series_list:
        clean, parsed_year, suffix = parse_title_year_and_suffix(show.title or "")
        year = show.year if show.year is not None else parsed_year
        parsed[show.source_id] = (clean, year, suffix)
        groups.setdefault((clean, year), []).append(show.source_id)

    resolutions: dict[str, _NameResolution] = {}
    for (clean, year), source_ids in groups.items():
        if len(source_ids) == 1:
            sid = source_ids[0]
            resolutions[sid] = _NameResolution(clean, year, None, None)
            continue
        suffix_buckets: dict[str | None, list[str]] = {}
        for sid in source_ids:
            _, _, suffix = parsed[sid]
            suffix_buckets.setdefault(suffix, []).append(sid)
        for suffix, sids in suffix_buckets.items():
            if len(sids) == 1:
                resolutions[sids[0]] = _NameResolution(clean, year, suffix, None)
            else:
                for sid in sids:
                    resolutions[sid] = _NameResolution(
                        clean, year, suffix, _short_id(sid),
                    )
    return resolutions


def _classify_image_error(exc: BaseException) -> str:
    """Bucket an image-download exception so we can aggregate counts.

    Buckets:
      - "http_4xx" / "http_5xx": server returned an HTTP error (mostly 404).
      - "connect_error":         DNS, refused, SSL handshake — network reachability.
      - "timeout":               request didn't complete within the client timeout.
      - "other":                 unexpected — caller logs full traceback.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return "http_4xx" if 400 <= code < 500 else "http_5xx"
    if isinstance(exc, httpx.ConnectError):
        return "connect_error"
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    return "other"


class PlexLibraryGenerator:
    """Orchestrates Plex library generation from a media source.

    Implements an idempotent sync algorithm:
    1. Load existing mapping
    2. Query source for movies + series
    3. Diff each item (create / update / move / skip)
    4. Delete items in mapping but absent from source
    5. Save mapping + return report
    """

    def __init__(
        self,
        source: MediaSource,
        storage: LibraryStorage,
        output_dir: Path,
        strm_only: bool = False,
    ):
        self.source = source
        self.storage = storage
        self.output_dir = output_dir
        self.strm_only = strm_only
        self.mapping = MappingStore(output_dir)

    async def generate(self) -> SyncReport:
        from concurrent.futures import Future
        start = time.monotonic()
        report = SyncReport()
        # Collect image download futures for non-blocking I/O
        self._image_futures: list[Future] = []

        self.mapping.load()
        # Mapping keys are version keys: f"{server_id}:{source_id}". seen_paths
        # tracks every .strm path written this run so we can tell, on deletion,
        # whether a folder still holds a live version (shared NFO must survive).
        seen_keys: set[str] = set()
        seen_paths: set[str] = set()

        # --- Movies ---
        movies = await self.source.get_movies()
        movie_names = _resolve_movie_names(movies)
        for movie in movies:
            try:
                self._sync_movie(movie, movie_names[movie.source_id], report, seen_keys, seen_paths)
            except Exception as e:
                msg = f"Error processing movie {movie.source_id} ({movie.title}): {e}"
                logger.error(msg, exc_info=True)
                report.errors.append(msg)

        # --- Series ---
        series_list = await self.source.get_series()
        series_names = _resolve_series_names(series_list)
        for series in series_list:
            try:
                self._sync_series(series, series_names[series.source_id], report, seen_keys, seen_paths)
            except Exception as e:
                msg = f"Error processing series {series.source_id} ({series.title}): {e}"
                logger.error(msg, exc_info=True)
                report.errors.append(msg)

        # --- Wait for all image downloads to complete ---
        if self._image_futures:
            logger.info(f"Waiting for {len(self._image_futures)} image downloads...")
            for future in self._image_futures:
                try:
                    future.result(timeout=30.0)
                except Exception as e:
                    reason = _classify_image_error(e)
                    report.image_failures += 1
                    report.image_failure_reasons[reason] = (
                        report.image_failure_reasons.get(reason, 0) + 1
                    )
                    # Per-image detail at DEBUG to keep INFO logs readable.
                    # Only the "other" bucket gets a full traceback (real bugs).
                    logger.debug(
                        "Image download failed (%s): %s", reason, e,
                        exc_info=(reason == "other"),
                    )
            self._image_futures.clear()

        # --- Delete stale versions (folder-aware) ---
        # A folder still referenced by a live version keeps its shared NFO/images;
        # only when no live version remains do we remove the folder-level metadata.
        from pathlib import PurePosixPath
        live_dirs = {str(PurePosixPath(p).parent) for p in seen_paths}
        stale_keys = self.mapping.all_source_ids() - seen_keys
        for key in stale_keys:
            entry = self.mapping.get(key)
            if entry:
                p = PurePosixPath(entry.path)
                parent = str(p.parent)
                self.storage.delete_file(entry.path)
                # Per-file sibling NFO (episodes write one NFO per version file).
                self.storage.delete_file(str(p.with_suffix(".nfo")))
                # Shared folder-level metadata only when the folder is now dead.
                if parent not in live_dirs:
                    for name in ("poster.jpg", "fanart.jpg", "movie.nfo", "tvshow.nfo"):
                        self.storage.delete_file(f"{parent}/{name}")
                self.storage.cleanup_empty_dirs(entry.path)
                self.mapping.remove(key)
                report.deleted += 1
                logger.debug(f"Deleted: {entry.path}")

        self.mapping.save()
        report.duration_seconds = round(time.monotonic() - start, 2)

        logger.info(
            f"Plex library sync complete: "
            f"{report.created} created, {report.updated} updated, "
            f"{report.deleted} deleted, {report.unchanged} unchanged, "
            f"{report.image_failures} image failures, "
            f"{len(report.errors)} errors "
            f"({report.duration_seconds}s)"
        )
        if report.image_failures:
            # Single aggregated WARNING — keeps the INFO log readable but still
            # surfaces the breakdown so flapping hosts / TMDB stale paths are visible.
            reasons_str = ", ".join(
                f"{k}={v}" for k, v in sorted(report.image_failure_reasons.items())
            )
            logger.warning(
                "Plex generation: %d image downloads failed (%s)",
                report.image_failures, reasons_str,
            )
        return report

    def _sync_strm(
        self, key: str, path: str, stream_url: str, report: SyncReport,
    ) -> None:
        """Idempotent write of a single .strm version, keyed by version key.

        CREATE (new key) / MOVE (path changed) / UPDATE (url changed) / unchanged."""
        existing = self.mapping.get(key)
        if existing is None:
            self.storage.write_strm(path, stream_url)
            self.mapping.set(key, path, stream_url)
            report.created += 1
            logger.debug(f"Created: {path}")
        elif existing.path != path:
            self.storage.delete_file(existing.path)
            self.storage.delete_file(existing.path[:-5] + ".nfo" if existing.path.endswith(".strm") else existing.path)
            self.storage.cleanup_empty_dirs(existing.path)
            self.storage.write_strm(path, stream_url)
            self.mapping.set(key, path, stream_url)
            report.updated += 1
            logger.debug(f"Moved: {existing.path} -> {path}")
        elif existing.stream_url != stream_url:
            self.storage.write_strm(path, stream_url)
            self.mapping.set(key, path, stream_url)
            report.updated += 1
            logger.debug(f"Updated URL: {path}")
        else:
            report.unchanged += 1

    def _sync_movie(
        self, movie: PlexMovie, name: _NameResolution,
        report: SyncReport, seen_keys: set[str], seen_paths: set[str],
    ) -> None:
        multi = len(movie.versions) > 1
        for v in movie.versions:
            key = f"{v.server_id}:{v.source_id}"
            seen_keys.add(key)
            if multi:
                path = movie_version_path(
                    name.clean_title, name.year, v.label or v.source_id,
                    suffix=name.suffix, fallback_id=name.fallback_id,
                )
            else:
                path = movie_path(
                    name.clean_title, name.year,
                    suffix=name.suffix, fallback_id=name.fallback_id,
                )
            seen_paths.add(path)
            self._sync_strm(key, path, v.stream_url, report)

        # Folder-level metadata (movie.nfo + poster/fanart) written once per group.
        if not self.strm_only:
            self._write_movie_metadata(movie, name, report)

    def _write_movie_metadata(
        self, movie: PlexMovie, name: _NameResolution, report: SyncReport,
    ) -> None:
        nfo = build_movie_nfo(movie)
        self.storage.write_file(
            movie_nfo_path(name.clean_title, name.year, name.suffix, name.fallback_id),
            nfo,
        )

        if movie.poster_url:
            poster_rel = movie_poster_path(
                name.clean_title, name.year, name.suffix, name.fallback_id,
            )
            if hasattr(self.storage, 'submit_image_download'):
                f = self.storage.submit_image_download(poster_rel, movie.poster_url)
                if f is not None:
                    self._image_futures.append(f)
            elif not self.storage.download_image(poster_rel, movie.poster_url):
                report.image_failures += 1
        if movie.fanart_url:
            fanart_rel = movie_fanart_path(
                name.clean_title, name.year, name.suffix, name.fallback_id,
            )
            if hasattr(self.storage, 'submit_image_download'):
                f = self.storage.submit_image_download(fanart_rel, movie.fanart_url)
                if f is not None:
                    self._image_futures.append(f)
            elif not self.storage.download_image(fanart_rel, movie.fanart_url):
                report.image_failures += 1

    def _sync_series(
        self, series: PlexSeries, name: _NameResolution,
        report: SyncReport, seen_keys: set[str], seen_paths: set[str],
    ) -> None:
        # Write series-level metadata (tvshow.nfo, poster, fanart) once per group
        if not self.strm_only:
            self._write_series_metadata(series, name, report)

        # Sync each episode slot using the same name resolution as the series
        for ep in series.episodes:
            try:
                self._sync_episode(ep, name, report, seen_keys, seen_paths)
            except Exception as e:
                msg = (
                    f"Error processing episode {ep.source_id} "
                    f"({series.title} S{ep.season_num:02d}E{ep.episode_num:02d}): {e}"
                )
                logger.error(msg, exc_info=True)
                report.errors.append(msg)

    def _sync_episode(
        self, ep, name: _NameResolution,
        report: SyncReport, seen_keys: set[str], seen_paths: set[str],
    ) -> None:
        # Plex merges several files sharing the same SxxEyy into one episode with
        # multiple versions; each version is a distinct file with a matching NFO.
        multi = len(ep.versions) > 1
        nfo = build_episode_nfo(ep) if not self.strm_only else None
        for v in ep.versions:
            key = f"{v.server_id}:{v.source_id}"
            seen_keys.add(key)
            if multi:
                path = series_episode_version_path(
                    name.clean_title, ep.season_num, ep.episode_num, v.label or v.source_id,
                    year=name.year, suffix=name.suffix, fallback_id=name.fallback_id,
                )
            else:
                path = series_episode_path(
                    name.clean_title, ep.season_num, ep.episode_num,
                    year=name.year, suffix=name.suffix, fallback_id=name.fallback_id,
                )
            seen_paths.add(path)
            self._sync_strm(key, path, v.stream_url, report)
            if nfo is not None:
                self.storage.write_file(path[:-5] + ".nfo", nfo)

    def _write_series_metadata(
        self, series: PlexSeries, name: _NameResolution, report: SyncReport,
    ) -> None:
        nfo = build_tvshow_nfo(series)
        self.storage.write_file(
            series_nfo_path(name.clean_title, name.year, name.suffix, name.fallback_id),
            nfo,
        )

        if series.poster_url:
            poster_rel = series_poster_path(
                name.clean_title, name.year, name.suffix, name.fallback_id,
            )
            if hasattr(self.storage, 'submit_image_download'):
                f = self.storage.submit_image_download(poster_rel, series.poster_url)
                if f is not None:
                    self._image_futures.append(f)
            elif not self.storage.download_image(poster_rel, series.poster_url):
                report.image_failures += 1
        if series.fanart_url:
            fanart_rel = series_fanart_path(
                name.clean_title, name.year, name.suffix, name.fallback_id,
            )
            if hasattr(self.storage, 'submit_image_download'):
                f = self.storage.submit_image_download(fanart_rel, series.fanart_url)
                if f is not None:
                    self._image_futures.append(f)
            elif not self.storage.download_image(fanart_rel, series.fanart_url):
                report.image_failures += 1
