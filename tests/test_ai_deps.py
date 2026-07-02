"""Unit tests for app.api.deps.verify_api_key.

Covers the 3 failure modes (2x 503, 1x 401) plus a passing case.
Also asserts the C2 critical correction (no `==` on settings.AI_API_KEY)
holds at the source level via a static grep over app/.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.api.deps import verify_api_key
from app.config import settings
from app.db.database import _VEC_LOADED
from app.services import api_key_service


REPO_ROOT = Path(__file__).resolve().parents[1]


async def _resolve_none(*args, **kwargs):
    """Stub api_key_service.resolve → no matching per-user key (hermetic, no DB)."""
    return None


# ──────────────────────────────────────────────────────────────────────────────
# verify_api_key behaviour
# ──────────────────────────────────────────────────────────────────────────────

async def test_401_when_ai_api_key_empty_and_no_user_key(monkeypatch):
    # An empty master secret is no longer a 503 "not configured": per-user keys
    # may still exist, so an unknown key just fails auth with 401.
    monkeypatch.setattr(settings, "AI_API_KEY", "")
    monkeypatch.setitem(_VEC_LOADED, "ok", True)
    monkeypatch.setattr(api_key_service, "resolve", _resolve_none)
    with pytest.raises(HTTPException) as exc:
        await verify_api_key(request=None, x_api_key="anything")
    assert exc.value.status_code == 401
    assert exc.value.detail == "Invalid API key"


async def test_503_when_vec_unavailable(monkeypatch):
    monkeypatch.setattr(settings, "AI_API_KEY", "secret")
    monkeypatch.setitem(_VEC_LOADED, "ok", False)
    with pytest.raises(HTTPException) as exc:
        await verify_api_key(request=None, x_api_key="secret")
    assert exc.value.status_code == 503
    assert exc.value.detail == "AI vector storage unavailable"


async def test_401_when_header_missing(monkeypatch):
    monkeypatch.setattr(settings, "AI_API_KEY", "secret")
    monkeypatch.setitem(_VEC_LOADED, "ok", True)
    with pytest.raises(HTTPException) as exc:
        await verify_api_key(request=None, x_api_key=None)
    assert exc.value.status_code == 401
    assert exc.value.detail == "Invalid API key"


async def test_401_when_header_wrong(monkeypatch):
    monkeypatch.setattr(settings, "AI_API_KEY", "secret")
    monkeypatch.setitem(_VEC_LOADED, "ok", True)
    monkeypatch.setattr(api_key_service, "resolve", _resolve_none)
    with pytest.raises(HTTPException) as exc:
        await verify_api_key(request=None, x_api_key="wrong")
    assert exc.value.status_code == 401


async def test_pass_when_key_correct(monkeypatch):
    monkeypatch.setattr(settings, "AI_API_KEY", "secret")
    monkeypatch.setitem(_VEC_LOADED, "ok", True)
    result = await verify_api_key(request=None, x_api_key="secret")
    assert result is None


# ──────────────────────────────────────────────────────────────────────────────
# C2 static guard: forbid `==` on settings.AI_API_KEY anywhere in app/.
# ──────────────────────────────────────────────────────────────────────────────

def test_no_equality_on_ai_api_key_in_app():
    """Belt-and-suspenders : verify the C2 correction stays in place.

    The dependency MUST use secrets.compare_digest. Plain == is forbidden
    so the test scans every .py under app/ and asserts zero matches.
    """
    pattern = re.compile(r"==\s*settings\.AI_API_KEY|settings\.AI_API_KEY\s*==")
    offenders: list[tuple[Path, int, str]] = []
    for path in (REPO_ROOT / "app").rglob("*.py"):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                offenders.append((path, lineno, line.strip()))
    assert not offenders, f"Forbidden `==` on settings.AI_API_KEY: {offenders}"


def test_deps_uses_compare_digest():
    """Make sure secrets.compare_digest is actually referenced in deps.py."""
    text = (REPO_ROOT / "app" / "api" / "deps.py").read_text(encoding="utf-8")
    assert text.count("compare_digest") >= 1
