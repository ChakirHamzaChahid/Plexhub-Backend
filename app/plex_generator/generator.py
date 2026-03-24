import logging
import time
from pathlib import Path

from app.plex_generator.mapping import MappingStore
from app.plex_generator.models import PlexMovie, PlexSeries, SyncReport
from app.plex_generator.naming import (
    movie_path,
    movie_nfo_path,
    movie_poster_path,
    movie_fanart_path,
    series_episode_path,
    series_episode_nfo_path,
    series_nfo_path,
    series_poster_path,
    series_fanart_path,
)
from app.plex_generator.nfo_builder import build_movie_nfo, build_tvshow_nfo, build_episode_nfo
from app.plex_generator.source import MediaSource
from app.plex_generator.storage import LibraryStorage

logger = logging.getLogger("plexhub.plex_generator")


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
        start = time.monotonic()
        report = SyncReport()

        self.mapping.load()
        seen_source_ids: set[str] = set()

        # --- Movies ---
        movies = await self.source.get_movies()
        for movie in movies:
            try:
                self._sync_movie(movie, report, seen_source_ids)
            except Exception as e:
                msg = f"Error processing movie {movie.source_id} ({movie.title}): {e}"
                logger.error(msg, exc_info=True)
                report.errors.append(msg)

        # --- Series ---
        series_list = await self.source.get_series()
        for series in series_list:
            try:
                self._sync_series(series, report, seen_source_ids)
            except Exception as e:
                msg = f"Error processing series {series.source_id} ({series.title}): {e}"
                logger.error(msg, exc_info=True)
                report.errors.append(msg)

        # --- Delete stale entries ---
        stale_ids = self.mapping.all_source_ids() - seen_source_ids
        for source_id in stale_ids:
            entry = self.mapping.get(source_id)
            if entry:
                self.storage.delete_file(entry.path)
                self.storage.cleanup_empty_dirs(entry.path)
                self.mapping.remove(source_id)
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
        return report

    def _sync_movie(
        self, movie: PlexMovie, report: SyncReport, seen: set[str],
    ) -> None:
        seen.add(movie.source_id)
        expected_path = movie_path(movie.title, movie.year)
        existing = self.mapping.get(movie.source_id)

        if existing is None:
            # CREATE
            self.storage.write_strm(expected_path, movie.stream_url)
            if not self.strm_only:
                self._write_movie_metadata(movie, report)
            self.mapping.set(movie.source_id, expected_path, movie.stream_url)
            report.created += 1
            logger.debug(f"Created: {expected_path}")

        elif existing.path != expected_path:
            # MOVE (title/year changed)
            self.storage.delete_file(existing.path)
            self.storage.cleanup_empty_dirs(existing.path)
            self.storage.write_strm(expected_path, movie.stream_url)
            if not self.strm_only:
                self._write_movie_metadata(movie, report)
            self.mapping.set(movie.source_id, expected_path, movie.stream_url)
            report.updated += 1
            logger.debug(f"Moved: {existing.path} -> {expected_path}")

        elif existing.stream_url != movie.stream_url:
            # UPDATE (URL changed)
            self.storage.write_strm(expected_path, movie.stream_url)
            self.mapping.set(movie.source_id, expected_path, movie.stream_url)
            report.updated += 1
            logger.debug(f"Updated URL: {expected_path}")

        else:
            report.unchanged += 1

    def _write_movie_metadata(self, movie: PlexMovie, report: SyncReport) -> None:
        nfo = build_movie_nfo(movie)
        self.storage.write_file(movie_nfo_path(movie.title, movie.year), nfo)

        if movie.poster_url:
            if not self.storage.download_image(
                movie_poster_path(movie.title, movie.year), movie.poster_url,
            ):
                report.image_failures += 1
        if movie.fanart_url:
            if not self.storage.download_image(
                movie_fanart_path(movie.title, movie.year), movie.fanart_url,
            ):
                report.image_failures += 1

    def _sync_series(
        self, series: PlexSeries, report: SyncReport, seen: set[str],
    ) -> None:
        # Write series-level metadata (NFO, poster, fanart) once
        if not self.strm_only:
            self._write_series_metadata(series, report)

        # Sync each episode
        for ep in series.episodes:
            try:
                self._sync_episode(ep, report, seen)
            except Exception as e:
                msg = (
                    f"Error processing episode {ep.source_id} "
                    f"({series.title} S{ep.season_num:02d}E{ep.episode_num:02d}): {e}"
                )
                logger.error(msg, exc_info=True)
                report.errors.append(msg)

    def _sync_episode(
        self, ep, report: SyncReport, seen: set[str],
    ) -> None:
        seen.add(ep.source_id)
        expected_path = series_episode_path(
            ep.series_title, ep.season_num, ep.episode_num,
        )
        existing = self.mapping.get(ep.source_id)

        if existing is None:
            self.storage.write_strm(expected_path, ep.stream_url)
            if not self.strm_only:
                self._write_episode_metadata(ep)
            self.mapping.set(ep.source_id, expected_path, ep.stream_url)
            report.created += 1
            logger.debug(f"Created: {expected_path}")

        elif existing.path != expected_path:
            self.storage.delete_file(existing.path)
            self.storage.cleanup_empty_dirs(existing.path)
            self.storage.write_strm(expected_path, ep.stream_url)
            if not self.strm_only:
                self._write_episode_metadata(ep)
            self.mapping.set(ep.source_id, expected_path, ep.stream_url)
            report.updated += 1
            logger.debug(f"Moved: {existing.path} -> {expected_path}")

        elif existing.stream_url != ep.stream_url:
            self.storage.write_strm(expected_path, ep.stream_url)
            self.mapping.set(ep.source_id, expected_path, ep.stream_url)
            report.updated += 1
            logger.debug(f"Updated URL: {expected_path}")

        else:
            report.unchanged += 1

    def _write_episode_metadata(self, ep) -> None:
        nfo = build_episode_nfo(ep)
        self.storage.write_file(
            series_episode_nfo_path(ep.series_title, ep.season_num, ep.episode_num),
            nfo,
        )

    def _write_series_metadata(self, series: PlexSeries, report: SyncReport) -> None:
        nfo = build_tvshow_nfo(series)
        self.storage.write_file(series_nfo_path(series.title), nfo)

        if series.poster_url:
            if not self.storage.download_image(
                series_poster_path(series.title), series.poster_url,
            ):
                report.image_failures += 1
        if series.fanart_url:
            if not self.storage.download_image(
                series_fanart_path(series.title), series.fanart_url,
            ):
                report.image_failures += 1
