"""Versioned persistent cache of the scrape gateway (lot B1, ticket B1-10).

ADR 0009 D2.1..D2.5 and D2.11, PRD FR-011/FR-013/FR-014/FR-029..FR-031.

One table (`scrape_cache`, ORM `ScrapeCacheEntry`) for every family. This
module is the only reader/writer of that table; it knows nothing about TMDB,
OMDb, single-flight or HTTP (those live in `scrape_gateway_service`, B1-11).

Rules inherited from CLAUDE.md section 9, trap 8 (do not undo):

- Every write goes through `write_with_retry` (a FRESH session per attempt).
  Never `commit_with_retry`, never a `rollback()` followed by a retry on the
  same session (pending objects are expelled: a silent loss of the write).
- The caller writes AFTER the provider call: no transaction is ever open
  while the network is busy.
- A read opens a short session and closes it before returning.

Logs never carry a payload, a title or a key (a `match` key embeds the
normalized title): only the family and the reason.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import and_, case, delete, func, select, text, tuple_
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import async_session_factory
from app.models.database import ScrapeCacheEntry
from app.services import scrape_keys
from app.services.scrape_keys import CacheKey
from app.utils.db_retry import write_with_retry
from app.utils.time import now_ms

logger = logging.getLogger("plexhub.scrape_gateway_cache")

SessionFactory = Callable[[], AsyncSession]

# Result classes (ADR 0009 D2.3/D2.4).
POSITIVE_RESULTS = ("matched", "found")
AMBIGUOUS_RESULTS = ("ambiguous",)
NEGATIVE_RESULTS = ("nomatch", "not_found")
KNOWN_RESULTS = POSITIVE_RESULTS + AMBIGUOUS_RESULTS + NEGATIVE_RESULTS
# Results that never carry a payload (D2.4); every other one must.
PAYLOADLESS_RESULTS = NEGATIVE_RESULTS

PURGE_LIMIT_RANGE = (1, 100_000)

# Chunking of IN lists: 4 bound parameters per key, far under SQLite's limit.
_INVALIDATE_CHUNK = 200


# --- Process-local counters (`sinceStart`, ADR 0009 D2.11) -------------------


class GatewayCounters:
    """In-memory counters of the gateway since process start.

    The caller (`scrape_gateway_service`) increments them; this module only
    holds them. Single uvicorn process (CLAUDE.md section 9, traps 7 and 18c):
    plain integers are enough, no lock."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.hits = 0
        self.misses = 0
        self.coalesced = 0
        self.upstream_errors = 0

    def hit(self) -> None:
        self.hits += 1

    def miss(self) -> None:
        self.misses += 1

    def coalesce(self) -> None:
        self.coalesced += 1

    def upstream_error(self) -> None:
        self.upstream_errors += 1

    def snapshot(self) -> dict[str, int]:
        """Keys match `SinceStart` (`app/models/scrape_schemas.py`)."""
        return {
            "hits": self.hits,
            "misses": self.misses,
            "coalesced": self.coalesced,
            "upstream_errors": self.upstream_errors,
        }


counters = GatewayCounters()


# --- Entries ---------------------------------------------------------------


@dataclass(frozen=True)
class CachedEntry:
    key: CacheKey
    result: str
    payload: dict | None
    fetched_at: int
    expires_at: int


def compute_expires_at(result: str, fetched_at: int) -> int:
    """`fetched_at` + success TTL (matched/found) or negative TTL
    (ambiguous/nomatch/not_found), both bounded by `scrape_keys` (D2.3)."""
    if result in POSITIVE_RESULTS:
        return fetched_at + scrape_keys.ttl_success_ms()
    if result in AMBIGUOUS_RESULTS or result in NEGATIVE_RESULTS:
        return fetched_at + scrape_keys.ttl_negative_ms()
    raise ValueError(f"unknown cache result: {result!r}")


