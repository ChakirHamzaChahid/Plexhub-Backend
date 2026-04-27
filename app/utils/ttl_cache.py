"""Tiny in-memory TTL+LRU cache used for TMDB search results.

Bounded in size and time to keep memory predictable on the 1 GB Docker limit.
Single-asyncio-loop assumption — no locking.
"""
from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any, Generic, Hashable, TypeVar

K = TypeVar("K", bound=Hashable)
V = TypeVar("V")

_MISS = object()


class TTLCache(Generic[K, V]):
    """Bounded LRU cache with per-entry TTL.

    Eviction order: expired first, then least-recently-used.
    """

    def __init__(self, max_size: int, ttl_seconds: float):
        if max_size <= 0:
            raise ValueError("max_size must be > 0")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be > 0")
        self._max_size = max_size
        self._ttl = ttl_seconds
        self._data: OrderedDict[K, tuple[V, float]] = OrderedDict()

    def __len__(self) -> int:
        return len(self._data)

    def get(self, key: K, default: Any = _MISS) -> V | Any:
        entry = self._data.get(key)
        if entry is None:
            return default
        value, expires_at = entry
        if time.monotonic() >= expires_at:
            del self._data[key]
            return default
        # Move to end → most recently used.
        self._data.move_to_end(key)
        return value

    def set(self, key: K, value: V) -> None:
        if key in self._data:
            self._data.move_to_end(key)
        self._data[key] = (value, time.monotonic() + self._ttl)
        # Evict oldest until under capacity.
        while len(self._data) > self._max_size:
            self._data.popitem(last=False)

    def clear(self) -> None:
        self._data.clear()
