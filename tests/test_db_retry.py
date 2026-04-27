"""Tests for app.utils.db_retry."""
import asyncio

import pytest
from sqlalchemy.exc import OperationalError

from app.utils.db_retry import run_with_retry, _is_locked


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