def _dump_payload(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _decode_payload(result: str, raw: str | None) -> tuple[bool, dict | None]:
    """(ok, payload). A payloadless result tolerates a stray payload by
    ignoring it; any other result needs a JSON object."""
    if result in PAYLOADLESS_RESULTS:
        return True, None
    if raw is None:
        return False, None
    try:
        decoded = json.loads(raw)
    except ValueError:
        return False, None
    if not isinstance(decoded, dict):
        return False, None
    return True, decoded


# --- Read ------------------------------------------------------------------


async def get(
    key: CacheKey,
    *,
    session_factory: SessionFactory = async_session_factory,
    now: int | None = None,
) -> CachedEntry | None:
    """The entry for `key` if it is still servable, else None.

    Servable = `expires_at > now`, `key_version == KEY_VERSION` and
    `schema_version == SCHEMA_VERSION`. An unreadable stored payload is
    treated as absent (WARNING with the family only). The session is closed
    before returning."""
    if key.key_version != scrape_keys.KEY_VERSION:
        return None
    current = now_ms() if now is None else now

    async with session_factory() as session:
        row = (
            await session.execute(
                select(
                    ScrapeCacheEntry.result,
                    ScrapeCacheEntry.payload,
                    ScrapeCacheEntry.fetched_at,
                    ScrapeCacheEntry.expires_at,
                ).where(
                    ScrapeCacheEntry.family == key.family,
                    ScrapeCacheEntry.key_version == key.key_version,
                    ScrapeCacheEntry.language == key.language,
                    ScrapeCacheEntry.cache_key == key.key,
                    ScrapeCacheEntry.expires_at > current,
                    ScrapeCacheEntry.schema_version == scrape_keys.SCHEMA_VERSION,
                )
            )
        ).first()
    if row is None:
        return None

    result, raw, fetched_at, expires_at = row
    if result not in KNOWN_RESULTS:
        logger.warning("scrape cache: unknown result stored, entry ignored (family=%s)", key.family)
        return None
    ok, payload = _decode_payload(result, raw)
    if not ok:
        logger.warning("scrape cache: unreadable payload, entry ignored (family=%s)", key.family)
        return None
    return CachedEntry(
        key=key, result=result, payload=payload, fetched_at=fetched_at, expires_at=expires_at
    )


# --- Write -----------------------------------------------------------------

PutItem = tuple[CacheKey, str, dict | None, int]


def _prepare_rows(entries: Sequence[PutItem]) -> tuple[list[dict[str, Any]], list[CachedEntry]]:
    """Validate EVERY entry and build the rows: any invalid entry raises
    ValueError before the database is touched (all or nothing)."""
    rows: list[dict[str, Any]] = []
    cached: list[CachedEntry] = []
    for key, result, payload, fetched_at in entries:
        if result not in KNOWN_RESULTS:
            raise ValueError(f"unknown cache result: {result!r}")
        if result in PAYLOADLESS_RESULTS:
            if payload is not None:
                raise ValueError(f"result {result!r} must not carry a payload")
        elif payload is None:
            raise ValueError(f"result {result!r} requires a payload")
        elif not isinstance(payload, dict):
            raise ValueError("payload must be a JSON object")
        if isinstance(fetched_at, bool) or not isinstance(fetched_at, int):
            raise ValueError("fetched_at must be an integer (epoch ms)")
        expires_at = compute_expires_at(result, fetched_at)
        rows.append(
            {
                "family": key.family,
                "key_version": key.key_version,
                "language": key.language,
                "cache_key": key.key,
                "result": result,
                "schema_version": scrape_keys.SCHEMA_VERSION,
                "payload": None if payload is None else _dump_payload(payload),
                "fetched_at": fetched_at,
                "expires_at": expires_at,
            }
        )
        cached.append(
            CachedEntry(
                key=key, result=result, payload=payload, fetched_at=fetched_at, expires_at=expires_at
            )
        )
    return rows, cached


async def put_many(
    entries: Sequence[PutItem],
    *,
    session_factory: SessionFactory = async_session_factory,
) -> list[CachedEntry]:
    """Upsert every entry in ONE transaction (all or nothing).

    Used to write a `match` entry and its `movie|show` details together
    (ADR 0009 D2.5). To be called AFTER the provider call, with no
    transaction open elsewhere."""
    rows, cached = _prepare_rows(entries)
    if not rows:
        return []

    async def _work(session: AsyncSession) -> None:
        stmt = sqlite_insert(ScrapeCacheEntry)
        stmt = stmt.on_conflict_do_update(
            index_elements=["family", "key_version", "language", "cache_key"],
            set_={
                "result": stmt.excluded.result,
                "schema_version": stmt.excluded.schema_version,
                "payload": stmt.excluded.payload,
                "fetched_at": stmt.excluded.fetched_at,
                "expires_at": stmt.excluded.expires_at,
            },
        )
        await session.execute(stmt, rows)
        await session.commit()

    await write_with_retry(_work, session_factory=session_factory, op="scrape_cache.put")
    return cached


async def put(
    key: CacheKey,
    result: str,
    payload: dict | None,
    fetched_at: int,
    *,
    session_factory: SessionFactory = async_session_factory,
) -> CachedEntry:
    (entry,) = await put_many([(key, result, payload, fetched_at)], session_factory=session_factory)
    return entry


# --- Stats -----------------------------------------------------------------


async def stats(
    *,
    session_factory: SessionFactory = async_session_factory,
    now: int | None = None,
) -> list[dict[str, Any]]:
    """One `GROUP BY family, key_version` query (ADR 0009 D2.11).

    Each dict validates as `FamilyStats` (snake_case keys, accepted through
    `populate_by_name`). `entries = positive + ambiguous + negative`;
    `expired` is the subset of `entries` with `expires_at <= now`."""
    current = now_ms() if now is None else now
    known = ScrapeCacheEntry.result.in_(KNOWN_RESULTS)

    def _count(condition) -> Any:
        return func.coalesce(func.sum(case((condition, 1), else_=0)), 0)

    stmt = (
        select(
            ScrapeCacheEntry.family,
            ScrapeCacheEntry.key_version,
            _count(ScrapeCacheEntry.result.in_(POSITIVE_RESULTS)).label("positive"),
            _count(ScrapeCacheEntry.result.in_(AMBIGUOUS_RESULTS)).label("ambiguous"),
            _count(ScrapeCacheEntry.result.in_(NEGATIVE_RESULTS)).label("negative"),
            _count(ScrapeCacheEntry.expires_at <= current).label("expired"),
            func.min(ScrapeCacheEntry.fetched_at).label("oldest"),
            func.max(ScrapeCacheEntry.fetched_at).label("newest"),
        )
        .where(known)
        .group_by(ScrapeCacheEntry.family, ScrapeCacheEntry.key_version)
        .order_by(ScrapeCacheEntry.family, ScrapeCacheEntry.key_version)
    )
    async with session_factory() as session:
        rows = (await session.execute(stmt)).all()

    return [
        {
            "family": r.family,
            "key_version": r.key_version,
            "entries": r.positive + r.ambiguous + r.negative,
            "positive": r.positive,
            "ambiguous": r.ambiguous,
            "negative": r.negative,
            "expired": r.expired,
            "oldest_fetched_at": r.oldest,
            "newest_fetched_at": r.newest,
        }
        for r in rows
    ]


# --- Invalidation ----------------------------------------------------------


async def invalidate_keys(
    texts: Sequence[str],
    *,
    session_factory: SessionFactory = async_session_factory,
) -> int:
    """Delete the entries named by their text form, all or nothing.

    Every key is parsed BEFORE anything is deleted: one malformed key raises
    `InvalidCacheKey` (a ValueError) and nothing is removed. One transaction;
    idempotent (an absent key counts for 0)."""
    keys = {k.to_text(): k for k in (scrape_keys.parse(t) for t in texts)}.values()
    if not keys:
        return 0
    tuples = [(k.family, k.key_version, k.language, k.key) for k in keys]

    async def _work(session: AsyncSession) -> int:
        deleted = 0
        for start in range(0, len(tuples), _INVALIDATE_CHUNK):
            chunk = tuples[start : start + _INVALIDATE_CHUNK]
            result = await session.execute(
                delete(ScrapeCacheEntry).where(
                    tuple_(
                        ScrapeCacheEntry.family,
                        ScrapeCacheEntry.key_version,
                        ScrapeCacheEntry.language,
                        ScrapeCacheEntry.cache_key,
                    ).in_(chunk)
                )
            )
            deleted += result.rowcount or 0
        await session.commit()
        return deleted

    return await write_with_retry(_work, session_factory=session_factory, op="scrape_cache.invalidate_keys")


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def invalidate_family(
    family: Literal["movie", "show", "season"],
    tmdb_id: int,
    *,
    session_factory: SessionFactory = async_session_factory,
) -> int:
    """Delete `movie|{id}` / `show|{id}` (exact) or `season|{id}|%`, all
    languages and key versions. `match` entries are left alone (ADR 0009
    D2.11: they only carry the tmdbId)."""
    if family not in ("movie", "show", "season"):
        raise ValueError(f"unsupported family: {family!r}")
    if isinstance(tmdb_id, bool) or not isinstance(tmdb_id, int) or tmdb_id < 1:
        raise ValueError("tmdb_id must be a positive integer")

    if family == "season":
        # The id is an integer (no wildcard in it); the escaping is kept so
        # the pattern stays correct whatever feeds it.
        condition = and_(
            ScrapeCacheEntry.family == "season",
            ScrapeCacheEntry.cache_key.like(f"season|{_escape_like(str(tmdb_id))}|%", escape="\\"),
        )
    else:
        condition = and_(
            ScrapeCacheEntry.family == family,
            ScrapeCacheEntry.cache_key == f"{family}|{tmdb_id}",
        )

    async def _work(session: AsyncSession) -> int:
        result = await session.execute(delete(ScrapeCacheEntry).where(condition))
        await session.commit()
        return result.rowcount or 0

    return await write_with_retry(_work, session_factory=session_factory, op="scrape_cache.invalidate_family")


# --- Purge -----------------------------------------------------------------

_PURGEABLE = (
    "expires_at <= :now OR key_version != :kv OR schema_version != :sv"
)


async def purge(
    limit: int,
    *,
    now: int | None = None,
    session_factory: SessionFactory = async_session_factory,
) -> tuple[int, int]:
    """Delete at most `limit` purgeable rows (expired, or of another key or
    schema version); return `(deleted, remaining_purgeable)`.

    One short transaction. The bounded `rowid IN (SELECT ... LIMIT)` keeps a
    huge backlog from holding the single SQLite writer for long."""
    low, high = PURGE_LIMIT_RANGE
    if isinstance(limit, bool) or not isinstance(limit, int) or not low <= limit <= high:
        raise ValueError(f"limit must be an integer between {low} and {high}")
    params = {
        "now": now_ms() if now is None else now,
        "kv": scrape_keys.KEY_VERSION,
        "sv": scrape_keys.SCHEMA_VERSION,
    }

    async def _work(session: AsyncSession) -> tuple[int, int]:
        deleted = (
            await session.execute(
                text(
                    "DELETE FROM scrape_cache WHERE rowid IN ("
                    f"SELECT rowid FROM scrape_cache WHERE {_PURGEABLE} LIMIT :limit)"
                ),
                {**params, "limit": limit},
            )
        ).rowcount or 0
        remaining = (
            await session.execute(
                text(f"SELECT COUNT(*) FROM scrape_cache WHERE {_PURGEABLE}"), params
            )
        ).scalar_one()
        await session.commit()
        return deleted, int(remaining)

    return await write_with_retry(_work, session_factory=session_factory, op="scrape_cache.purge")
