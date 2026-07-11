"""Security regression tests for POST /api/plex/generate (CR-S01).

The request body's ``outputDir`` (camel for ``output_dir``) reaches ANY holder of
a valid API key — the master secret OR any active per-user key, both accepted by
``verify_backend_secret`` (``app/api/deps.py``) — not just an operator. Before this
fix it flowed straight into ``Path(output_dir)`` -> ``LocalStorage`` with zero
confinement, letting any API-key holder write/delete files at an arbitrary
filesystem path and exfiltrate other accounts' Xtream credentials embedded in the
generated ``.strm`` files (see ``docs/audit/cleanroom-2026-07-11/40-security.md``,
CR-S01).

These tests assert the containment guard added at
``app.api.plex._resolve_confined_output_dir`` (called from
``generate_plex_library``, ``app/api/plex.py``):
  - a client ``outputDir`` that resolves OUTSIDE ``settings.PLEX_LIBRARY_DIR``
    (``..`` traversal, an unrelated absolute path, or the base's own parent)
    -> HTTP 400, never reaches the generator;
  - a client ``outputDir`` that resolves to the base itself or a descendant of it
    -> accepted (falls through to generation, not rejected by the guard);
  - a client ``outputDir`` supplied while NO base is configured -> HTTP 400 (no
    safe root to confine to — never fall back to trusting the client path).
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.plex_generator import source as plex_source_module

# pytest-asyncio runs in auto mode (pyproject.toml) — async tests need no mark.

# The JSON API is X-API-Key gated (fail-closed); set the master secret and send
# it as the header — same pattern as tests/test_adult_classification.py.
API_KEY = "test-master-key-plex-security"
API_HEADERS = {"X-API-Key": API_KEY}


@pytest.fixture(autouse=True)
def _master_key(monkeypatch):
    monkeypatch.setattr(settings, "AI_API_KEY", API_KEY)


class TestGenerateOutputDirEscapeRejected:
    """Any outputDir that resolves outside settings.PLEX_LIBRARY_DIR -> 400."""

    @pytest.mark.parametrize(
        "build_escape",
        [
            pytest.param(lambda base, tmp: str(tmp / "elsewhere"), id="unrelated-absolute-path"),
            pytest.param(lambda base, tmp: str(base / ".." / "escaped"), id="dotdot-traversal"),
            pytest.param(lambda base, tmp: str(base.parent), id="parent-of-base"),
        ],
    )
    async def test_outside_base_rejected_400(
        self, api_client, tmp_path, monkeypatch, build_escape
    ):
        base = tmp_path / "library"
        base.mkdir()
        monkeypatch.setattr(settings, "PLEX_LIBRARY_DIR", str(base))

        escape = build_escape(base, tmp_path)
        resp = await api_client.post(
            "/api/plex/generate",
            json={"outputDir": escape, "dryRun": True},
            headers=API_HEADERS,
        )
        assert resp.status_code == 400
        assert "outputDir" in resp.json()["detail"]

    async def test_empty_base_plus_client_path_rejected_400(
        self, api_client, tmp_path, monkeypatch
    ):
        """No PLEX_LIBRARY_DIR configured + a client path -> reject, never trust
        an arbitrary client-chosen root."""
        monkeypatch.setattr(settings, "PLEX_LIBRARY_DIR", "")
        resp = await api_client.post(
            "/api/plex/generate",
            json={"outputDir": str(tmp_path / "anything"), "dryRun": True},
            headers=API_HEADERS,
        )
        assert resp.status_code == 400
        assert "PLEX_LIBRARY_DIR" in resp.json()["detail"]

    async def test_missing_output_dir_and_empty_base_still_400(
        self, api_client, monkeypatch
    ):
        """Pre-existing guard (no outputDir + no configured base) must survive
        the CR-S01 refactor."""
        monkeypatch.setattr(settings, "PLEX_LIBRARY_DIR", "")
        resp = await api_client.post(
            "/api/plex/generate",
            json={"dryRun": True},
            headers=API_HEADERS,
        )
        assert resp.status_code == 400


class TestGenerateOutputDirInsideBaseAccepted:
    """A path inside (or equal to) PLEX_LIBRARY_DIR must NOT be rejected by the
    confinement guard."""

    @pytest_asyncio.fixture
    async def _empty_db(self, db_engine, monkeypatch):
        """Wire DatabaseSource (used by the generate() endpoint) to an empty
        in-memory DB so the request can run generate() end-to-end without any
        seeded Xtream accounts/media (source.py imports `async_session_factory`
        by name at module scope, so it must be patched on that module, not on
        app.db.database)."""
        factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr(plex_source_module, "async_session_factory", factory)

    @pytest.mark.parametrize(
        "build_ok",
        [
            pytest.param(lambda base: str(base), id="equal-to-base"),
            pytest.param(lambda base: str(base / "subdir"), id="descendant-of-base"),
        ],
    )
    async def test_inside_base_accepted(
        self, api_client, tmp_path, monkeypatch, _empty_db, build_ok
    ):
        base = tmp_path / "library"
        base.mkdir()
        monkeypatch.setattr(settings, "PLEX_LIBRARY_DIR", str(base))

        target = build_ok(base)
        resp = await api_client.post(
            "/api/plex/generate",
            json={"outputDir": target, "dryRun": True},
            headers=API_HEADERS,
        )
        # A 400 here would mean the containment check itself regressed and now
        # rejects legitimate in-base paths.
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["created"] == 0
        assert body["errors"] == []
