"""Stream validation must never probe a provider wider than its max_connections.

Probing more concurrent streams than the provider allows trips its connection
cap → 503 / dropped connections → those throttle responses look like dead
streams and (across STREAM_BROKEN_THRESHOLD runs) wrongly mark playable streams
broken. So the validator clamps per-account concurrency to max_connections, the
same way the sync worker does (PR #11).
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from types import SimpleNamespace

import pytest

from app.config import settings
from app.models.database import Media, XtreamAccount
from app.workers import health_check_worker as hc


# ─── _account_concurrency (pure) ─────────────────────────────────────────


@pytest.mark.parametrize(
    "max_connections, global_cap, expected",
    [
        (1, 20, 1),     # the 1-connection mirror → exactly 1 in flight
        (3, 20, 3),     # Cloudflare-fronted, 3 connections
        (0, 20, 20),    # 0 = provider sets no limit → fall back to global cap
        (None, 20, 20),  # NULL behaves like 0
        (50, 20, 20),   # provider allows more than we want → global cap wins
        (5, 4, 4),      # global cap below provider limit → global cap wins
    ],
)
def test_account_concurrency_clamps(monkeypatch, max_connections, global_cap, expected):
    monkeypatch.setattr(settings, "STREAM_VALIDATION_CONCURRENCY", global_cap)
    acc = SimpleNamespace(max_connections=max_connections)
    assert hc._account_concurrency(acc) == expected


# ─── pipeline validation respects per-account limits ─────────────────────


def _media(server_id: str, n: int) -> list[Media]:
    return [
        Media(
            rating_key=f"vod_{server_id}_{i}.mp4",
            server_id=server_id,
            library_section_id="1",
            title=f"t{i}",
            type="movie",
            page_offset=i,  # keep the (server_id, section, filter, sort, offset) index unique
            last_stream_check=None,
            is_in_allowed_categories=True,
        )
        for i in range(n)
    ]


def _account(acc_id: str, max_conn: int) -> XtreamAccount:
    return XtreamAccount(
        id=acc_id,
        label=acc_id,
        base_url=f"http://{acc_id}.test",
        username="u",
        password="p",
        max_connections=max_conn,
    )


@pytest.mark.asyncio
async def test_pipeline_validation_peak_concurrency_per_account(monkeypatch, db_factory):
    # Two providers: one allows a single connection, one allows three.
    async with db_factory() as db:
        db.add(_account("aaaaaaaa", 1))
        db.add(_account("bbbbbbbb", 3))
        for m in _media("xtream_aaaaaaaa", 30):
            db.add(m)
        for m in _media("xtream_bbbbbbbb", 30):
            db.add(m)
        await db.commit()

    monkeypatch.setattr(settings, "STREAM_VALIDATION_ENABLED", True)
    # Global cap deliberately far above both providers so the *clamp* is what
    # limits — not the global setting.
    monkeypatch.setattr(settings, "STREAM_VALIDATION_CONCURRENCY", 20)
    monkeypatch.setattr(hc, "worker_session_factory", db_factory)

    async def _fake_client():
        return None

    monkeypatch.setattr(hc, "_get_client", _fake_client)

    cur: dict[str, int] = defaultdict(int)
    peak: dict[str, int] = defaultdict(int)

    async def _fake_check_one(client, item, account, semaphore):
        # The semaphore is exactly what enforces the per-account ceiling, so
        # honour it like the real _check_one does.
        async with semaphore:
            sid = item.server_id
            cur[sid] += 1
            peak[sid] = max(peak[sid], cur[sid])
            await asyncio.sleep(0.01)
            cur[sid] -= 1
            return item, False, "get_ct_video", None

    monkeypatch.setattr(hc, "_check_one", _fake_check_one)

    await hc._run_pipeline_validation_impl()

    # The 1-connection provider is never probed more than once at a time;
    # the 3-connection provider reaches its cap but never exceeds it.
    assert peak["xtream_aaaaaaaa"] == 1
    assert peak["xtream_bbbbbbbb"] == 3
