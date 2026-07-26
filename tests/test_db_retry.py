"""Tests for app.utils.db_retry."""
import asyncio

import pytest
from sqlalchemy.exc import OperationalError

from app.utils.db_retry import run_with_retry, write_with_retry, _is_locked


class _FakeOpError(OperationalError):
    """OperationalError doesn't have a no-arg constructor — build a minimal one."""
    def __init__(self, msg: str):
        super().__init__(msg, params=None, orig=Exception(msg))


class TestIsLocked:
    def test_matches_database_is_locked(self):
        assert _is_locked(Exception("OperationalError: database is locked"))

    def test_matches_table_locked(self):
        assert _is_locked(Exception("database table is locked"))

    def test_no_match_other_error(self):
        assert not _is_locked(Exception("syntax error"))


class TestRunWithRetry:
    def test_succeeds_first_try(self):
        calls = {"n": 0}

        async def op():
            calls["n"] += 1
            return "ok"

        result = asyncio.run(run_with_retry(op, delays=(0.0,)))
        assert result == "ok"
        assert calls["n"] == 1

    def test_retries_then_succeeds(self):
        calls = {"n": 0}

        async def op():
            calls["n"] += 1
            if calls["n"] < 3:
                raise _FakeOpError("database is locked")
            return "ok"

        result = asyncio.run(run_with_retry(op, delays=(0.0, 0.0, 0.0)))
        assert result == "ok"
        assert calls["n"] == 3

    def test_gives_up_after_attempts(self):
        async def op():
            raise _FakeOpError("database is locked")

        with pytest.raises(OperationalError):
            asyncio.run(run_with_retry(op, delays=(0.0,)))

    def test_non_locked_error_not_retried(self):
        calls = {"n": 0}

        async def op():
            calls["n"] += 1
            raise _FakeOpError("syntax error near 'foo'")

        with pytest.raises(OperationalError):
            asyncio.run(run_with_retry(op, delays=(0.0, 0.0, 0.0)))
        assert calls["n"] == 1  # no retry on non-lock errors


class _FakeSession:
    """Minimal stand-in for `AsyncSession` — just enough surface for
    `write_with_retry`'s `work(session)` contract, no real DB involved.
    Each instance is distinct so tests can prove a fresh one is created
    per attempt (mirrors the real-lock proof in
    tests/test_db_retry_real_lock.py, without a file-backed engine)."""

    def __init__(self):
        self.committed = False

    async def commit(self):
        self.committed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class TestWriteWithRetry:
    """Synthetic control-flow coverage for the fresh-session-per-attempt
    primitive (AUDIT-P1-001 / ADR 0004, Decision 4). The genuine-lock proof
    lives in tests/test_db_retry_real_lock.py; this class only exercises
    the retry bookkeeping in isolation."""

    def test_succeeds_first_try_with_one_session(self):
        sessions_made: list[_FakeSession] = []

        def factory():
            s = _FakeSession()
            sessions_made.append(s)
            return s

        async def work(session):
            await session.commit()
            return "ok"

        result = asyncio.run(
            write_with_retry(work, session_factory=factory, delays=(0.0,))
        )
        assert result == "ok"
        assert len(sessions_made) == 1
        assert sessions_made[0].committed

    def test_retries_with_a_fresh_session_each_attempt(self):
        sessions_made: list[_FakeSession] = []
        attempts = {"n": 0}

        def factory():
            s = _FakeSession()
            sessions_made.append(s)
            return s

        async def work(session):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise _FakeOpError("database is locked")
            await session.commit()
            return "ok"

        result = asyncio.run(
            write_with_retry(work, session_factory=factory, delays=(0.0, 0.0, 0.0))
        )
        assert result == "ok"
        assert attempts["n"] == 3
        # One fresh session object per attempt, none reused.
        assert len(sessions_made) == 3
        assert len({id(s) for s in sessions_made}) == 3
        # Only the final (successful) attempt's session got committed.
        assert sessions_made[-1].committed
        assert not sessions_made[0].committed
        assert not sessions_made[1].committed

    def test_non_locked_error_not_retried(self):
        attempts = {"n": 0}

        def factory():
            return _FakeSession()

        async def work(session):
            attempts["n"] += 1
            raise _FakeOpError("syntax error near 'foo'")

        with pytest.raises(OperationalError):
            asyncio.run(
                write_with_retry(work, session_factory=factory, delays=(0.0, 0.0, 0.0))
            )
        assert attempts["n"] == 1

    def test_gives_up_after_attempts(self):
        def factory():
            return _FakeSession()

        async def work(session):
            raise _FakeOpError("database is locked")

        with pytest.raises(OperationalError):
            asyncio.run(write_with_retry(work, session_factory=factory, delays=(0.0,)))
