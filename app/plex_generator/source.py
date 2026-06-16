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
from app.services.stream_service import build_stream_url
from app.utils.server_id import build_server_id
from app.utils.string_normalizer import parse_title_year_and_suffix

logger = logging.getLogger("plexhub.plex_generator.source")


class MediaSource(ABC):
    """Abstract source of media items for Plex library generation."""

    @abstractmethod
    async def get_movies(self) -> list[PlexMovie]: ...

    @abstractmethod
    async def get_series(self) -> list[PlexSeries]: ...


def _group_key(row: Media) -> str:
    """Grouping key for dedup. Prefers the cross-source unification_id
    (imdb://… > tmdb://… > title_…_year). Falls back to a per-row key so an
    item with no resolvable identity stays a group of its own (no false merge).
    """
    return row.unification_id or f"{row.server_id}:{row.rating_key}"


def _is_enriched(row: Media) -> bool:
    return bool(row.imdb_id) or bool(row.tmdb_id and str(row.tmdb_id).isdigit())


def _best_row(rows: list[Media]) -> Media:
    """Pick the row whose metadata best represents the group (NFO + images).

    Preference: enriched (has imdb/tmdb) > has resolved poster > higher rating >
    has a year > longer (more descriptive) title. Deterministic tie-break on
    (server_id, rating_key) so re-runs are stable."""
    def key(r: Media):
        return (
            _is_enriched(r),
            bool(r.resolved_thumb_url or r.thumb_url),
            r.display_rating or r.scraped_rating or 0.0,
            r.year is not None,
            len(r.title or ""),
            # stable tie-break (negated lexicographically via reverse=True below)
        )
    return max(rows, key=lambda r: (key(r), r.server_id or "", r.rating_key or ""))


def _dedup_labels(labels: list[str]) -> list[str]:
    """Ensure version labels are unique within a group (append #n on collision)."""
    seen: dict[str, int] = {}
    out: list[str] = []
    for label in labels:
        n = seen.get(label, 0) + 1
        seen[label] = n
        out.append(label if n == 1 else f"{label} #{n}")
    return out


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

    def _version_label(self, row: Media, account: XtreamAccount) -> str:
        """Human-readable edition label, e.g. "VF · Compte 1" / "Compte 1".

        The qualifier (VF/HD/VOSTFR…) comes from the source title; the account
        label disambiguates the same qualifier across providers."""
        _, _, suffix = parse_title_year_and_suffix(row.title or "")
        acc_label = (account.label or account.id).strip()
        if suffix:
            return f"{suffix} · {acc_label}"
        return acc_label

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

            groups: dict[str, list[Media]] = {}
            async for row in result.scalars():
                groups.setdefault(_group_key(row), []).append(row)

            movies: list[PlexMovie] = []
            for gkey, rows in groups.items():
                best = _best_row(rows)

                versions: list[PlexMovieVersion] = []
                raw_labels: list[str] = []
                pending: list[tuple[Media, str]] = []
                for row in rows:
                    account = accounts.get(row.server_id)
                    if account is None:
                        continue
                    url = build_stream_url(account, row.rating_key)
                    if not url:
                        continue
                    raw_labels.append(self._version_label(row, account))
                    pending.append((row, url))

                if not pending:
                    continue

                for (row, url), label in zip(pending, _dedup_labels(raw_labels)):
                    versions.append(PlexMovieVersion(
                        source_id=row.rating_key,
                        server_id=row.server_id,
                        label=label,
                        stream_url=url,
                    ))

                movies.append(PlexMovie(
                    source_id=gkey,
                    title=best.title,
                    year=best.year,
                    versions=versions,
                    poster_url=best.resolved_thumb_url or best.thumb_url,
                    fanart_url=best.resolved_art_url or best.art_url,
                    genres=best.genres,
                    summary=best.summary,
                    imdb_id=best.imdb_id,
                    tmdb_id=int(best.tmdb_id) if best.tmdb_id and str(best.tmdb_id).isdigit() else None,
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

            # Shows, grouped by unification across accounts.
            show_stream = await db.stream(
                select(Media).where(
                    Media.server_id.in_(server_ids),
                    Media.type == "show",
                    Media.is_in_allowed_categories == True,  # noqa: E712
                ).execution_options(yield_per=1000)
            )
            show_groups: dict[str, list[Media]] = {}
            async for show in show_stream.scalars():
                show_groups.setdefault(_group_key(show), []).append(show)

            # Episodes, indexed by their owning show (scoped per server — a
            # ratingKey is only unique within one server, AUDIT-APP-16 style).
            ep_query = select(Media).where(
                Media.server_id.in_(server_ids),
                Media.type == "episode",
            )
            if settings.STREAM_FILTER_BROKEN:
                ep_query = ep_query.where(Media.is_broken == False)  # noqa: E712
            ep_stream = await db.stream(ep_query.execution_options(yield_per=1000))
            episodes_by_show: dict[tuple[str, str], list[Media]] = {}
            async for ep in ep_stream.scalars():
                key = (ep.server_id, ep.grandparent_rating_key or "")
                episodes_by_show.setdefault(key, []).append(ep)

            series_list: list[PlexSeries] = []
            for gkey, shows in show_groups.items():
                best = _best_row(shows)

                # Gather every episode of every show in the group, then regroup
                # by (season, episode) so the same SxxEyy across accounts merges.
                slots: dict[tuple[int, int], list[Media]] = {}
                for show in shows:
                    for ep in episodes_by_show.get((show.server_id, show.rating_key), []):
                        if not ep.parent_index or not ep.index:
                            continue
                        slots.setdefault((ep.parent_index, ep.index), []).append(ep)

                plex_episodes: list[PlexEpisode] = []
                for (season_num, episode_num), eps in slots.items():
                    best_ep = _best_row(eps)

                    raw_labels: list[str] = []
                    pending: list[tuple[Media, str]] = []
                    for ep in eps:
                        account = accounts.get(ep.server_id)
                        if account is None:
                            continue
                        url = build_stream_url(account, ep.rating_key)
                        if not url:
                            continue
                        raw_labels.append(self._version_label(ep, account))
                        pending.append((ep, url))

                    if not pending:
                        continue

                    ep_versions: list[PlexEpisodeVersion] = []
                    for (ep, url), label in zip(pending, _dedup_labels(raw_labels)):
                        ep_versions.append(PlexEpisodeVersion(
                            source_id=ep.rating_key,
                            server_id=ep.server_id,
                            label=label,
                            stream_url=url,
                        ))

                    plex_episodes.append(PlexEpisode(
                        source_id=f"{gkey}|S{season_num:02d}E{episode_num:02d}",
                        series_title=best.title,
                        season_num=season_num,
                        episode_num=episode_num,
                        title=best_ep.title,
                        versions=ep_versions,
                        summary=best_ep.summary,
                        duration_ms=best_ep.duration,
                        thumb_url=best_ep.resolved_thumb_url or best_ep.thumb_url,
                    ))

                if not plex_episodes:
                    continue

                series_list.append(PlexSeries(
                    source_id=gkey,
                    title=best.title,
                    year=best.year,
                    poster_url=best.resolved_thumb_url or best.thumb_url,
                    fanart_url=best.resolved_art_url or best.art_url,
                    genres=best.genres,
                    summary=best.summary,
                    imdb_id=best.imdb_id,
                    tmdb_id=int(best.tmdb_id) if best.tmdb_id and str(best.tmdb_id).isdigit() else None,
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
