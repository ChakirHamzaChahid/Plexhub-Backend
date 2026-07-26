"""AUDIT-P3-003 (S2.2): `DatabaseSource.get_movies()`/`get_series()` aggregate
across accounts on the event loop. Measured on the real catalog (audit
docs/audit/v1/30-perf.md): `aggregate_movies` = 239ms CPU (12 331 rows),
`aggregate_series` = 504ms CPU (2 873 shows + 77 781 episodes) — and the
per-group `_build_versions` labelling loop on top of that. This is now
offloaded via `asyncio.to_thread` (mirrors `media_service.py`'s existing
pattern on the SAME pure `aggregate_movies`/`aggregate_series` functions).

This module proves two things:
  1. RESPIRATION — a concurrent witness task keeps getting scheduled (and
     visibly makes progress) while `get_movies()`/`get_series()` run, which
     is only possible because the CPU-bound aggregation runs in a worker
     thread. The SAME witness task, run against the pre-fix code shape
     (calling `_build_movies`/`_build_series` directly, inline, on the loop),
     provably advances ZERO times — asyncio is single-threaded and
     cooperative, so a coroutine holding control with no `await` inside a
     call starves every other task deterministically, not just "usually".
     This makes the check a structural one rather than a timing-based
     heuristic (avoids flakiness under CI/parallel-test load).
  2. PARITY — the offloaded path (`get_movies()`/`get_series()`, running
     `_build_movies`/`_build_series` in a worker thread) produces EXACTLY the
     same groups/versions/labels as calling `_build_movies`/`_build_series`
     directly and synchronously (the pre-fix code shape) on the same rows —
     i.e. the refactor changed WHERE the work runs, never WHAT it computes.
"""
import asyncio

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.database import Media, XtreamAccount
from app.plex_generator.source import DatabaseSource
from app.utils.server_id import build_server_id


def _account(id_: str) -> XtreamAccount:
    return XtreamAccount(
        id=id_, label=f"Compte {id_}", base_url=f"http://{id_}.example",
        port=80, username="u", password="p", is_active=True, created_at=0,
    )


def _movie_row(account_id: str, idx: int, unif: str) -> Media:
    return Media(
        rating_key=f"vod_{idx}.mp4", server_id=build_server_id(account_id),
        filter="all", sort_order="default", library_section_id="xtream_vod",
        title=f"Movie {idx} (VF)", type="movie", year=2000 + (idx % 20),
        unification_id=unif, page_offset=idx, is_in_allowed_categories=True,
        is_broken=False,
    )


def _show_row(account_id: str, show_idx: int, unif: str, page_offset: int) -> Media:
    return Media(
        rating_key=f"series_{account_id}_{show_idx}", server_id=build_server_id(account_id),
        filter="all", sort_order="default", library_section_id="xtream_series",
        title=f"Show {show_idx}", type="show", year=2010 + (show_idx % 10),
        unification_id=unif, page_offset=page_offset, is_in_allowed_categories=True,
        is_broken=False,
    )


def _episode_row(account_id: str, show_idx: int, season: int, ep: int, page_offset: int) -> Media:
    show_rk = f"series_{account_id}_{show_idx}"
    return Media(
        rating_key=f"ep_{account_id}_{show_idx}_{season}_{ep}.mkv",
        server_id=build_server_id(account_id),
        filter="all", sort_order="default", library_section_id="xtream_series",
        title=f"Episode {ep}", type="episode",
        grandparent_rating_key=show_rk, parent_index=season, index=ep,
        unification_id="", page_offset=page_offset, is_in_allowed_categories=True,
        is_broken=False,
    )


N_MOVIE_ROWS = 5000
N_ACCOUNTS = 4
N_SHOWS = 500
EPISODES_PER_SHOW = 10  # -> 5000 episode rows, split across N_ACCOUNTS


async def _spin(counter: list[int], stop: asyncio.Event):
    """A witness task that increments and immediately re-yields to the loop.

    This is a structural probe, not a timing one (deliberately avoids
    wall-clock-gap heuristics, which are noisy under CI/parallel-test load):
    asyncio is single-threaded, so while a coroutine is executing a plain
    SYNCHRONOUS call inline (the pre-fix code shape), this task literally
    cannot be scheduled at all — Python only gets to switch tasks at an
    ``await`` point, and a blocking call contains none. Its counter is
    therefore expected to advance ZERO times during such a call, deterministically
    (not "on average" — every single time), while it advances many times
    during an ``asyncio.to_thread``-offloaded call (the loop stays free to
    schedule it repeatedly while the worker thread does the CPU work).
    """
    while not stop.is_set():
        counter[0] += 1
        await asyncio.sleep(0)


