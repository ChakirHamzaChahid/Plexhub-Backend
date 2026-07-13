"""CR-T04: exercise startup wiring (master election, scheduler, pipeline lock,
auto-provision gating) WITHOUT booting uvicorn -- the real `fcntl.flock` used
by `app.main.lifespan`'s master election does not exist on Windows, and the
existing `api_client` fixture (`tests/conftest.py`) deliberately skips the
lifespan entirely, so none of this was ever driven by a test.

Strategy (test-only, no app/ edits):
  - `_auto_provision_xtream_account()` is tested directly (Group 1) --  it is
    a real module-level coroutine in `app.main`, independent of the lifespan.
  - The master-election / scheduler-registration / pipeline-lock logic lives
    entirely as LOCAL CLOSURES inside `app.main.lifespan()` (`scheduled_sync_
    enrich_generate`, `initial_sync_then_enrich`) -- they cannot be imported.
    Instead of refactoring `main.py` (tracked as CR-A05: "no piece of main.py
    is independently testable without extracting these closures"), Group 2
    drives the REAL `lifespan()` async context manager directly, with:
      * a fake `fcntl` module injected into `sys.modules` (per the task's
        own suggested approach) so master/slave election is deterministic on
        any OS,
      * a fake `AsyncIOScheduler` that records `add_job(func, ...)` calls,
        which hands back the REAL closure function objects -- letting the
        test invoke `scheduled_sync_enrich_generate` (and the captured
        `initial_sync_then_enrich` coroutine) directly to prove the shared
        `_PIPELINE_LOCK` skip-if-already-running guard (CR-F04) actually
        works, using the real production code, not a reimplementation,
      * the sync/enrichment/validation/generation steps monkeypatched to
        call-order-recording spies (hermetic, no network/DB/filesystem work),
      * `shutdown_image_pool` (a process-wide, non-restartable
        `ThreadPoolExecutor.shutdown()`) monkeypatched to a no-op, so running
        the real lifespan teardown in this test can never break OTHER test
        files later in the same pytest session.

No test here calls the real `init_db()` (would touch the actual project
`data/plexhub.db` -- `app.db.database.engine`/`async_session_factory` are
built once at import time from `settings.DB_PATH`, fixed before any
monkeypatch of `settings.DATA_DIR` could take effect) -- it is monkeypatched
to a no-op, consistent with never wanting `lifespan()` tests to touch the
real DB file.
"""
from __future__ import annotations

import sys
import types

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.models.database import XtreamAccount
from app.services.xtream_service import xtream_service

# pytest-asyncio runs in auto mode (pyproject.toml) -- async tests need no mark.


def _fake_fcntl_module(*, flock_succeeds: bool) -> types.ModuleType:
    """A minimal stand-in for the POSIX `fcntl` module (`main.py`'s master
    election uses only `flock`, `LOCK_EX`, `LOCK_NB`)."""
    fake = types.ModuleType("fcntl")
    fake.LOCK_EX = 2
    fake.LOCK_NB = 4

    def _flock(fd, flags):
        if not flock_succeeds:
            raise OSError("lock held by another process (simulated)")

    fake.flock = _flock
    return fake


class _FakeScheduler:
    """Stand-in for `apscheduler.schedulers.asyncio.AsyncIOScheduler`.

    Records every `add_job(func, ...)` call so the test can retrieve and
    directly invoke the REAL closure functions defined inside
    `app.main.lifespan` (they cannot be imported -- see module docstring).
    """

    def __init__(self, instances: list["_FakeScheduler"]):
        instances.append(self)
        self.jobs: dict[str, object] = {}
        self.started = False
        self.shutdown_called = False

    def add_job(self, func, trigger=None, **kwargs):
        job_id = kwargs.get("id") or getattr(func, "__name__", str(func))
        self.jobs[job_id] = func

    def start(self):
        self.started = True

    def shutdown(self, wait=True):
        self.shutdown_called = True


def _account(account_id: str) -> XtreamAccount:
    return XtreamAccount(
        id=account_id, label="Test", base_url="http://x.example", port=80,
        username="u", password="p", is_active=True, created_at=0,
    )


