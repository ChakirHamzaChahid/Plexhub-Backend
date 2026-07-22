"""DAV-1B: `app/dav/throttle.py` — per-account upstream concurrency gate.

Pure asyncio-primitive tests — no HTTP/DB involved. pytest-asyncio auto mode
(pyproject.toml): async `test_*` functions need no decorator.
"""
from __future__ import annotations

import asyncio

import pytest

from app.config import settings
from app.dav.throttle import AccountThrottle, ThrottleTimeout, account_throttle, upstream_limit
from app.models.database import XtreamAccount


def _account(max_connections: int = 1) -> XtreamAccount:
    return XtreamAccount(
        id="acct1",
        label="Test provider",
        base_url="http://provider.example",
        port=80,
        username="u",
        password="p",
        max_connections=max_connections,
    )


# ─── upstream_limit: clamp by account.max_connections ──────────────────────


class TestUpstreamLimit:
    def test_global_default_wins_when_account_allows_more(self, monkeypatch):
        monkeypatch.setattr(settings, "DAV_UPSTREAM_PER_ACCOUNT", 3)
        assert upstream_limit(_account(max_connections=10)) == 3

    def test_account_max_connections_wins_when_lower_than_global_default(self, monkeypatch):
        # The scenario from the ticket: DAV_UPSTREAM_PER_ACCOUNT=3 but this
        # account only tolerates 1 concurrent connection -> clamp to 1.
        monkeypatch.setattr(settings, "DAV_UPSTREAM_PER_ACCOUNT", 3)
        assert upstream_limit(_account(max_connections=1)) == 1

    def test_unset_max_connections_falls_back_to_global_default(self, monkeypatch):
        monkeypatch.setattr(settings, "DAV_UPSTREAM_PER_ACCOUNT", 2)
        assert upstream_limit(_account(max_connections=0)) == 2

    def test_result_is_never_below_one(self, monkeypatch):
        monkeypatch.setattr(settings, "DAV_UPSTREAM_PER_ACCOUNT", 0)
        assert upstream_limit(_account(max_connections=0)) == 1


# ─── AccountThrottle: acquire/release/timeout semantics ────────────────────


class TestAccountThrottleConcurrency:
    async def test_second_get_waits_while_first_holds_the_only_permit(self):
        throttle = AccountThrottle()
        release1 = await throttle.acquire("acct1", limit=1, timeout=5)

        second_acquired = asyncio.Event()

        async def _second():
            release2 = await throttle.acquire("acct1", limit=1, timeout=5)
            second_acquired.set()
            release2()

        task = asyncio.create_task(_second())
        try:
            await asyncio.sleep(0.05)
            assert not second_acquired.is_set(), (
                "a second GET must wait while the only permit is held"
            )
        finally:
            release1()
            await asyncio.wait_for(task, timeout=5)
        assert second_acquired.is_set()

    async def test_timeout_raises_when_no_permit_frees_up_in_time(self):
        throttle = AccountThrottle()
        release1 = await throttle.acquire("acct1", limit=1, timeout=5)
        try:
            with pytest.raises(ThrottleTimeout):
                await throttle.acquire("acct1", limit=1, timeout=0.05)
        finally:
            release1()

    async def test_release_is_idempotent_and_does_not_over_release_the_semaphore(self):
        throttle = AccountThrottle()
        release = await throttle.acquire("acct1", limit=1, timeout=5)
        release()
        release()  # calling twice must be a no-op, not a second real release

        # If the double-release HAD leaked an extra permit, this acquire
        # would still succeed even without releasing it — so additionally
        # prove there is still only ONE permit by acquiring then timing out
        # on a second, concurrent acquire.
        release2 = await throttle.acquire("acct1", limit=1, timeout=1)
        with pytest.raises(ThrottleTimeout):
            await throttle.acquire("acct1", limit=1, timeout=0.05)
        release2()

    async def test_permit_is_released_after_full_body_consumption_and_explicit_release(self):
        """Mirrors how the router is expected to use this: hold the permit
        for the duration of streaming a body, release in a `finally` once
        fully consumed — a later caller must then acquire immediately."""
        throttle = AccountThrottle()

        async def _consume_body(chunks: list[bytes]) -> None:
            release = await throttle.acquire("acct1", limit=1, timeout=5)
            try:
                for _ in chunks:
                    await asyncio.sleep(0)
            finally:
                release()

        await _consume_body([b"a", b"b", b"c"])

        release2 = await asyncio.wait_for(
            throttle.acquire("acct1", limit=1, timeout=1), timeout=1,
        )
        release2()

    async def test_independent_accounts_do_not_share_a_semaphore(self):
        throttle = AccountThrottle()
        release_a = await throttle.acquire("acct-a", limit=1, timeout=5)
        try:
            # acct-b must NOT be blocked by acct-a's held permit.
            release_b = await asyncio.wait_for(
                throttle.acquire("acct-b", limit=1, timeout=1), timeout=1,
            )
            release_b()
        finally:
            release_a()


class TestModuleSingleton:
    def test_account_throttle_singleton_is_an_account_throttle(self):
        assert isinstance(account_throttle, AccountThrottle)
