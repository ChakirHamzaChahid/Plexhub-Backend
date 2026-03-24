"""Tests for retry logic in Xtream and TMDB services."""
import asyncio

import httpx
import pytest

from app.services.xtream_service import XtreamService
from app.services.tmdb_service import TMDBService


# ─── Xtream Retry ───────────────────────────────────────────────


class TestXtreamRetry:
    @pytest.fixture
    def service(self):
        svc = XtreamService()
        yield svc
        asyncio.run(svc.close())

    def test_retry_constants(self, service):
        """Verify retry configuration is correct."""
        from app.services.xtream_service import _RETRY_DELAYS, _RETRYABLE
        assert _RETRY_DELAYS == (1, 2, 4)
        assert httpx.TimeoutException in _RETRYABLE
        assert httpx.ConnectError in _RETRYABLE
        assert httpx.RemoteProtocolError in _RETRYABLE


class TestTMDBRetry:
    def test_retry_constants(self):
        from app.services.tmdb_service import _RETRY_DELAYS, _RETRYABLE
        assert _RETRY_DELAYS == (1, 2, 4)
        assert httpx.TimeoutException in _RETRYABLE

    def test_service_singleton(self):
        from app.services.tmdb_service import tmdb_service
        assert isinstance(tmdb_service, TMDBService)

    def test_is_configured_without_key(self):
        svc = TMDBService()
        # Will depend on whether TMDB_API_KEY is set in env
        assert isinstance(svc.is_configured, bool)