# ─── Group 1: `_auto_provision_xtream_account` (direct call, no lifespan) ──


class TestAutoProvisionXtreamAccount:
    """`app.main._auto_provision_xtream_account` is a real module-level
    coroutine -- exercised directly, independent of the lifespan machinery."""

    async def _wire(self, db_engine, monkeypatch):
        factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        import app.db.database as database_mod
        monkeypatch.setattr(database_mod, "async_session_factory", factory)
        monkeypatch.setattr(settings, "XTREAM_BASE_URL", "http://provider.example")
        monkeypatch.setattr(settings, "XTREAM_PORT", 8080)
        monkeypatch.setattr(settings, "XTREAM_USERNAME", "auto_user")
        monkeypatch.setattr(settings, "XTREAM_PASSWORD", "auto_pass")
        return factory

    async def test_provisions_account_when_configured(self, db_engine, monkeypatch):
        factory = await self._wire(db_engine, monkeypatch)

        async def _fake_authenticate(credentials):
            assert credentials.base_url == "http://provider.example"
            assert credentials.username == "auto_user"
            return {
                "user_info": {
                    "status": "Active", "exp_date": "2000000000",
                    "max_connections": "3", "allowed_output_formats": ["m3u8", "ts"],
                },
                "server_info": {"url": "srv.example", "https_port": "443"},
            }

        monkeypatch.setattr(xtream_service, "authenticate", _fake_authenticate)

        import app.main as main_module
        await main_module._auto_provision_xtream_account()

        async with factory() as s:
            rows = (await s.execute(select(XtreamAccount))).scalars().all()

        assert len(rows) == 1
        row = rows[0]
        assert row.base_url == "http://provider.example"
        assert row.username == "auto_user"
        assert row.status == "Active"
        assert row.max_connections == 3
        assert row.is_active is True

    async def test_skips_when_account_already_exists(self, db_engine, monkeypatch):
        factory = await self._wire(db_engine, monkeypatch)

        import hashlib
        account_id = hashlib.md5(
            f"{settings.XTREAM_BASE_URL}{settings.XTREAM_USERNAME}".encode()
        ).hexdigest()[:8]
        async with factory() as s:
            s.add(_account(account_id))
            await s.commit()

        auth_calls = {"n": 0}

        async def _fake_authenticate(credentials):
            auth_calls["n"] += 1
            return {"user_info": {}, "server_info": {}}

        monkeypatch.setattr(xtream_service, "authenticate", _fake_authenticate)

        import app.main as main_module
        await main_module._auto_provision_xtream_account()

        assert auth_calls["n"] == 0, "must not re-authenticate for an already-provisioned account"
        async with factory() as s:
            rows = (await s.execute(select(XtreamAccount))).scalars().all()
        assert len(rows) == 1

    async def test_auth_failure_is_swallowed_no_account_created(self, db_engine, monkeypatch):
        factory = await self._wire(db_engine, monkeypatch)

        async def _fake_authenticate(credentials):
            raise ConnectionError("provider unreachable")

        monkeypatch.setattr(xtream_service, "authenticate", _fake_authenticate)

        import app.main as main_module
        await main_module._auto_provision_xtream_account()  # must not raise

        async with factory() as s:
            rows = (await s.execute(select(XtreamAccount))).scalars().all()
        assert rows == []


# ─── Group 2: real `lifespan()` -- master election + scheduler + pipeline lock ──


