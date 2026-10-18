"""B1-10: `scrape_gateway_cache` (ADR 0009 D2.1..D2.5, D2.11).

Real file-backed SQLite (tmp_path), schema from `create_all`. Writes go
through `write_with_retry`; the real-lock behaviour has its own module
(`tests/test_scrape_cache_real_lock.py`).
"""
from __future__ import annotations

import logging

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.database import Base
from app.models.scrape_schemas import FamilyStats, SinceStart
from app.services import scrape_gateway_cache as cache
from app.services import scrape_keys
from app.services.scrape_keys import CacheKey, InvalidCacheKey

DAY = 86_400_000
T0 = 1_800_000_000_000


@pytest_asyncio.fixture
async def factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'gw.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


class _WarnHandler(logging.Handler):
    """Attached on the originating logger: `plexhub` has propagate=False once
    app.main is imported, so caplog (root) would not see the record."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def warnings_seen():
    logger = logging.getLogger("plexhub.scrape_gateway_cache")
    handler = _WarnHandler()
    logger.addHandler(handler)
    yield handler.records
    logger.removeHandler(handler)


def movie(tmdb_id: int = 603, lang: str = "fr-FR") -> CacheKey:
    return scrape_keys.movie_key(tmdb_id, lang)


def season(show: int, n: int, lang: str = "fr-FR") -> CacheKey:
    return scrape_keys.season_key(show, n, lang)


async def raw_rows(factory) -> list[tuple]:
    async with factory() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT family, key_version, language, cache_key, result, "
                    "schema_version, payload, fetched_at, expires_at "
                    "FROM scrape_cache ORDER BY family, key_version, language, cache_key"
                )
            )
        ).all()
    return [tuple(r) for r in rows]


async def insert_raw(factory, **over) -> None:
    row = {
        "family": "movie", "key_version": scrape_keys.KEY_VERSION, "language": "fr-FR",
        "cache_key": "movie|603", "result": "found", "schema_version": scrape_keys.SCHEMA_VERSION,
        "payload": '{"a":1}', "fetched_at": T0, "expires_at": T0 + DAY,
    }
    row.update(over)
    async with factory() as s:
        await s.execute(
            text(
                "INSERT INTO scrape_cache VALUES (:family, :key_version, :language, :cache_key, "
                ":result, :schema_version, :payload, :fetched_at, :expires_at)"
            ),
            row,
        )
        await s.commit()


# --- compute_expires_at ----------------------------------------------------


@pytest.mark.parametrize("result", ["matched", "found"])
def test_expires_success_uses_success_ttl(result):
    assert cache.compute_expires_at(result, T0) == T0 + scrape_keys.ttl_success_ms()


@pytest.mark.parametrize("result", ["ambiguous", "nomatch", "not_found"])
def test_expires_negative_uses_negative_ttl(result):
    assert cache.compute_expires_at(result, T0) == T0 + scrape_keys.ttl_negative_ms()


def test_expires_unknown_result_raises():
    with pytest.raises(ValueError):
        cache.compute_expires_at("weird", T0)


# --- get -------------------------------------------------------------------


async def test_get_absent_returns_none(factory):
    assert await cache.get(movie(), session_factory=factory, now=T0) is None


async def test_put_then_get_round_trip(factory):
    entry = await cache.put(movie(), "found", {"title": "Matrix", "ä": "é"}, T0, session_factory=factory)
    got = await cache.get(movie(), session_factory=factory, now=T0 + 1)
    assert got == entry
    assert got.payload == {"title": "Matrix", "ä": "é"}
    assert got.expires_at == T0 + scrape_keys.ttl_success_ms()


async def test_get_expiry_boundary(factory):
    entry = await cache.put(movie(), "found", {"x": 1}, T0, session_factory=factory)
    assert await cache.get(movie(), session_factory=factory, now=entry.expires_at - 1) is not None
    assert await cache.get(movie(), session_factory=factory, now=entry.expires_at) is None
    assert await cache.get(movie(), session_factory=factory, now=entry.expires_at + 1) is None


async def test_get_ignores_other_key_version(factory):
    other = CacheKey("movie", scrape_keys.KEY_VERSION + 1, "fr-FR", "movie|603")
    await insert_raw(factory, key_version=other.key_version)
    assert await cache.get(other, session_factory=factory, now=T0) is None
    # And the current-version key does not see the foreign-version row either.
    assert await cache.get(movie(), session_factory=factory, now=T0) is None


async def test_get_ignores_other_schema_version(factory):
    await insert_raw(factory, schema_version=scrape_keys.SCHEMA_VERSION + 1)
    assert await cache.get(movie(), session_factory=factory, now=T0) is None


async def test_get_follows_a_bumped_key_version(factory, monkeypatch):
    await cache.put(movie(), "found", {"x": 1}, T0, session_factory=factory)
    monkeypatch.setattr(scrape_keys, "KEY_VERSION", scrape_keys.KEY_VERSION + 1)
    assert await cache.get(movie(), session_factory=factory, now=T0 + 1) is None


@pytest.mark.parametrize("raw", ["{not json", "[1,2]", "null", None])
async def test_get_unreadable_payload_is_absent_with_warning(factory, warnings_seen, raw):
    await insert_raw(factory, payload=raw)
    assert await cache.get(movie(), session_factory=factory, now=T0) is None
    assert len(warnings_seen) == 1
    message = warnings_seen[0].getMessage()
    assert "movie" in message  # the family
    assert "603" not in message and "{not json" not in message  # no key, no content


async def test_get_corrupt_match_warning_does_not_leak_the_title(factory, warnings_seen):
    key = scrape_keys.match_key("movie", "Le Titre Secret", 1999, "fr-FR")
    await insert_raw(factory, family="match", cache_key=key.key, result="matched", payload="}{")
    assert await cache.get(key, session_factory=factory, now=T0) is None
    assert len(warnings_seen) == 1
    assert "secret" not in warnings_seen[0].getMessage().lower()


async def test_get_negative_result_has_no_payload(factory):
    key = scrape_keys.match_key("movie", "Inconnu", None, "fr-FR")
    await cache.put(key, "nomatch", None, T0, session_factory=factory)
    got = await cache.get(key, session_factory=factory, now=T0 + 1)
    assert got is not None and got.result == "nomatch" and got.payload is None


async def test_get_closes_its_session_before_returning(factory):
    opened, closed = [], []

    def tracking_factory():
        s = factory()
        opened.append(s)
        original = s.close

        async def _close():
            closed.append(s)
            await original()

        s.close = _close
        return s

    await cache.get(movie(), session_factory=tracking_factory, now=T0)
    assert len(opened) == 1 and closed == opened


# --- put / put_many --------------------------------------------------------


async def test_put_stores_the_exact_row(factory):
    await cache.put(movie(), "found", {"b": 2, "a": "é"}, T0, session_factory=factory)
    (row,) = await raw_rows(factory)
    assert row == (
        "movie", scrape_keys.KEY_VERSION, "fr-FR", "movie|603", "found",
        scrape_keys.SCHEMA_VERSION, '{"a":"é","b":2}', T0, T0 + scrape_keys.ttl_success_ms(),
    )


async def test_upsert_replaces_the_entry(factory):
    await cache.put(movie(), "found", {"v": 1}, T0, session_factory=factory)
    await cache.put(movie(), "found", {"v": 2}, T0 + 5 * DAY, session_factory=factory)
    rows = await raw_rows(factory)
    assert len(rows) == 1
    assert rows[0][6] == '{"v":2}' and rows[0][7] == T0 + 5 * DAY


async def test_upsert_can_turn_a_negative_into_a_success(factory):
    key = scrape_keys.match_key("movie", "Matrix", 1999, "fr-FR")
    await cache.put(key, "nomatch", None, T0, session_factory=factory)
    await cache.put(key, "matched", {"confidence": 0.9, "tmdbId": 603}, T0 + 1, session_factory=factory)
    got = await cache.get(key, session_factory=factory, now=T0 + 2)
    assert got.result == "matched" and got.payload["tmdbId"] == 603


@pytest.mark.parametrize("result", ["nomatch", "not_found"])
async def test_negative_with_payload_is_refused(factory, result):
    with pytest.raises(ValueError):
        await cache.put(movie(), result, {"x": 1}, T0, session_factory=factory)
    assert await raw_rows(factory) == []


@pytest.mark.parametrize("result", ["matched", "found", "ambiguous"])
async def test_non_negative_without_payload_is_refused(factory, result):
    with pytest.raises(ValueError):
        await cache.put(movie(), result, None, T0, session_factory=factory)


async def test_unknown_result_is_refused(factory):
    with pytest.raises(ValueError):
        await cache.put(movie(), "weird", {"x": 1}, T0, session_factory=factory)


async def test_put_many_is_all_or_nothing_on_an_invalid_entry(factory):
    good = (movie(603), "found", {"x": 1}, T0)
    bad = (movie(604), "nomatch", {"oops": True}, T0)
    with pytest.raises(ValueError):
        await cache.put_many([good, bad], session_factory=factory)
    assert await raw_rows(factory) == []


async def test_put_many_is_all_or_nothing_on_a_database_failure(factory):
    """A row that violates a NOT NULL constraint at execution time rolls the
    whole transaction back (the valid sibling is not left behind)."""
    async with factory() as s:
        await s.execute(text("DROP TABLE scrape_cache"))
        await s.execute(
            text(
                "CREATE TABLE scrape_cache (family TEXT NOT NULL, key_version INTEGER NOT NULL, "
                "language TEXT NOT NULL, cache_key TEXT NOT NULL, result TEXT NOT NULL, "
                "schema_version INTEGER NOT NULL, payload TEXT, fetched_at INTEGER NOT NULL, "
                "expires_at INTEGER NOT NULL CHECK (expires_at < 0), "
                "PRIMARY KEY (family, key_version, language, cache_key))"
            )
        )
        await s.commit()
    with pytest.raises(Exception):
        await cache.put_many(
            [(movie(603), "found", {"x": 1}, T0), (movie(604), "found", {"x": 2}, T0)],
            session_factory=factory,
        )
    assert await raw_rows(factory) == []


async def test_put_many_writes_match_and_details_together(factory):
    match = scrape_keys.match_key("movie", "Matrix", 1999, "fr-FR")
    entries = [
        (match, "matched", {"confidence": 0.95, "tmdbId": 603}, T0),
        (movie(603), "found", {"title": "The Matrix"}, T0),
    ]
    out = await cache.put_many(entries, session_factory=factory)
    assert [e.key for e in out] == [match, movie(603)]
    assert {r[0] for r in await raw_rows(factory)} == {"match", "movie"}


async def test_put_many_empty_is_a_noop(factory):
    assert await cache.put_many([], session_factory=factory) == []


async def test_put_many_rejects_a_non_integer_fetched_at(factory):
    with pytest.raises(ValueError):
        await cache.put(movie(), "found", {"x": 1}, 1.5, session_factory=factory)


# --- stats -----------------------------------------------------------------


async def test_stats_exact_counts_and_pydantic_validation(factory):
    now = T0 + 10 * DAY
    # movie v1: 2 found (one expired), 1 not_found (expired)
    await insert_raw(factory, cache_key="movie|1", result="found", fetched_at=T0, expires_at=now + 1)
    await insert_raw(factory, cache_key="movie|2", result="found", fetched_at=T0 + 1, expires_at=now)
    await insert_raw(factory, cache_key="movie|3", result="not_found", payload=None,
                     fetched_at=T0 + 2, expires_at=now - 5)
    # match v1: matched, ambiguous (expired), nomatch
    await insert_raw(factory, family="match", cache_key="movie|a|2000", result="matched",
                     fetched_at=T0 + 3, expires_at=now + 100)
    await insert_raw(factory, family="match", cache_key="movie|b|2000", result="ambiguous",
                     fetched_at=T0 + 4, expires_at=now - 1)
    await insert_raw(factory, family="match", cache_key="movie|c|2000", result="nomatch",
                     payload=None, fetched_at=T0 + 5, expires_at=now + 100)
    # movie of a foreign key version
    await insert_raw(factory, key_version=2, cache_key="movie|1", result="found",
                     fetched_at=T0 + 9, expires_at=now + 1)

    out = await cache.stats(session_factory=factory, now=now)
    parsed = [FamilyStats.model_validate(d) for d in out]

    assert [(d["family"], d["key_version"]) for d in out] == [
        ("match", 1), ("movie", 1), ("movie", 2),
    ]
    by = {(f.family, f.key_version): f for f in parsed}
    assert by[("match", 1)].model_dump() == dict(
        family="match", key_version=1, entries=3, positive=1, ambiguous=1, negative=1,
        expired=1, oldest_fetched_at=T0 + 3, newest_fetched_at=T0 + 5,
    )
    assert by[("movie", 1)].model_dump() == dict(
        family="movie", key_version=1, entries=3, positive=2, ambiguous=0, negative=1,
        expired=2, oldest_fetched_at=T0, newest_fetched_at=T0 + 2,
    )
    assert by[("movie", 2)].entries == 1 and by[("movie", 2)].expired == 0


async def test_stats_empty_table(factory):
    assert await cache.stats(session_factory=factory, now=T0) == []


async def test_stats_expired_is_a_subset_of_entries(factory):
    await insert_raw(factory, cache_key="movie|1", expires_at=T0 - 1)
    (d,) = await cache.stats(session_factory=factory, now=T0)
    f = FamilyStats.model_validate(d)
    assert f.entries == 1 and f.expired == 1 and f.positive == 1


# --- invalidate_keys -------------------------------------------------------


async def test_invalidate_keys_deletes_and_counts(factory):
    await cache.put_many(
        [(movie(1), "found", {"x": 1}, T0), (movie(2), "found", {"x": 2}, T0),
         (movie(3), "found", {"x": 3}, T0)],
        session_factory=factory,
    )
    n = await cache.invalidate_keys([movie(1).to_text(), movie(2).to_text()], session_factory=factory)
    assert n == 2
    assert [r[3] for r in await raw_rows(factory)] == ["movie|3"]


async def test_invalidate_keys_is_all_or_nothing_on_a_malformed_key(factory):
    await cache.put(movie(1), "found", {"x": 1}, T0, session_factory=factory)
    with pytest.raises(InvalidCacheKey):
        await cache.invalidate_keys([movie(1).to_text(), "garbage"], session_factory=factory)
    assert len(await raw_rows(factory)) == 1


async def test_invalidate_keys_is_idempotent(factory):
    await cache.put(movie(1), "found", {"x": 1}, T0, session_factory=factory)
    text_key = movie(1).to_text()
    assert await cache.invalidate_keys([text_key], session_factory=factory) == 1
    assert await cache.invalidate_keys([text_key], session_factory=factory) == 0


async def test_invalidate_keys_ignores_duplicates_and_respects_language_and_version(factory):
    await cache.put_many(
        [(movie(1, "fr-FR"), "found", {"x": 1}, T0), (movie(1, "en-US"), "found", {"x": 1}, T0)],
        session_factory=factory,
    )
    await insert_raw(factory, key_version=2)  # movie|603 v2, untouched
    n = await cache.invalidate_keys(
        [movie(1, "fr-FR").to_text(), movie(1, "fr-FR").to_text()], session_factory=factory
    )
    assert n == 1
    assert len(await raw_rows(factory)) == 2


async def test_invalidate_keys_handles_a_key_with_a_title_containing_wildcards(factory):
    key = scrape_keys.match_key("movie", "100% Wolf_1", 2020, "fr-FR")
    other = scrape_keys.match_key("movie", "100x Wolfx1", 2020, "fr-FR")
    await cache.put_many(
        [(key, "nomatch", None, T0), (other, "nomatch", None, T0)], session_factory=factory
    )
    assert await cache.invalidate_keys([key.to_text()], session_factory=factory) == 1
    assert len(await raw_rows(factory)) == 1


async def test_invalidate_keys_empty_list(factory):
    assert await cache.invalidate_keys([], session_factory=factory) == 0


async def test_invalidate_keys_more_than_one_chunk(factory):
    ids = list(range(1, 451))
    await cache.put_many([(movie(i), "found", {"i": i}, T0) for i in ids], session_factory=factory)
    n = await cache.invalidate_keys([movie(i).to_text() for i in ids], session_factory=factory)
    assert n == 450 and await raw_rows(factory) == []


# --- invalidate_family -----------------------------------------------------


async def test_invalidate_family_season_all_languages_not_other_shows(factory):
    await cache.put_many(
        [
            (season(1396, 0), "found", {"s": 0}, T0),
            (season(1396, 1), "found", {"s": 1}, T0),
            (season(1396, 1, "en-US"), "found", {"s": 1}, T0),
            (season(13960, 1), "found", {"s": 1}, T0),
            (season(139, 1), "found", {"s": 1}, T0),
            (scrape_keys.show_key(1396, "fr-FR"), "found", {"t": 1}, T0),
        ],
        session_factory=factory,
    )
    await insert_raw(factory, family="season", key_version=2, cache_key="season|1396|4")
    n = await cache.invalidate_family("season", 1396, session_factory=factory)
    assert n == 4  # 3 current-version + 1 foreign-version
    left = sorted(r[3] for r in await raw_rows(factory))
    assert left == ["season|13960|1", "season|139|1", "show|1396"]


@pytest.mark.parametrize("family", ["movie", "show"])
async def test_invalidate_family_movie_show_exact_all_languages(family, factory):
    k = scrape_keys.movie_key if family == "movie" else scrape_keys.show_key
    await cache.put_many(
        [
            (k(603, "fr-FR"), "found", {"x": 1}, T0),
            (k(603, "en-US"), "found", {"x": 1}, T0),
            (k(6030, "fr-FR"), "found", {"x": 1}, T0),
        ],
        session_factory=factory,
    )
    assert await cache.invalidate_family(family, 603, session_factory=factory) == 2
    assert [r[3] for r in await raw_rows(factory)] == [f"{family}|6030"]


async def test_invalidate_family_leaves_match_entries(factory):
    key = scrape_keys.match_key("movie", "Matrix", 1999, "fr-FR")
    await cache.put_many(
        [(key, "matched", {"confidence": 1.0, "tmdbId": 603}, T0), (movie(603), "found", {"x": 1}, T0)],
        session_factory=factory,
    )
    assert await cache.invalidate_family("movie", 603, session_factory=factory) == 1
    assert [r[0] for r in await raw_rows(factory)] == ["match"]


async def test_invalidate_family_absent_is_zero(factory):
    assert await cache.invalidate_family("show", 42, session_factory=factory) == 0


@pytest.mark.parametrize("family,tmdb_id", [("omdb", 1), ("movie", 0), ("movie", -3), ("movie", True)])
async def test_invalidate_family_rejects_bad_arguments(factory, family, tmdb_id):
    with pytest.raises(ValueError):
        await cache.invalidate_family(family, tmdb_id, session_factory=factory)


# --- purge -----------------------------------------------------------------


async def seed_purge(factory, expired: int, live: int) -> None:
    for i in range(expired):
        await insert_raw(factory, cache_key=f"movie|{i + 1}", expires_at=T0 - 1 - i)
    for i in range(live):
        await insert_raw(factory, cache_key=f"movie|{1000 + i}", expires_at=T0 + DAY)


async def test_purge_is_bounded_and_reports_the_remaining(factory):
    await seed_purge(factory, expired=5, live=3)
    deleted, remaining = await cache.purge(2, now=T0, session_factory=factory)
    assert (deleted, remaining) == (2, 3)
    deleted, remaining = await cache.purge(10, now=T0, session_factory=factory)
    assert (deleted, remaining) == (3, 0)
    assert len(await raw_rows(factory)) == 3  # live entries untouched
    assert await cache.purge(10, now=T0, session_factory=factory) == (0, 0)


async def test_purge_expiry_boundary(factory):
    await insert_raw(factory, cache_key="movie|1", expires_at=T0)       # expired (<=)
    await insert_raw(factory, cache_key="movie|2", expires_at=T0 + 1)   # alive
    assert await cache.purge(10, now=T0, session_factory=factory) == (1, 0)
    assert [r[3] for r in await raw_rows(factory)] == ["movie|2"]


async def test_purge_removes_obsolete_versions_even_if_not_expired(factory):
    await insert_raw(factory, cache_key="movie|1", key_version=2, expires_at=T0 + DAY)
    await insert_raw(factory, cache_key="movie|2", schema_version=2, expires_at=T0 + DAY)
    await insert_raw(factory, cache_key="movie|3", expires_at=T0 + DAY)
    assert await cache.purge(10, now=T0, session_factory=factory) == (2, 0)
    assert [r[3] for r in await raw_rows(factory)] == ["movie|3"]


@pytest.mark.parametrize("limit", [0, -1, 100_001, True, 1.5, "5"])
async def test_purge_rejects_a_limit_out_of_range(factory, limit):
    with pytest.raises(ValueError):
        await cache.purge(limit, now=T0, session_factory=factory)


async def test_purge_accepts_both_bounds(factory):
    await seed_purge(factory, expired=1, live=0)
    assert await cache.purge(1, now=T0, session_factory=factory) == (1, 0)
    assert await cache.purge(100_000, now=T0, session_factory=factory) == (0, 0)


# --- counters --------------------------------------------------------------


def test_counters_snapshot_matches_since_start_and_reset():
    c = cache.counters
    c.hit()
    c.hit()
    c.miss()
    c.coalesce()
    c.upstream_error()
    snap = c.snapshot()
    assert snap == {"hits": 2, "misses": 1, "coalesced": 1, "upstream_errors": 1}
    assert SinceStart.model_validate(snap).hits == 2
    c.reset()
    assert c.snapshot() == {"hits": 0, "misses": 0, "coalesced": 0, "upstream_errors": 0}


def test_counters_are_reset_between_tests_first():
    assert cache.counters.snapshot()["hits"] == 0


def test_counters_are_reset_between_tests_second():
    cache.counters.hit()
    assert cache.counters.snapshot()["hits"] == 1
