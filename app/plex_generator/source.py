import logging
from abc import ABC, abstractmethod

from sqlalchemy import select

from app.config import settings
from app.db.database import async_session_factory
from app.models.database import Media, XtreamAccount
from app.plex_generator.models import (
    PlexMovie, PlexMovieVersion,
    PlexEpisode, PlexEpisodeVersion,
    PlexSeries,
)
from app.services.aggregation_service import (
    aggregate_movies, aggregate_series, build_versions, canonical_title_year,
)
from app.services.stream_service import build_stream_url
from app.utils.server_id import build_server_id

logger = logging.getLogger("plexhub.plex_generator.source")


class MediaSource(ABC):
    """Abstract source of media items for Plex library generation."""

    @abstractmethod
    async def get_movies(self) -> list[PlexMovie]: ...

    @abstractmethod
    async def get_series(self) -> list[PlexSeries]: ...


def _tmdb_int(value) -> int | None:
    return int(value) if value and str(value).isdigit() else None


class DatabaseSource(MediaSource):
    """Reads synced media from the SQLite database, aggregates across all
    (or a subset of) active Xtream accounts, and groups duplicates by
    unification_id so the same movie/series across accounts becomes a single
    library entry with multiple playable versions.
    """

    def __init__(self, account_ids: list[str] | None = None):
        # None => every active account (the unified default). A list restricts
        # the aggregation to those accounts (still flat, still deduped).
        self.account_ids = account_ids

    async def _load_accounts(self, db) -> dict[str, XtreamAccount]:
        query = select(XtreamAccount).where(XtreamAccount.is_active == True)  # noqa: E712
        if self.account_ids is not None:
            query = query.where(XtreamAccount.id.in_(self.account_ids))
        result = await db.execute(query)
        accounts = result.scalars().all()
        return {build_server_id(acc.id): acc for acc in accounts}

    def _build_versions(self, members, accounts):
        """Turn group member rows into (row, url, unique-label) triples.

        Filters out members whose stream is marked broken (when
        `settings.STREAM_FILTER_BROKEN` is on) and members whose account can't
        resolve to a stream URL — so a broken/unreachable source is never
        published as a playable `.strm` (CR-F10). This filtering happens here,
        AFTER `aggregate_movies`/`aggregate_series` (`get_movies`/`get_series`
        below) already grouped on the FULL member set: `is_broken` is
        intentionally NOT filtered at the row-SELECTION stage anymore, so the
        generator's dedup grouping (best_row / convergence) now sees the same
        row predicate as the REST `/unified` endpoints
        (`media_service.get_unified_list` never excludes broken rows before
        grouping either) — only which versions get PUBLISHED still differs,
        by design.

        The remaining sort/label/dedup sequence is delegated to the shared
        `aggregation_service.build_versions` helper (CR-A07) — see its
        docstring for why the stable-identity sort must happen before
        labelling."""
        resolved: list[tuple[Media, str]] = []
        for row in members:
            if settings.STREAM_FILTER_BROKEN and row.is_broken:
                continue
            account = accounts.get(row.server_id)
            if account is None:
                continue
            url = build_stream_url(account, row.rating_key)
            if not url:
                continue
            resolved.append((row, url))
        url_by_key = {(row.server_id, row.rating_key): url for row, url in resolved}
        labelled = build_versions(
            [row for row, _ in resolved],
            lambda r: accounts[r.server_id].label or accounts[r.server_id].id,
        )
        return [
            (row, url_by_key[(row.server_id, row.rating_key)], label)
            for row, label in labelled
        ]

    async def get_movies(self) -> list[PlexMovie]:
        async with async_session_factory() as db:
            accounts = await self._load_accounts(db)
            if not accounts:
                logger.warning("No active accounts for Plex generation (movies)")
                return []

            query = select(Media).where(
                Media.server_id.in_(list(accounts.keys())),
                Media.type == "movie",
                Media.is_in_allowed_categories == True,  # noqa: E712
            )
            # CR-F10: `is_broken` is intentionally NOT filtered here anymore —
            # aggregate_movies/_converge below now groups over the SAME row
            # predicate the REST `/unified` endpoints use (`media_service.
            # get_unified_list` never excludes broken rows before grouping),
            # so best_row/convergence agree across both consumers. Broken
            # members are excluded later, from what gets PUBLISHED, in
            # `_build_versions` (per-group, post-aggregation).
            # CR-P05 (constrained-by-design): `db.stream(execution_options(yield_per=1000))`
            # already avoids buffering the whole driver-side result set at once
            # (server-side cursor, batched fetch) — that part IS the streaming
            # fix. The list comprehension below still has to hold every row in
            # Python, though: `aggregate_movies`/`_converge` (unification-id
            # grouping, id-based convergence) is a whole-set operation — you
            # cannot correctly group/dedup a title across accounts from a
            # partial window of rows without risking split or merged groups in
            # the generated library. So the residual O(catalog) list here is
            # inherent to correct dedup, not a missed streaming opportunity;
            # see `docs/audit/cleanroom-2026-07-11/50-perf.md` (CR-P05).
            result = await db.stream(query.execution_options(yield_per=1000))
            rows = [row async for row in result.scalars()]

            movies: list[PlexMovie] = []
            for grp in aggregate_movies(rows):
                triples = self._build_versions(grp.members, accounts)
                if not triples:
                    continue
                best = grp.best
                clean_title, clean_year = canonical_title_year(best)
                movies.append(PlexMovie(
                    source_id=grp.key,
                    title=clean_title,
                    is_adult=bool(best.is_adult),
                    year=clean_year,
                    versions=[PlexMovieVersion(
                        source_id=row.rating_key, server_id=row.server_id,
                        label=label, stream_url=url, file_size=row.file_size,
                    ) for row, url, label in triples],
                    poster_url=best.resolved_thumb_url or best.thumb_url,
                    fanart_url=best.resolved_art_url or best.art_url,
                    genres=best.genres,
                    summary=best.summary,
                    imdb_id=best.imdb_id,
                    tmdb_id=_tmdb_int(best.tmdb_id),
                    content_rating=best.content_rating,
                    rating=best.display_rating if best.display_rating else best.scraped_rating,
                    duration_ms=best.duration,
                    cast=best.cast,
                ))

            logger.info(
                f"Loaded {len(movies)} movie groups from "
                f"{sum(len(v.versions) for v in movies)} sources "
                f"across {len(accounts)} account(s)"
            )
            return movies

    async def get_series(self) -> list[PlexSeries]:
        async with async_session_factory() as db:
            accounts = await self._load_accounts(db)
            if not accounts:
                logger.warning("No active accounts for Plex generation (series)")
                return []
            server_ids = list(accounts.keys())

            # CR-P05 (constrained-by-design, same rationale as get_movies
            # above): both queries below already stream from the driver
            # (`yield_per=1000`); the resulting `shows`/`episodes` lists must
            # still hold every row because `aggregate_series` groups shows by
            # `unification_id` and matches episodes across accounts by
            # `(season, episode)` — a whole-set operation, not chunkable
            # without risking incorrect/partial groups in the generated
            # library.
            show_stream = await db.stream(
                select(Media).where(
                    Media.server_id.in_(server_ids),
                    Media.type == "show",
                    Media.is_in_allowed_categories == True,  # noqa: E712
                ).execution_options(yield_per=1000)
            )
            shows = [s async for s in show_stream.scalars()]

            ep_query = select(Media).where(
                Media.server_id.in_(server_ids),
                Media.type == "episode",
            )
            # CR-F10: see get_movies — is_broken filtering moved to
            # `_build_versions` (post-aggregation) so episode-slot grouping
            # also matches the REST API's row predicate.
            ep_stream = await db.stream(ep_query.execution_options(yield_per=1000))
            episodes = [e async for e in ep_stream.scalars()]

            series_list: list[PlexSeries] = []
            for grp in aggregate_series(shows, episodes):
                best = grp.best
                clean_title, clean_year = canonical_title_year(best)
                plex_episodes: list[PlexEpisode] = []
                for slot in grp.slots:
                    triples = self._build_versions(slot.members, accounts)
                    if not triples:
                        continue
                    best_ep = slot.best
                    plex_episodes.append(PlexEpisode(
                        source_id=f"{grp.key}|S{slot.season:02d}E{slot.episode:02d}",
                        series_title=clean_title,
                        season_num=slot.season,
                        episode_num=slot.episode,
                        title=best_ep.title,
                        versions=[PlexEpisodeVersion(
                            source_id=row.rating_key, server_id=row.server_id,
                            label=label, stream_url=url, file_size=row.file_size,
                        ) for row, url, label in triples],
                        summary=best_ep.summary,
                        duration_ms=best_ep.duration,
                        thumb_url=best_ep.resolved_thumb_url or best_ep.thumb_url,
                    ))

                if not plex_episodes:
                    continue

                series_list.append(PlexSeries(
                    source_id=grp.key,
                    title=clean_title,
                    year=clean_year,
                    poster_url=best.resolved_thumb_url or best.thumb_url,
                    fanart_url=best.resolved_art_url or best.art_url,
                    genres=best.genres,
                    summary=best.summary,
                    imdb_id=best.imdb_id,
                    tmdb_id=_tmdb_int(best.tmdb_id),
                    content_rating=best.content_rating,
                    rating=best.display_rating if best.display_rating else best.scraped_rating,
                    cast=best.cast,
                    episodes=plex_episodes,
                ))

            logger.info(
                f"Loaded {len(series_list)} series groups "
                f"({sum(len(s.episodes) for s in series_list)} episode slots) "
                f"across {len(accounts)} account(s)"
            )
            return series_list