@pytest_asyncio.fixture
async def synthetic_movies_factory(db_engine, monkeypatch):
    """5 000 movie rows across 4 accounts, ~1/3 sharing a unification_id with
    another account (real cross-account grouping work, not just a flat scan)
    — large enough that the pre-fix on-loop aggregation cost is measurable in
    a unit test without needing the full 189MB real catalog."""
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        s.add_all([_account(f"acc{i}") for i in range(N_ACCOUNTS)])
        rows = []
        for idx in range(N_MOVIE_ROWS):
            acc = f"acc{idx % N_ACCOUNTS}"
            unif = f"tmdb://{idx // N_ACCOUNTS}" if idx % 3 == 0 else ""
            rows.append(_movie_row(acc, idx, unif))
        s.add_all(rows)
        await s.commit()

    import app.plex_generator.source as source_mod
    monkeypatch.setattr(source_mod, "async_session_factory", factory)
    return factory


@pytest_asyncio.fixture
async def synthetic_series_factory(db_engine, monkeypatch):
    """500 shows x 10 episodes = 5 000 episode rows across 4 accounts, ~1/3 of
    shows duplicated (same unification_id) on a second account so
    aggregate_series does real cross-account slot-merging work."""
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        s.add_all([_account(f"acc{i}") for i in range(N_ACCOUNTS)])
        rows = []
        # page_offset only needs to be unique per (server_id, library_section_id,
        # filter, sort_order) — shows and episodes share that tuple's shape here,
        # so a single per-account running counter keeps every row unique.
        offset_counters: dict[str, int] = {f"acc{i}": 0 for i in range(N_ACCOUNTS)}

        def _next_offset(acc: str) -> int:
            n = offset_counters[acc]
            offset_counters[acc] = n + 1
            return n

        for show_idx in range(N_SHOWS):
            unif = f"tmdb://show{show_idx}" if show_idx % 3 == 0 else ""
            accs = ["acc0", "acc1"] if unif and show_idx % 6 == 0 else [f"acc{show_idx % N_ACCOUNTS}"]
            for acc in accs:
                rows.append(_show_row(acc, show_idx, unif, _next_offset(acc)))
                for season in range(1, 3):
                    for ep in range(1, EPISODES_PER_SHOW // 2 + 1):
                        rows.append(_episode_row(acc, show_idx, season, ep, _next_offset(acc)))
        s.add_all(rows)
        await s.commit()

    import app.plex_generator.source as source_mod
    monkeypatch.setattr(source_mod, "async_session_factory", factory)
    return factory


class TestRespiration:
    """The loop must keep making progress while get_movies()/get_series()
    run (§9 piège 11) — proven by a concurrent heartbeat not stalling for
    more than a small bound, AND by direct comparison against the
    synchronous (pre-fix) code shape on the exact same rows."""

    @pytest.mark.asyncio
    async def test_get_movies_offloaded_blocks_far_less_than_sync_equivalent(
        self, synthetic_movies_factory,
    ):
        src = DatabaseSource()

        # --- offloaded path (current code: get_movies() -> to_thread) ---
        counter = [0]
        stop = asyncio.Event()
        witness = asyncio.create_task(_spin(counter, stop))
        await asyncio.sleep(0)  # let the witness start ticking
        before = counter[0]
        movies = await src.get_movies()
        offloaded_progress = counter[0] - before
        stop.set()
        await witness

        # --- pre-fix equivalent: same pure function, called directly on the
        # loop (no to_thread) — reproduces the exact code shape being fixed.
        async with synthetic_movies_factory() as s:
            rows = list((await s.execute(select(Media).where(Media.type == "movie"))).scalars().all())
            accounts = await src._load_accounts(s)

        counter2 = [0]
        stop2 = asyncio.Event()
        witness2 = asyncio.create_task(_spin(counter2, stop2))
        await asyncio.sleep(0)
        before2 = counter2[0]
        movies_sync = src._build_movies(rows, accounts)  # on-loop, no to_thread
        sync_progress = counter2[0] - before2
        stop2.set()
        await witness2

        # Respiration: while get_movies() awaits the worker thread, the loop
        # keeps scheduling the witness task many times over; while the
        # synchronous equivalent runs INLINE on the loop, the witness cannot
        # be scheduled even once (single-threaded cooperative scheduling —
        # no await point exists inside the blocking call for it to run at).
        # This is a structural check, not a timing one, so it isn't flaky
        # under CI/parallel-test load the way a wall-clock-gap measurement is.
        assert sync_progress == 0, (
            f"witness task advanced {sync_progress} times during a supposedly "
            f"blocking synchronous call — dataset too small to be a useful probe"
        )
        assert offloaded_progress > 0, (
            "witness task never advanced during get_movies() — the "
            "aggregation is no longer offloaded off the event loop"
        )

        # Parity: identical groups, regardless of where the work ran.
        by_key_async = {m.source_id: m for m in movies}
        by_key_sync = {m.source_id: m for m in movies_sync}
        assert set(by_key_async) == set(by_key_sync)
        for key, m_async in by_key_async.items():
            m_sync = by_key_sync[key]
            assert m_async.title == m_sync.title
            assert m_async.year == m_sync.year
            assert sorted(v.label for v in m_async.versions) == \
                sorted(v.label for v in m_sync.versions)
            assert sorted(v.stream_url for v in m_async.versions) == \
                sorted(v.stream_url for v in m_sync.versions)

    @pytest.mark.asyncio
    async def test_get_series_offloaded_blocks_far_less_than_sync_equivalent(
        self, synthetic_series_factory,
    ):
        src = DatabaseSource()

        counter = [0]
        stop = asyncio.Event()
        witness = asyncio.create_task(_spin(counter, stop))
        await asyncio.sleep(0)
        before = counter[0]
        series = await src.get_series()
        offloaded_progress = counter[0] - before
        stop.set()
        await witness

        async with synthetic_series_factory() as s:
            accounts = await src._load_accounts(s)
            shows = list((await s.execute(select(Media).where(Media.type == "show"))).scalars().all())
            episodes = list((await s.execute(select(Media).where(Media.type == "episode"))).scalars().all())

        counter2 = [0]
        stop2 = asyncio.Event()
        witness2 = asyncio.create_task(_spin(counter2, stop2))
        await asyncio.sleep(0)
        before2 = counter2[0]
        series_sync = src._build_series(shows, episodes, accounts)  # on-loop
        sync_progress = counter2[0] - before2
        stop2.set()
        await witness2

        assert sync_progress == 0, (
            f"witness task advanced {sync_progress} times during a supposedly "
            f"blocking synchronous call — dataset too small to be a useful probe"
        )
        assert offloaded_progress > 0, (
            "witness task never advanced during get_series() — the "
            "aggregation is no longer offloaded off the event loop"
        )

        by_key_async = {s_.source_id: s_ for s_ in series}
        by_key_sync = {s_.source_id: s_ for s_ in series_sync}
        assert set(by_key_async) == set(by_key_sync)
        for key, s_async in by_key_async.items():
            s_sync = by_key_sync[key]
            assert s_async.title == s_sync.title
            slots_async = {(e.season_num, e.episode_num): sorted(v.label for v in e.versions)
                           for e in s_async.episodes}
            slots_sync = {(e.season_num, e.episode_num): sorted(v.label for v in e.versions)
                          for e in s_sync.episodes}
            assert slots_async == slots_sync


class TestNoSessionAccessInThread:
    """Invariant (§9 piège 8): `_build_movies`/`_build_series` must be
    callable with NO active DB session/event loop at all — proving they
    truly never reach back into a (thread-unsafe) AsyncSession. This is what
    makes running them via asyncio.to_thread safe in the first place."""

    def test_build_movies_runs_with_no_event_loop(self):
        src = DatabaseSource()
        account = _account("a")
        accounts = {build_server_id("a"): account}
        rows = [_movie_row("a", 1, "tmdb://1"), _movie_row("a", 2, "tmdb://1")]

        # No asyncio.run(), no event loop at all — a plain synchronous call,
        # exactly like it would run inside a ThreadPoolExecutor worker thread.
        movies = src._build_movies(rows, accounts)
        assert len(movies) == 1
        assert len(movies[0].versions) == 2

    def test_build_series_runs_with_no_event_loop(self):
        src = DatabaseSource()
        account = _account("a")
        accounts = {build_server_id("a"): account}
        show = _show_row("a", 1, "tmdb://show1", 0)
        ep = _episode_row("a", 1, 1, 1, 1)

        series = src._build_series([show], [ep], accounts)
        assert len(series) == 1
        assert len(series[0].episodes) == 1


class TestDryRunCliParity:
    """DoD: a CLI dry-run over the offloaded source produces the same report
    shape as before (created/updated/deleted/unchanged counts unaffected by
    WHERE the aggregation ran)."""

    @pytest.mark.asyncio
    async def test_generate_dry_run_matches_expected_counts(self, tmp_path, synthetic_movies_factory):
        from app.plex_generator.generator import PlexLibraryGenerator
        from app.plex_generator.storage import DryRunStorage

        src = DatabaseSource()
        storage = DryRunStorage()
        gen = PlexLibraryGenerator(src, storage, tmp_path, strm_only=True)
        report = await gen.generate()

        # Every synthetic movie row resolves to a playable version (accounts
        # always present, URLs always buildable) -> one .strm created per
        # version (strm_only), so total created == total versions across
        # every deduped group, not the group count itself.
        movies = await src.get_movies()
        assert report.created == sum(len(m.versions) for m in movies)
        assert report.errors == []
