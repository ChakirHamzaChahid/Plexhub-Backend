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
    aggregate_movies, aggregate_series, dedup_labels, version_label,
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
        """Turn group member rows into (row, url, unique-label) triples."""
        raw_labels: list[str] = []
        pending: list[tuple[Media, str]] = []
        for row in members:
            account = accounts.get(row.server_id)
            if account is None:
                continue
            url = build_stream_url(account, row.rating_key)
            if not url:
                continue
            raw_labels.append(version_label(row, account.label or account.id))
            pending.append((row, url))
        labels = dedup_labels(raw_labels)
        return [(row, url, label) for (row, url), label in zip(pending, labels)]

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
            if settings.STREAM_FILTER_BROKEN:
                query = query.where(Media.is_broken == False)  # noqa: E712
            result = await db.stream(query.execution_options(yield_per=1000))
            rows = [row async for row in result.scalars()]

            movies: list[PlexMovie] = []
            for grp in aggregate_movies(rows):
                triples = self._build_versions(grp.members, accounts)
                if not triples:
                    continue
                best = grp.best
                movies.append(PlexMovie(
                    source_id=grp.key,
                    title=best.title,
                    year=best.year,
                    versions=[PlexMovieVersion(
                        source_id=row.rating_key, server_id=row.server_id,
                        label=label, stream_url=url,
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
            if settings.STREAM_FILTER_BROKEN:
                ep_query = ep_query.where(Media.is_broken == False)  # noqa: E712
            ep_stream = await db.stream(ep_query.execution_options(yield_per=1000))
            episodes = [e async for e in ep_stream.scalars()]

            series_list: list[PlexSeries] = []
            for grp in aggregate_series(shows, episodes):
                best = grp.best
                plex_episodes: list[PlexEpisode] = []
                for slot in grp.slots:
                    triples = self._build_versions(slot.members, accounts)
                    if not triples:
                        continue
                    best_ep = slot.best
                    plex_episodes.append(PlexEpisode(
                        source_id=f"{grp.key}|S{slot.season:02d}E{slot.episode:02d}",
                        series_title=best.title,
                        season_num=slot.season,
                        episode_num=slot.episode,
                        title=best_ep.title,
                        versions=[PlexEpisodeVersion(
                            source_id=row.rating_key, server_id=row.server_id,
                            label=label, stream_url=url,
                        ) for row, url, label in triples],
                        summary=best_ep.summary,
                        duration_ms=best_ep.duration,
                        thumb_url=best_ep.resolved_thumb_url or best_ep.thumb_url,
                    ))

                if not plex_episodes:
                    continue

                series_list.append(PlexSeries(
                    source_id=grp.key,
                    title=best.title,
                    year=best.year,
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
