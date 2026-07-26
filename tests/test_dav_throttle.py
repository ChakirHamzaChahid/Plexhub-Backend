"""DAV-1B: `app/dav/throttle.py` — per-account upstream concurrency gate.

Pure asyncio-primitive tests — no HTTP/DB involved. pytest-asyncio auto mode
(pyproject.toml): async `test_*` functions need no decorator.
"""
from __future__ import annotations

import asyncio

import pytest
from prometheus_client import REGISTRY, generate_latest

from app.config import settings
from app.dav.throttle import AccountThrottle, ThrottleTimeout, account_throttle, upstream_limit
from app.models.database import XtreamAccount
from app.utils import metrics


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


# ─── S5.3 (AUDIT-P8-002): permit-wait histogram + rejection counter ────────


def _histogram_count(hist) -> float:
    """`Histogram` has no public `_count` accessor (unlike `Counter`'s
    `_value`) — read it back the same way `prometheus_client.generate_latest`
    would, via `.collect()`'s samples, filtering the `_count` suffix."""
    for metric in hist.collect():
        for sample in metric.samples:
            if sample.name.endswith("_count"):
                return sample.value
    raise AssertionError("no _count sample found")  # pragma: no cover - defensive


def _counter_value(counter) -> float:
    return counter._value.get()


class TestPermitWaitAndRejectionMetrics:
    """`AccountThrottle.acquire` is the ONLY call site for both
    `plexhub_dav_permit_wait_seconds` (success) and
    `plexhub_dav_throttle_rejections_total` (`ThrottleTimeout`) — both
    unlabelled by design (no `account_id`, no URL), see the metric
    docstrings in `app/utils/metrics.py` and `AccountThrottle.acquire`'s own.
    """

    async def test_successful_acquire_observes_wait_seconds_and_does_not_reject(self):
        throttle = AccountThrottle()
        wait_before = _histogram_count(metrics.dav_permit_wait_seconds)
        rejections_before = _counter_value(metrics.dav_throttle_rejections_total)

        release = await throttle.acquire("acct1", limit=1, timeout=5)
        release()

        assert _histogram_count(metrics.dav_permit_wait_seconds) == wait_before + 1
        assert _counter_value(metrics.dav_throttle_rejections_total) == rejections_before

    async def test_queued_acquire_observes_a_nonzero_wait(self):
        """A second caller that has to wait for the first release still
        succeeds (no timeout) — its wait time must be observed as > 0,
        proving the histogram measures actual queueing, not just "acquired
        instantly"."""
        throttle = AccountThrottle()
        release1 = await throttle.acquire("acct1", limit=1, timeout=5)

        async def _second():
            return await throttle.acquire("acct1", limit=1, timeout=5)

        task = asyncio.create_task(_second())
        await asyncio.sleep(0.05)
        release1()
        release2 = await asyncio.wait_for(task, timeout=5)
        release2()

        sum_after = metrics.dav_permit_wait_seconds._sum.get()
        assert sum_after > 0, "queued caller's wait must be reflected in the histogram sum"

    async def test_timeout_increments_rejection_counter_and_leaves_wait_histogram_untouched(self):
        """This is the signal AUDIT-P8-002 asks for: a 503+Retry-After
        (`ThrottleTimeout`) MUST be countable. The wait histogram is NOT
        touched on this path (it only measures the wait that precedes an
        actual relay, per its own docstring)."""
        throttle = AccountThrottle()
        release1 = await throttle.acquire("acct1", limit=1, timeout=5)
        try:
            rejections_before = _counter_value(metrics.dav_throttle_rejections_total)
            wait_count_before = _histogram_count(metrics.dav_permit_wait_seconds)

            with pytest.raises(ThrottleTimeout):
                await throttle.acquire("acct1", limit=1, timeout=0.05)

            assert _counter_value(metrics.dav_throttle_rejections_total) == rejections_before + 1
            assert _histogram_count(metrics.dav_permit_wait_seconds) == wait_count_before
        finally:
            release1()

    async def test_multiple_timeouts_accumulate_on_the_same_counter(self):
        throttle = AccountThrottle()
        release1 = await throttle.acquire("acct1", limit=1, timeout=5)
        try:
            before = _counter_value(metrics.dav_throttle_rejections_total)
            for _ in range(3):
                with pytest.raises(ThrottleTimeout):
                    await throttle.acquire("acct1", limit=1, timeout=0.02)
            assert _counter_value(metrics.dav_throttle_rejections_total) == before + 3
        finally:
            release1()

    async def test_account_id_never_appears_as_a_prometheus_label_or_value(self):
        """Open-cardinality identifiers (`account_id`) must never leak into
        the metrics surface (piège 18f / AUDIT-P8-002 non-negotiable) — both
        metrics are declared with NO labelnames in `app/utils/metrics.py`, so
        there is no `.labels(...)` call site available even by mistake. This
        proves it end-to-end: acquire/timeout using a distinctive
        "account_id" that looks like it could carry a secret, then scrape
        the real Prometheus registry and assert it never shows up."""
        secret_looking_account_id = "acct-SEKRET-SHOULD-NEVER-LEAK-42"
        throttle = AccountThrottle()

        # Hold the only permit, then force a real timeout on a second
        # acquire for the SAME account id -> exercises both the success
        # path (permit-wait histogram) and the rejection path (counter)
        # with a distinctive, secret-looking account id.
        release1 = await throttle.acquire(secret_looking_account_id, limit=1, timeout=5)
        with pytest.raises(ThrottleTimeout):
            await throttle.acquire(secret_looking_account_id, limit=1, timeout=0.02)
        release1()

        body = generate_latest(REGISTRY).decode("utf-8")
        assert secret_looking_account_id not in body
        # The two counter/unbucketed lines carry NO labels at all (only the
        # histogram's `_bucket` lines legitimately have a `le="..."` label,
        # which is a fixed bucket boundary, never an account id).
        for line in body.splitlines():
            if line.startswith("plexhub_dav_throttle_rejections_total "):
                assert "{" not in line
            if line.startswith("plexhub_dav_permit_wait_seconds_count ") or line.startswith(
                "plexhub_dav_permit_wait_seconds_sum "
            ):
                assert "{" not in line