class TestLifespanMasterElection:
    """Drives the REAL `app.main.lifespan()` context manager with a fake
    `fcntl` (deterministic master/slave election on any OS) and a fake
    scheduler (captures the real pipeline closures for direct invocation)."""

    def _wire_common(self, monkeypatch, tmp_path):
        """Shared, safe monkeypatching for every lifespan-driving test:
        no real DB/network/filesystem-generation work, and no permanent
        process-wide side effect (the image-download thread pool is never
        really shut down)."""
        import app.main as main_module
        import apscheduler.schedulers.asyncio as aps_asyncio_mod
        import app.plex_generator.storage as storage_mod
        import app.workers.sync_worker as sync_worker_mod
        import app.workers.enrichment_worker as enrichment_worker_mod
        import app.workers.health_check_worker as health_check_worker_mod

        monkeypatch.setattr(settings, "DATA_DIR", tmp_path / "data")
        monkeypatch.setattr(settings, "XTREAM_BASE_URL", "")
        monkeypatch.setattr(settings, "XTREAM_USERNAME", "")
        monkeypatch.setattr(settings, "XTREAM_PASSWORD", "")

        async def _noop_init_db():
            pass

        monkeypatch.setattr(main_module, "init_db", _noop_init_db)
        # Process-wide, non-restartable ThreadPoolExecutor -- must never
        # actually shut down here, or every OTHER test in this pytest session
        # that generates a Plex library afterwards would start failing.
        monkeypatch.setattr(storage_mod, "shutdown_image_pool", lambda: None)

        instances: list[_FakeScheduler] = []
        monkeypatch.setattr(
            aps_asyncio_mod, "AsyncIOScheduler", lambda: _FakeScheduler(instances)
        )

        calls: list[str] = []

        async def _spy_sync(*a, **kw):
            calls.append("sync")

        async def _spy_enrich(*a, **kw):
            calls.append("enrich")

        async def _spy_validate(*a, **kw):
            calls.append("validate")

        async def _spy_generate(*a, **kw):
            calls.append("generate")

        monkeypatch.setattr(sync_worker_mod, "run_all_accounts", _spy_sync)
        monkeypatch.setattr(enrichment_worker_mod, "run", _spy_enrich)
        monkeypatch.setattr(health_check_worker_mod, "run_pipeline_validation", _spy_validate)
        monkeypatch.setattr(main_module, "_auto_generate_plex_library", _spy_generate)

        captured_bg: dict[str, object] = {}

        def _fake_create_background_task(coro, *, name=None):
            captured_bg["coro"] = coro
            captured_bg["name"] = name

            class _FakeTask:
                def cancel(self):
                    pass

                def done(self):
                    return True

            return _FakeTask()

        import app.utils.tasks as tasks_mod
        monkeypatch.setattr(tasks_mod, "create_background_task", _fake_create_background_task)

        return main_module, instances, calls, captured_bg

    async def test_master_starts_scheduler_and_registers_the_pipeline_job(
        self, monkeypatch, tmp_path
    ):
        main_module, instances, calls, captured_bg = self._wire_common(monkeypatch, tmp_path)
        monkeypatch.setitem(sys.modules, "fcntl", _fake_fcntl_module(flock_succeeds=True))

        async with main_module.lifespan(main_module.app):
            assert len(instances) == 1, "the master must construct exactly one scheduler"
            scheduler = instances[0]
            assert scheduler.started is True
            assert "sync_enrich_generate" in scheduler.jobs
            assert "health_check" in scheduler.jobs
            assert captured_bg.get("name") == "initial_sync"

            # Drive the REAL closure captured from add_job() -- proves the
            # scheduled pipeline actually runs sync -> enrich -> validate ->
            # generate in order (CR-A02 wiring), using production code.
            pipeline_job = scheduler.jobs["sync_enrich_generate"]
            await pipeline_job()
            assert calls == ["sync", "enrich", "validate", "generate"]

            # CR-F04: a pipeline run already in progress must make a second
            # invocation skip entirely (no re-entrant execution), whether it's
            # the interval tick or the boot-time initial run contending for
            # the SAME `_PIPELINE_LOCK`.
            calls.clear()
            async with main_module._PIPELINE_LOCK:
                await pipeline_job()
            assert calls == [], "the interval job must skip while _PIPELINE_LOCK is held"

            calls.clear()
            async with main_module._PIPELINE_LOCK:
                # Consumes the captured boot-time coroutine exactly once --
                # exercising `initial_sync_then_enrich`'s own lock-skip branch.
                await captured_bg["coro"]
            assert calls == [], "the boot-time initial run must also skip while the lock is held"

        assert scheduler.shutdown_called is True

    async def test_slave_never_constructs_a_scheduler_or_runs_the_pipeline(
        self, monkeypatch, tmp_path
    ):
        main_module, instances, calls, captured_bg = self._wire_common(monkeypatch, tmp_path)
        monkeypatch.setitem(sys.modules, "fcntl", _fake_fcntl_module(flock_succeeds=False))

        async with main_module.lifespan(main_module.app):
            assert instances == [], "a slave must never construct a scheduler"
            assert captured_bg == {}, "a slave must never schedule the boot-time initial sync"

        assert calls == []


