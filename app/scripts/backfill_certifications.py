"""Backfill TMDb age certifications for rows that still lack content_rating.

Candidates: media rows where tmdb_id IS NOT NULL and content_rating is NULL,
empty, or "+" (494 legacy rows with "+" garbage from early ingestion — treated
as empty and overwritten).

Fill-if-empty semantics match the enrichment worker: a row that already has a
real content_rating (e.g. set from an NFO <mpaa> tag) is never touched.
Candidates by definition do NOT have a real rating, so a plain UPDATE of
matched rows is safe and correct.

Concurrency: ~8 parallel TMDb requests, ~0.5 s inter-batch sleep on top of
the service's built-in 429 back-off.

Usage:
    # Preview what would change without writing anything
    python -m app.scripts.backfill_certifications --dry-run

    # Live run (commits every 100 rows)
    python -m app.scripts.backfill_certifications

    # Limit scope for a test run
    python -m app.scripts.backfill_certifications --limit 500

The script is idempotent: re-running after an interruption skips rows that
were already updated (they no longer match the NULL/empty/+ filter).

NOT auto-run or registered anywhere — __main__-only.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass, field

from sqlalchemy import and_, or_, select, update

from app.config import settings
from app.db.database import async_session_factory
from app.models.database import Media
from app.services.tmdb_service import tmdb_service

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("plexhub.backfill_cert")

# Rows fetched per DB page (not the TMDb concurrency).
_DB_PAGE = 500
# Parallel TMDb calls per page.
_CONCURRENCY = 8
# Seconds to sleep between pages to avoid sustained 429 pressure.
_INTER_BATCH_SLEEP = 0.5
# Rows committed per transaction.
_COMMIT_BATCH = 100

# Values of content_rating treated as "absent" — safe to overwrite.
_EMPTY_VALUES = (None, "", "+")


@dataclass
class _Stats:
    candidates: int = 0
    fetched: int = 0
    updated: int = 0
    skipped_no_cert: int = 0
    errors: int = 0
    pages: int = 0
    extra: dict = field(default_factory=dict)


def _is_empty(value: str | None) -> bool:
    return value in _EMPTY_VALUES


async def _fetch_cert(
    tmdb_id: str,
    media_type: str,
    semaphore: asyncio.Semaphore,
) -> str | None:
    """Fetch certification for one item. Returns None on any error or miss."""
    async with semaphore:
        try:
            if media_type in ("movie",):
                data = await tmdb_service.get_movie_details(int(tmdb_id))
            else:
                # "show" and anything else treated as TV.
                data = await tmdb_service.get_tv_details(int(tmdb_id))
            return data.content_rating
        except Exception as exc:
            logger.debug("TMDb fetch failed tmdb_id=%s type=%s: %s", tmdb_id, media_type, exc)
            return None


async def _candidates_page(db, offset: int, limit: int) -> list[Media]:
    """Return one page of candidate rows (NULL/empty/+ content_rating with tmdb_id)."""
    result = await db.execute(
        select(Media)
        .where(
            and_(
                Media.tmdb_id.isnot(None),
                Media.tmdb_id != "",
                or_(
                    Media.content_rating.is_(None),
                    Media.content_rating == "",
                    Media.content_rating == "+",
                ),
                # Only movies and shows — episodes inherit from the parent; skipping
                # them avoids unnecessary TMDb calls and keeps the run short.
                Media.type.in_(("movie", "show")),
            )
        )
        .order_by(Media.rating_key, Media.server_id)
        .offset(offset)
        .limit(limit)
    )
    return list(result.scalars().all())


async def _count_candidates(db) -> int:
    from sqlalchemy import func

    result = await db.execute(
        select(func.count()).where(
            and_(
                Media.tmdb_id.isnot(None),
                Media.tmdb_id != "",
                or_(
                    Media.content_rating.is_(None),
                    Media.content_rating == "",
                    Media.content_rating == "+",
                ),
                Media.type.in_(("movie", "show")),
            )
        )
    )
    return result.scalar_one()


async def run(*, dry_run: bool = False, limit: int | None = None) -> _Stats:
    stats = _Stats()
    semaphore = asyncio.Semaphore(_CONCURRENCY)

    if not tmdb_service.is_configured:
        logger.error("TMDB_API_KEY not set — cannot run backfill.")
        return stats

    async with async_session_factory() as db:
        total = await _count_candidates(db)
        if limit is not None:
            total = min(total, limit)
        stats.candidates = total
        logger.info(
            "Backfill certifications: %d candidate rows (dry_run=%s, limit=%s)",
            total, dry_run, limit,
        )
        if total == 0:
            logger.info("Nothing to do — all rows already have content_rating.")
            return stats

        offset = 0
        committed_since_last = 0

        while offset < total:
            page_size = min(_DB_PAGE, total - offset)
            rows = await _candidates_page(db, offset, page_size)
            if not rows:
                break

            stats.pages += 1
            # Fan-out TMDb calls for the page.
            tasks = [
                _fetch_cert(row.tmdb_id, row.type, semaphore)
                for row in rows
            ]
            certs = await asyncio.gather(*tasks)

            for row, cert in zip(rows, certs):
                stats.fetched += 1
                if not cert:
                    stats.skipped_no_cert += 1
                    logger.debug(
                        "No cert: tmdb_id=%s type=%s title=%r",
                        row.tmdb_id, row.type, row.title,
                    )
                    continue

                logger.debug(
                    "cert=%r  tmdb_id=%s type=%s title=%r",
                    cert, row.tmdb_id, row.type, row.title,
                )
                if not dry_run:
                    await db.execute(
                        update(Media)
                        .where(
                            Media.rating_key == row.rating_key,
                            Media.server_id == row.server_id,
                        )
                        .values(content_rating=cert)
                    )
                stats.updated += 1
                committed_since_last += 1

                if not dry_run and committed_since_last >= _COMMIT_BATCH:
                    await db.commit()
                    committed_since_last = 0
                    logger.info(
                        "Progress: %d/%d fetched, %d updated, %d no-cert",
                        stats.fetched, total, stats.updated, stats.skipped_no_cert,
                    )

            offset += len(rows)

            if offset < total:
                await asyncio.sleep(_INTER_BATCH_SLEEP)

        # Final commit for any remainder.
        if not dry_run and committed_since_last > 0:
            await db.commit()

    logger.info(
        "Backfill complete: candidates=%d fetched=%d updated=%d "
        "skipped_no_cert=%d errors=%d pages=%d dry_run=%s",
        stats.candidates, stats.fetched, stats.updated,
        stats.skipped_no_cert, stats.errors, stats.pages, dry_run,
    )

    # Coverage summary.
    async with async_session_factory() as db:
        from sqlalchemy import func

        total_movies_shows = (
            await db.execute(
                select(func.count()).where(Media.type.in_(("movie", "show")))
            )
        ).scalar_one()

        rated = (
            await db.execute(
                select(func.count()).where(
                    and_(
                        Media.type.in_(("movie", "show")),
                        Media.content_rating.isnot(None),
                        Media.content_rating != "",
                        Media.content_rating != "+",
                    )
                )
            )
        ).scalar_one()

    pct = (rated / total_movies_shows * 100) if total_movies_shows else 0.0
    logger.info(
        "Coverage after run: %d / %d rows have content_rating (%.1f%%)",
        rated, total_movies_shows, pct,
    )
    stats.extra["coverage_pct"] = pct
    stats.extra["rated"] = rated
    stats.extra["total"] = total_movies_shows
    return stats


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Backfill TMDb age certifications into media.content_rating.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Print what would be updated without writing to the DB.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Cap the number of candidate rows processed (useful for smoke tests).",
    )
    args = parser.parse_args()
    asyncio.run(run(dry_run=args.dry_run, limit=args.limit))
