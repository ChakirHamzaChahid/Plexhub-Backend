"""S2.6 (AUDIT-P5-004) — observability on the `GET /api/media/episodes` 400.

The endpoint has required `server_id` since the MAO/Treadstone fix
(`app/api/media.py`, regression-tested in `test_router_http_coverage.py`'s
`TestEpisodesRequireServerId`), but shipped with no way to tell whether a
legacy `PlexHubTV` client was still hitting the old contract. This is pure
telemetry: the 400 status code and its `detail` body must stay byte-identical
— only a WARN log line + the `plexhub_media_episodes_missing_server_id_total`
counter (declared, zero-initialised, in `app/utils/metrics.py` by S1.4) are
new.

Note on capture mechanism: `app.main` (imported transitively by the
`api_client` fixture) sets ``logging.getLogger("plexhub").propagate = False``
(main.py, "avoids duplicate logs"). pytest's ``caplog`` fixture attaches its
capture handler to the ROOT logger, so once `app.main` has been imported in
the process, records from `plexhub.api.media` never reach it. Instead of
relying on `caplog`, this module attaches its own handler directly to the
`plexhub.api.media` logger — deterministic regardless of import order.

See `docs/plans/2026-07-26-refacto-audit-v1-plan.md` §VAGUE 2 S2.6.
"""
from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest
import pytest_asyncio

from app.config import settings
from app.db import database as db_module
from app.services import api_key_service
from app.utils import metrics

# pytest-asyncio runs in auto mode (pyproject.toml) — async tests need no mark.

API_KEY = "test-master-key-episodes-guard-metric"
API_HEADERS = {"X-API-Key": API_KEY}


@pytest_asyncio.fixture(autouse=True)
async def _wire_test_db(monkeypatch, db_factory):
    """Same wiring pattern as `test_router_http_coverage.py::_wire_test_db`:
    point `get_db` and `api_key_service` at the isolated in-memory engine and
    set the master key the guarded routers check against."""
    monkeypatch.setattr(db_module, "async_session_factory", db_factory)
    monkeypatch.setattr(api_key_service, "async_session_factory", db_factory)
    monkeypatch.setattr(settings, "AI_API_KEY", API_KEY)
    return db_factory


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def media_logs() -> Iterator[_ListHandler]:
    """Captures records from `plexhub.api.media` directly (see module
    docstring for why `caplog` can't be used here)."""
    handler = _ListHandler()
    target = logging.getLogger("plexhub.api.media")
    target.addHandler(handler)
    try:
        yield handler
    finally:
        target.removeHandler(handler)


def _count() -> float:
    return metrics.media_episodes_missing_server_id_total._value.get()


async def test_missing_server_id_logs_warning_and_increments_counter(api_client, media_logs):
    before = _count()
    resp = await api_client.get(
        "/api/media/episodes",
        params={"parent_rating_key": "series_7724"},
        headers={**API_HEADERS, "User-Agent": "PlexHubTV/legacy-1.0"},
    )

    # Contract untouched: still a 400 with the exact same detail text.
    assert resp.status_code == 400
    assert resp.json()["detail"] == (
        "server_id is required to list episodes: a series rating key is "
        "unique only per account and collides across accounts."
    )

    assert _count() == before + 1

    assert len(media_logs.records) == 1
    record = media_logs.records[0]
    message = record.getMessage()
    assert record.levelno == logging.WARNING
    assert "series_7724" in message
    assert "PlexHubTV/legacy-1.0" in message
    # No secret/credential vocabulary in the message.
    assert "X-API-Key" not in message
    assert API_KEY not in message


async def test_blank_server_id_also_logs_and_increments(api_client, media_logs):
    before = _count()
    resp = await api_client.get(
        "/api/media/episodes",
        params={"parent_rating_key": "series_7724", "server_id": ""},
        headers=API_HEADERS,
    )
    assert resp.status_code == 400
    assert _count() == before + 1
    assert len(media_logs.records) == 1


async def test_long_user_agent_is_truncated_in_log(api_client, media_logs):
    huge_ua = "X" * 5000
    resp = await api_client.get(
        "/api/media/episodes",
        params={"parent_rating_key": "series_7724"},
        headers={**API_HEADERS, "User-Agent": huge_ua},
    )
    assert resp.status_code == 400
    assert len(media_logs.records) == 1
    message = media_logs.records[0].getMessage()
    # The full 5000-char UA must NOT be reflected verbatim into the log.
    assert huge_ua not in message


async def test_with_server_id_neither_logs_nor_increments(api_client, media_logs):
    before = _count()
    resp = await api_client.get(
        "/api/media/episodes",
        params={"parent_rating_key": "series_7724", "server_id": "xtream_acctA"},
        headers=API_HEADERS,
    )
    # 200 with an empty result set (no seeded episodes) — the guard clause
    # itself is what's under test here, not the data path.
    assert resp.status_code == 200, resp.text
    assert _count() == before
    assert media_logs.records == []
