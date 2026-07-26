"""Auth guard on GET /metrics (S4.1, AUDIT-P2-001/AUDIT-P8-003/CR-S02,
ADR 0004 §Décision 3).

Covers the DoD from `docs/plans/2026-07-26-refacto-audit-v1-plan.md` §S4.1:
  - no credentials -> 401 (was 200 before this step);
  - correct METRICS_USERNAME/METRICS_PASSWORD -> 200 + Prometheus content;
  - METRICS_PASSWORD empty (and METRICS_PUBLIC not set) -> 503 fail-closed
    (same convention as ADMIN_PASSWORD/DAV_PASSWORD);
  - METRICS_PUBLIC=true -> 200 with NO credentials (explicit escape hatch);
  - constant-time comparison (grep-based guard, same pattern used elsewhere
    in this repo to forbid `==` on a secret).
"""
from __future__ import annotations

import logging
import pathlib
from collections.abc import Iterator

import pytest

from app.config import settings


# NOTE: no module-level `pytestmark = pytest.mark.asyncio` here — this file
# mixes async (HTTP) tests with plain sync tests (boot-log assertions,
# source-grep), and `asyncio_mode = "auto"` (pyproject.toml) already detects
# `async def` tests without it (same convention as tests/test_rating_blend.py).

METRICS_USER = "metrics-user"
METRICS_PASS = "test-metrics-pass"
METRICS_AUTH = (METRICS_USER, METRICS_PASS)


@pytest.fixture(autouse=True)
def _metrics_creds(monkeypatch):
    monkeypatch.setattr(settings, "METRICS_USERNAME", METRICS_USER)
    monkeypatch.setattr(settings, "METRICS_PASSWORD", METRICS_PASS)
    monkeypatch.setattr(settings, "METRICS_PUBLIC", False)


async def test_metrics_without_credentials_is_401(api_client):
    resp = await api_client.get("/metrics")
    assert resp.status_code == 401
    assert resp.headers.get("www-authenticate", "").lower().startswith("basic")


async def test_metrics_with_wrong_credentials_is_401(api_client):
    resp = await api_client.get("/metrics", auth=(METRICS_USER, "wrong"))
    assert resp.status_code == 401


async def test_metrics_with_correct_credentials_is_200_prometheus_body(api_client):
    resp = await api_client.get("/metrics", auth=METRICS_AUTH)
    assert resp.status_code == 200
    # Real Prometheus exposition content, not just "200 with an empty body".
    assert "plexhub_" in resp.text


async def test_metrics_password_empty_is_503_fail_closed(api_client, monkeypatch):
    monkeypatch.setattr(settings, "METRICS_PASSWORD", "")
    resp = await api_client.get("/metrics", auth=METRICS_AUTH)
    assert resp.status_code == 503


async def test_metrics_public_escape_hatch_allows_no_credentials(api_client, monkeypatch):
    monkeypatch.setattr(settings, "METRICS_PUBLIC", True)
    # Even an empty password must not fail-close once the escape hatch is on.
    monkeypatch.setattr(settings, "METRICS_PASSWORD", "")
    resp = await api_client.get("/metrics")
    assert resp.status_code == 200
    assert "plexhub_" in resp.text


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def config_logs() -> Iterator[_ListHandler]:
    """Captures records from `plexhub.config` directly rather than via
    `caplog`: once `app.main` has been imported (transitively, by the
    `api_client` fixture used by the tests above), `logging.getLogger(
    "plexhub").propagate = False` (main.py, "avoids duplicate logs") stops
    records from reaching caplog's root-attached handler — same gotcha
    already documented in `tests/test_media_episodes_missing_server_id_metric.py`."""
    handler = _ListHandler()
    target = logging.getLogger("plexhub.config")
    target.addHandler(handler)
    try:
        yield handler
    finally:
        target.removeHandler(handler)


def test_metrics_public_escape_hatch_logs_a_warning_at_boot(config_logs, monkeypatch, tmp_path):
    """`Settings.__init__` must WARN when METRICS_PUBLIC=true — never a silent
    reopen (ADR 0004 §Décision 3). Builds a throwaway `Settings()` instance
    with dirs redirected under `tmp_path` — never touches the real
    data/logs directories, and never reloads `app.config` (which would orphan
    every other module's already-bound `from app.config import settings`)."""
    from app import config as cfg_module

    monkeypatch.setattr(cfg_module.Settings, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(cfg_module.Settings, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(cfg_module.Settings, "METRICS_PUBLIC", True)

    cfg_module.Settings()

    assert any(
        "METRICS_PUBLIC=true" in record.getMessage() for record in config_logs.records
    )


def test_metrics_password_empty_without_public_logs_a_warning_at_boot(
    config_logs, monkeypatch, tmp_path
):
    """The counterpart of the case above: forgetting METRICS_PASSWORD (and not
    opting into METRICS_PUBLIC) must also be visible at boot, not just as a
    503 discovered later by an operator staring at a scrape failure."""
    from app import config as cfg_module

    monkeypatch.setattr(cfg_module.Settings, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(cfg_module.Settings, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(cfg_module.Settings, "METRICS_PUBLIC", False)
    monkeypatch.setattr(cfg_module.Settings, "METRICS_PASSWORD", "")

    cfg_module.Settings()

    assert any(
        "METRICS_PASSWORD not set" in record.getMessage() for record in config_logs.records
    )


def test_verify_metrics_basic_auth_uses_constant_time_compare():
    """Grep-based guard (same pattern as the docstring in app/api/deps.py):
    forbids a plain `==` comparison on the metrics credentials, which would
    reopen a timing oracle."""
    src = pathlib.Path("app/api/deps.py").read_text(encoding="utf-8")
    start = src.index("async def verify_metrics_basic_auth")
    end = src.index("\nasync def ", start + 1) if "\nasync def " in src[start + 1:] else len(src)
    body = src[start:end]
    assert "compare_digest" in body
    assert "credentials.username ==" not in body
    assert "credentials.password ==" not in body
