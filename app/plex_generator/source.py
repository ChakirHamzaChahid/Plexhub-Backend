import logging
from abc import ABC, abstractmethod

from sqlalchemy import select

from app.db.database import async_session_factory
from app.models.database import Media, XtreamAccount
from app.plex_generator.models import PlexMovie, PlexEpisode, PlexSeries
from app.services.stream_service import build_stream_url

logger = logging.getLogger("plexhub.plex_generator.source")


class MediaSource(ABC):
    """Abstract source of media items for Plex library generation."""

    @abstractmethod
    async def get_movies(self) -> list[PlexMovie]: ...

    @abstractmethod
    async def get_series(self) -> list[PlexSeries]: ...


class DatabaseSource(MediaSource):
    """Reads synced media from the SQLite database and builds stream URLs."""

    def __init__(self, account_id: str):
        self.account_id = account_id
        self.server_id = f"xtream_{account_id}"

    async def _load_account(self, db) -> XtreamAccount | None:
        result = await db.execute(
            select(XtreamAccount).where(XtreamAccount.id == self.account_id)
        )
        return result.scalars().first()

    async def get_movies(self) -> list[PlexMovie]:
        async with async_session_factory() as db:
            account = await self._load_account(db)
            if not account:
                logger.error(f"Account {self.account_id} not found")
                return []

            result = await db.execute(
                select(Media).where(
                    Media.server_id == self.server_id,
                    Media.type == "movie",
                    Media.is_in_allowed_categories == True,
                ).execution_options(yield_per=1000)
            )

            movies = []
            for row in result.scalars():
                url = build_stream_url(account, row.rating_key)
                if not url:
                    continue
                movies.append(PlexMovie(
                    source_id=row.rating_key,
                    title=row.title,
                    year=row.year,
                    stream_url=url,
                    poster_url=row.resolved_thumb_url or row.thumb_url,
                    fanart_url=row.resolved_art_url or row.art_url,
                    genres=row.genres,
                    summary=row.summary,
                    imdb_id=row.imdb_id,
                    tmdb_id=int(row.tmdb_id) if row.tmdb_id and str(row.tmdb_id).isdigit() else None,
                    content_rating=row.content_rating,
                    rating=row.display_rating if row.display_rating else row.scraped_rating,
                    duration_ms=row.duration,
                    cast=row.cast,
                ))

            logger.info(f"Loaded {len(movies)} movies from database")
            return movies

    async def get_series(self) -> list[PlexSeries]:
        async with async_session_factory() as db:
            account = await self._load_account(db)
            if not account:
                logger.error(f"Account {self.account_id} not found")
                return []

            # Load all shows
            show_result = await db.execute(
                select(Media).where(
                    Media.server_id == self.server_id,
                    Media.type == "show",
                    Media.is_in_allowed_categories == True,
                ).execution_options(yield_per=1000)
            )
            shows = list(show_result.scalars())

            # Load all episodes and group by series in a single streaming pass
            ep_result = await db.execute(
                select(Media).where(
                    Media.server_id == self.server_id,
                    Media.type == "episode",
                ).execution_options(yield_per=1000)
            )
            episodes_by_series: dict[str, list[Media]] = {}
            for ep in ep_result.scalars():
                key = ep.grandparent_rating_key or ""
                episodes_by_series.setdefault(key, []).append(ep)

            series_list = []
            for show in shows:
                eps = episodes_by_series.get(show.rating_key, [])
                plex_episodes = []
                for ep in eps:
                    url = build_stream_url(account, ep.rating_key)
                    if not url:
                        continue
                    if not ep.parent_index or not ep.index:
                        continue
                    plex_episodes.append(PlexEpisode(
                        source_id=ep.rating_key,
                        series_title=show.title,
                        season_num=ep.parent_index,
                        episode_num=ep.index,
                        title=ep.title,
                        stream_url=url,
                        summary=ep.summary,
                        duration_ms=ep.duration,
                        thumb_url=ep.resolved_thumb_url or ep.thumb_url,
                    ))

                if not plex_episodes:
                    continue

                series_list.append(PlexSeries(
                    source_id=show.rating_key,
                    title=show.title,
                    year=show.year,
                    poster_url=show.resolved_thumb_url or show.thumb_url,
                    fanart_url=show.resolved_art_url or show.art_url,
                    genres=show.genres,
                    summary=show.summary,
                    imdb_id=show.imdb_id,
                    tmdb_id=int(show.tmdb_id) if show.tmdb_id and str(show.tmdb_id).isdigit() else None,
                    content_rating=show.content_rating,
                    rating=show.display_rating if show.display_rating else show.scraped_rating,
                    cast=show.cast,
                    episodes=plex_episodes,
                ))

            logger.info(
                f"Loaded {len(series_list)} series "
                f"({sum(len(s.episodes) for s in series_list)} episodes) from database"
            )
            return series_list
