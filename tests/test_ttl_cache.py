"""Tests for app.utils.ttl_cache.TTLCache."""
import time
from unittest.mock import patch

import pytest

from app.utils.ttl_cache import TTLCache


class TestTTLCache:
    def test_get_set_basic(self):
        c: TTLCache[str, int] = TTLCache(max_size=10, ttl_seconds=60)
        assert c.get("missing", default=None) is None
        c.set("a", 1)
        assert c.get("a") == 1

    def test_returns_default_sentinel_on_miss(self):
        c: TTLCache[str, str] = TTLCache(max_size=10, ttl_seconds=60)
        sentinel = object()
        assert c.get("nope", default=sentinel) is sentinel

    def test_distinguishes_cached_none_from_miss(self):
        c: TTLCache[str, None] = TTLCache(max_size=10, ttl_seconds=60)
        sentinel = object()
        c.set("k", None)  # legitimate "no match" cached value
        assert c.get("k", default=sentinel) is None
        assert c.get("absent", default=sentinel) is sentinel

    def test_ttl_expiry(self):
        c: TTLCache[str, int] = TTLCache(max_size=10, ttl_seconds=60)
        c.set("a", 42)
        with patch("time.monotonic", return_value=time.monotonic() + 61):
            assert c.get("a", default=None) is None
            assert len(c) == 0  # expired entry removed on access

    def test_lru_eviction(self):
        c: TTLCache[str, int] = TTLCache(max_size=2, ttl_seconds=60)
        c.set("a", 1)
        c.set("b", 2)
        c.get("a")  # touches a — b becomes oldest
        c.set("c", 3)  # evicts b
        assert c.get("a") == 1
        assert c.get("c") == 3
        assert c.get("b", default=None) is None
        assert len(c) == 2

    def test_set_existing_key_refreshes_ttl(self):
        c: TTLCache[str, int] = TTLCache(max_size=10, ttl_seconds=60)
        c.set("a", 1)
        c.set("a", 2)  # overwrite
        assert c.get("a") == 2
        assert len(c) == 1

    def test_clear(self):
        c: TTLCache[str, int] = TTLCache(max_size=10, ttl_seconds=60)
        c.set("a", 1)
        c.set("b", 2)
        c.clear()
        assert len(c) == 0
        assert c.get("a", default=None) is None

    def test_invalid_args(self):
        with pytest.raises(ValueError):
            TTLCache(max_size=0, ttl_seconds=60)
        with pytest.raises(ValueError):
            TTLCache(max_size=10, ttl_seconds=0)