class TestAutoProvisionGatingInLifespan:
    """`_auto_provision_xtream_account` is only invoked when
    `settings.has_xtream_env` is true -- this call-site gate lives in
    `lifespan()` itself (unlike the function's own internals, covered in
    Group 1) and runs on BOTH master and slave, ahead of the master/slave
    branch -- so the slave path (simplest: no scheduler involved) is used
    for both variants here."""

    def _wire(self, monkeypatch, tmp_path):
        import app.main as main_module
        import app.plex_generator.storage as storage_mod

        monkeypatch.setattr(settings, "DATA_DIR", tmp_path / "data")

        async def _noop_init_db():
            pass

        monkeypatch.setattr(main_module, "init_db", _noop_init_db)
        monkeypatch.setattr(storage_mod, "shutdown_image_pool", lambda: None)
        monkeypatch.setitem(sys.modules, "fcntl", _fake_fcntl_module(flock_succeeds=False))

        calls = {"n": 0}

        async def _spy_provision():
            calls["n"] += 1

        monkeypatch.setattr(main_module, "_auto_provision_xtream_account", _spy_provision)
        return main_module, calls

    async def test_not_called_when_env_absent(self, monkeypatch, tmp_path):
        main_module, calls = self._wire(monkeypatch, tmp_path)
        monkeypatch.setattr(settings, "XTREAM_BASE_URL", "")
        monkeypatch.setattr(settings, "XTREAM_USERNAME", "")
        monkeypatch.setattr(settings, "XTREAM_PASSWORD", "")

        async with main_module.lifespan(main_module.app):
            pass

        assert calls["n"] == 0

    async def test_called_when_env_fully_configured(self, monkeypatch, tmp_path):
        main_module, calls = self._wire(monkeypatch, tmp_path)
        monkeypatch.setattr(settings, "XTREAM_BASE_URL", "http://provider.example")
        monkeypatch.setattr(settings, "XTREAM_USERNAME", "u")
        monkeypatch.setattr(settings, "XTREAM_PASSWORD", "p")

        async with main_module.lifespan(main_module.app):
            pass

        assert calls["n"] == 1


# ─── Residual (documented, not forced) ────────────────────────────────────
#
# NOT covered here, and not testable without a `main.py` refactor (CR-A05 --
# extracting `scheduled_sync_enrich_generate`/`initial_sync_then_enrich` to
# module level so they're independently importable/unit-testable):
#   - The `health_check` cron (`hour=2`), `epg_cleanup` cron (`hour=3`),
#     `subtitle_cache_cleanup` cron (`hour=3`) and `db_backup` cron
#     (guarded by `settings.BACKUP_ENABLED`) job bodies themselves are each
#     covered independently elsewhere (`health_check_worker`, EPG cleanup is
#     a 4-line inline coroutine, subtitle_service, backup_db) -- only their
#     APScheduler *registration* (i.e. that `add_job` is called with the
#     right id/trigger) is implicitly exercised by constructing the real
#     lifespan with the fake scheduler above; this file does not assert on
#     every individual job id beyond `sync_enrich_generate`/`health_check`.
#   - Actually booting `uvicorn app.main:app` end-to-end (real ASGI server,
#     real TCP socket) is out of scope for a unit test on Windows and is
#     covered operationally instead (`CLAUDE.md` §4 DoD: "boot OK" is a
#     manual/CI smoke check, not this suite).
