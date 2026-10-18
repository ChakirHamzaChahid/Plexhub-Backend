"""B1-06 : réglages de la passerelle de scraping (défauts, valeurs invalides, bornage)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from app.config import settings
from app.services import omdb_scrape_cache_service, scrape_cache_service, scrape_keys

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "app" / "config.py"
_DAY_MS = 86_400_000
_KEYS = (
    "SCRAPE_TTL_SUCCESS_DAYS",
    "SCRAPE_TTL_NEGATIVE_DAYS",
    "SCRAPE_RATE_LIMIT_PER_MINUTE",
    "SCRAPE_REQUEST_TIMEOUT_SECONDS",
)


def _fresh_settings(monkeypatch, tmp_path, **env):
    """Evaluate a private copy of app/config.py under a controlled environment.

    Never touches sys.modules (the shared `settings` singleton stays as is) and
    never reads a real .env (load_dotenv is neutralised)."""
    import dotenv

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)
    for key in _KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    spec = importlib.util.spec_from_file_location("_config_probe", _CONFIG_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Settings()


class TestDefaults:
    def test_defaults(self, monkeypatch, tmp_path):
        s = _fresh_settings(monkeypatch, tmp_path)
        assert s.SCRAPE_TTL_SUCCESS_DAYS == 30
        assert s.SCRAPE_TTL_NEGATIVE_DAYS == 3
        assert s.SCRAPE_RATE_LIMIT_PER_MINUTE == 300
        assert s.SCRAPE_REQUEST_TIMEOUT_SECONDS == 25

    def test_values_are_read_from_env(self, monkeypatch, tmp_path):
        s = _fresh_settings(
            monkeypatch, tmp_path,
            SCRAPE_TTL_SUCCESS_DAYS="90", SCRAPE_TTL_NEGATIVE_DAYS="7",
            SCRAPE_RATE_LIMIT_PER_MINUTE="60", SCRAPE_REQUEST_TIMEOUT_SECONDS="10",
        )
        assert (s.SCRAPE_TTL_SUCCESS_DAYS, s.SCRAPE_TTL_NEGATIVE_DAYS) == (90, 7)
        assert (s.SCRAPE_RATE_LIMIT_PER_MINUTE, s.SCRAPE_REQUEST_TIMEOUT_SECONDS) == (60, 10)

    @pytest.mark.parametrize("raw", ["abc", "", "3.5", "1e3"])
    def test_invalid_values_fall_back_to_defaults(self, monkeypatch, tmp_path, raw):
        s = _fresh_settings(
            monkeypatch, tmp_path,
            SCRAPE_TTL_SUCCESS_DAYS=raw, SCRAPE_TTL_NEGATIVE_DAYS=raw,
            SCRAPE_RATE_LIMIT_PER_MINUTE=raw, SCRAPE_REQUEST_TIMEOUT_SECONDS=raw,
        )
        assert s.SCRAPE_TTL_SUCCESS_DAYS == 30
        assert s.SCRAPE_TTL_NEGATIVE_DAYS == 3
        assert s.SCRAPE_RATE_LIMIT_PER_MINUTE == 300
        assert s.SCRAPE_REQUEST_TIMEOUT_SECONDS == 25


class TestBounds:
    @pytest.mark.parametrize(
        ("raw", "expected_days"),
        [(-5, 1), (0, 1), (1, 1), (30, 30), (365, 365), (366, 365), (10_000, 365)],
    )
    def test_ttl_success_is_clamped_to_1_365_days(self, monkeypatch, raw, expected_days):
        monkeypatch.setattr(settings, "SCRAPE_TTL_SUCCESS_DAYS", raw)
        assert scrape_keys.ttl_success_ms() == expected_days * _DAY_MS

    @pytest.mark.parametrize(
        ("raw", "expected_days"),
        [(-1, 1), (0, 1), (1, 1), (3, 3), (30, 30), (31, 30), (999, 30)],
    )
    def test_ttl_negative_is_clamped_to_1_30_days(self, monkeypatch, raw, expected_days):
        monkeypatch.setattr(settings, "SCRAPE_TTL_NEGATIVE_DAYS", raw)
        assert scrape_keys.ttl_negative_ms() == expected_days * _DAY_MS

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [(-3, 5), (0, 5), (4, 5), (5, 5), (25, 25), (28, 28), (29, 28), (600, 28)],
    )
    def test_timeout_is_clamped_to_5_28_seconds(self, monkeypatch, raw, expected):
        monkeypatch.setattr(settings, "SCRAPE_REQUEST_TIMEOUT_SECONDS", raw)
        assert scrape_keys.request_timeout_seconds() == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [(-10, 0), (0, 0), (1, 1), (300, 300), (100_000, 100_000)],
    )
    def test_rate_limit_non_positive_disables(self, monkeypatch, raw, expected):
        monkeypatch.setattr(settings, "SCRAPE_RATE_LIMIT_PER_MINUTE", raw)
        assert scrape_keys.rate_limit_per_minute() == expected

    def test_defaults_through_the_shared_settings(self):
        assert scrape_keys.ttl_success_ms() == 30 * _DAY_MS
        assert scrape_keys.ttl_negative_ms() == 3 * _DAY_MS
        assert scrape_keys.request_timeout_seconds() == 25
        assert scrape_keys.rate_limit_per_minute() == 300


class TestWorkerCachesUntouched:
    """Garde-fou FR-012 : les TTL de la passerelle ne touchent pas les caches existants."""

    def test_existing_ttl_constants_are_unchanged(self):
        assert scrape_cache_service.MATCH_TTL_MS == 30 * _DAY_MS
        assert scrape_cache_service.NEG_TTL_MS == 3 * _DAY_MS
        assert omdb_scrape_cache_service.MATCH_TTL_MS == 30 * _DAY_MS
        assert omdb_scrape_cache_service.NEG_TTL_MS == 3 * _DAY_MS
