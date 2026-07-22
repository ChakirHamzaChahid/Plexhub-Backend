"""In-memory virtual filesystem model for the DAV tree.

An `DavTree` is a read-only, flat snapshot of the whole `.strm`-mirroring
hierarchy (see `app/dav/tree_builder.py`) that the WebDAV router (`app/api/
dav.py`, out of this ticket's scope) walks to answer PROPFIND/HEAD/GET.
Rebuilding it is expensive (aggregates the whole media catalogue — same cost
as a `.strm` generation pass, see `plex_generator.source.DatabaseSource`), so
`DavTreeCache` guards it with a TTL + single-flight lock: concurrent DAV
requests hitting a cold/expired cache trigger exactly ONE rebuild, the rest
await it instead of each kicking off their own scan.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from app.config import settings

# Fixed mtime reported to WebDAV clients (rclone/Plex) for every entry. The
# underlying Xtream stream has no meaningful "last modified", and an
# ever-changing mtime reads as churn to rclone's VFS cache / Plex's scanner
# (unnecessary re-caching/re-scanning). 2024-01-01T00:00:00Z, epoch seconds.
DAV_STABLE_MTIME = 1704067200


@dataclass(slots=True)
class DavEntry:
    """One node of the virtual tree — a directory or a file.

    `server_id`/`rating_key` are only meaningful on files (`is_dir=False`):
    they let the DAV router resolve a GET straight back to the (Xtream
    account, stream) pair the file represents, with zero extra DB lookup.
    """

    name: str
    is_dir: bool
    size: int | None = None  # bytes; always None for a directory
    mtime: int = DAV_STABLE_MTIME
    server_id: str | None = None
    rating_key: str | None = None


def _normalize(rel_path: str) -> str:
    """Normalize a relative DAV path to the flat-dict key convention used by
    `DavTree`: no leading/trailing slash, `""` denotes the root."""
    return rel_path.strip("/")


@dataclass
class DavTree:
    """Flat, read-only snapshot of the whole virtual hierarchy.

    `entries` maps a normalized relative path -> its `DavEntry` (both files
    AND directories, including the root `""`). `children` maps a normalized
    *directory* path -> the sorted list of its direct children's *names*
    (not full paths) — sorted once at build time so PROPFIND listings/rclone
    directory reads are deterministic across rebuilds.
    """

    entries: dict[str, DavEntry] = field(default_factory=dict)
    children: dict[str, list[str]] = field(default_factory=dict)
    built_at: float = 0.0

    def lookup(self, rel_path: str) -> DavEntry | None:
        """The entry (file or directory) at `rel_path`, or None if it
        doesn't exist in this snapshot."""
        return self.entries.get(_normalize(rel_path))

    def list_dir(self, rel_path: str) -> list[tuple[str, DavEntry]] | None:
        """Direct children of the directory at `rel_path` as `(name, entry)`
        pairs, in the tree's deterministic sort order — or None if
        `rel_path` doesn't resolve to a known directory (missing, or a
        file)."""
        norm = _normalize(rel_path)
        entry = self.entries.get(norm)
        if entry is None or not entry.is_dir:
            return None
        out: list[tuple[str, DavEntry]] = []
        for name in self.children.get(norm, []):
            child_path = f"{norm}/{name}" if norm else name
            child = self.entries.get(child_path)
            if child is not None:
                out.append((name, child))
        return out


class DavTreeCache:
    """Lazy, TTL-bounded, single-flight cache of the built `DavTree`.

    Uses the classic check → lock → re-check pattern (mirrors `app/dav/
    throttle.py`'s `AccountThrottle._get_semaphore`, same house convention):
    the fast path (fresh cache) never touches the lock; a cold/expired cache
    makes every concurrent caller contend on the lock, but only the FIRST one
    through actually rebuilds — the rest observe the now-fresh cache on their
    own re-check and return it without triggering a second build.
    """

    def __init__(self) -> None:
        self._tree: DavTree | None = None
        self._lock = asyncio.Lock()

    def invalidate(self) -> None:
        """Force the next `get()` to rebuild, regardless of TTL. Called after
        a fresh library generation (see `plex_generation_service`) so the DAV
        tree reflects new/removed content without waiting out the TTL."""
        self._tree = None

    def _is_expired(self) -> bool:
        if self._tree is None:
            return True
        ttl_seconds = settings.DAV_TREE_TTL_MINUTES * 60
        if ttl_seconds <= 0:
            # 0 (or a misconfigured negative value) means "never expire on
            # its own" — the tree is only refreshed via explicit invalidate().
            return False
        return (time.monotonic() - self._tree.built_at) >= ttl_seconds

    async def get(self) -> DavTree:
        if not self._is_expired():
            return self._tree  # type: ignore[return-value]
        async with self._lock:
            # Re-check: another caller may have rebuilt while we waited for
            # the lock — don't rebuild twice for one expiry.
            if self._is_expired():
                # Deferred import: tree_builder imports DavEntry/DavTree from
                # this module, so importing it at module scope here would be
                # a circular import. This lookup happens at call time, which
                # is also what makes `monkeypatch.setattr(tree_builder,
                # "build_dav_tree", ...)` observable from here in tests.
                from app.dav.tree_builder import build_dav_tree

                self._tree = await build_dav_tree()
                self._tree.built_at = time.monotonic()
            return self._tree  # type: ignore[return-value]


# Singleton — one cache per process (mirrors `app/dav/throttle.py`'s
# `account_throttle` singleton convention).
dav_tree_cache = DavTreeCache()
