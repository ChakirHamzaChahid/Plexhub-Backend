"""F-007 — path confinement (BLOCKING security invariant), PH-DL-07 priority 1.

Test IDs (docs/40-testplan-media-download.md §3, F-007 table): DL-070..075.

`compute_dest_path` (defense: sanitizes every segment) and `resolve_confined`
(proof: realpath containment under `DOWNLOAD_DIR`) are the two halves of the
F-007 invariant (`app/services/download_service.py`). This file is the single
place that must prove "0 fichier écrit hors DOWNLOAD_DIR" — a failure here
blocks the release per the test plan's exit criteria.
"""
from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest

from app.services.download_service import (
    DownloadDisabledError,
    PathConfinementError,
    compute_dest_path,
    download_to_disk,
    resolve_confined,
)

# pytest-asyncio auto mode (pyproject.toml) — async tests need no decorator.


def _under_base(resolved: Path, base: Path) -> bool:
    resolved = Path(os.path.realpath(resolved))
    base = Path(os.path.realpath(base))
    return resolved == base or base in resolved.parents


# ─── DL-070: attack matrix through compute_dest_path (+ resolve_confined) ──

HOSTILE_MOVIE_TITLES = [
    pytest.param("../../../etc/passwd", id="dotdot-traversal"),
    pytest.param("a/b\\c", id="embedded-separators"),
    pytest.param("..", id="lone-parent-marker"),
    pytest.param("....", id="run-of-dots"),
    pytest.param("\x00\x01Title\x02", id="ascii-control-chars"),
    pytest.param("‮evil‬", id="unicode-rtl-override"),
    pytest.param("﻿Title​", id="unicode-bom-zero-width"),
    pytest.param("A" * 400, id="very-long-title"),
    pytest.param("   ...Title...   ", id="leading-trailing-dots-spaces"),
    pytest.param("", id="empty-title"),
    pytest.param("CON", id="windows-reserved-con"),
    pytest.param("NUL", id="windows-reserved-nul"),
    pytest.param("COM1", id="windows-reserved-com1"),
]


class TestComputeDestPathAttackMatrix:
    """DL-070: `compute_dest_path` + `resolve_confined` against pathological
    movie titles — the resolved path must always land strictly under
    `DOWNLOAD_DIR`, and no raw '..'/separator must survive as its own path
    component (no extra traversal component introduced)."""

    @pytest.mark.parametrize("title", HOSTILE_MOVIE_TITLES)
    def test_movie_title_stays_confined(self, download_dir, title):
        dest = compute_dest_path(
            media_type="movie", title=title, year=2020,
            season=None, episode=None, ext="mkv",
        )
        assert not dest.startswith("/"), "dest_path must be relative"
        parts = dest.split("/")
        assert len(parts) == 3, f"unexpected path depth from a hostile title: {dest!r}"
        assert parts[0] == "Movies"
        # No raw '..' must survive as an isolated path segment inside the
        # sanitized folder/file names (defense-in-depth check on top of the
        # realpath proof below).
        for seg in (parts[1], parts[2].rsplit(".", 1)[0]):
            assert seg.strip(".") != "" or seg == "", "a segment collapsed to only dots"
            assert ".." not in seg.split(" "), f"a raw '..' token survived: {seg!r}"

        resolved = resolve_confined(dest)
        assert _under_base(resolved, download_dir), (
            f"hostile title {title!r} escaped DOWNLOAD_DIR: {resolved}"
        )

    @pytest.mark.parametrize("title", HOSTILE_MOVIE_TITLES)
    def test_episode_title_stays_confined(self, download_dir, title):
        dest = compute_dest_path(
            media_type="episode", title=title, year=2020,
            season=1, episode=2, ext="mkv",
        )
        parts = dest.split("/")
        assert parts[0] == "Series"
        assert parts[2] == "Season 01"
        resolved = resolve_confined(dest)
        assert _under_base(resolved, download_dir)

    def test_hostile_extension_is_sanitized(self, download_dir):
        dest = compute_dest_path(
            media_type="movie", title="Normal Title", year=2020,
            season=None, episode=None, ext="mkv/../../evil",
        )
        resolved = resolve_confined(dest)
        assert _under_base(resolved, download_dir)
        # The extension segment must not smuggle extra path components either.
        assert dest.count("/") == 2

    def test_reserved_windows_name_as_bare_show_title_gets_suffixed(self, download_dir):
        """A show folder is the RAW sanitized title (no year suffix, unlike a
        movie's `<Title> (<Year>)`), so a literal Windows-reserved device name
        must be defused by `_sanitize_segment`'s reserved-name guard."""
        dest = compute_dest_path(
            media_type="episode", title="CON", year=None,
            season=1, episode=1, ext="mkv",
        )
        show_folder = dest.split("/")[1]
        assert show_folder.lower() != "con", "reserved Windows device name leaked verbatim"
        resolved = resolve_confined(dest)
        assert _under_base(resolved, download_dir)

    def test_no_year_movie_title_equal_to_reserved_name_is_suffixed(self, download_dir):
        """A movie with no `year` has no "(Year)" suffix either — same
        reserved-name exposure as the show case above."""
        dest = compute_dest_path(
            media_type="movie", title="NUL", year=None,
            season=None, episode=None, ext="mkv",
        )
        folder = dest.split("/")[1]
        assert folder.lower() != "nul"


# ─── DL-071: resolve_confined fed a hand-crafted malicious relative path,
# BYPASSING compute_dest_path entirely — simulates a corrupted DB row or a
# future regression that skips the sanitizer. Must ALWAYS raise, never write.

# `\` and `C:` are path separators / drive anchors ONLY on Windows. On the
# Linux/Docker deploy target (§1) they are valid filename characters, so these
# two strings stay CONFINED under DOWNLOAD_DIR and `resolve_confined` correctly
# does NOT raise (defense-in-depth against odd chars lives in `_sanitize_segment`,
# exercised via `compute_dest_path`). Run them only where they're real escapes.
_WINDOWS_ONLY = pytest.mark.skipif(
    os.name != "nt",
    reason="backslash/drive-letter traversal is a Windows-only escape; on POSIX "
    "these are ordinary filename chars that remain confined (see _sanitize_segment)",
)

DIRECT_ATTACK_PATHS = [
    pytest.param("../../etc/passwd", id="relative-traversal"),
    pytest.param("/etc/passwd", id="posix-absolute"),
    pytest.param("..\\..\\windows\\system32", id="windows-backslash-traversal", marks=_WINDOWS_ONLY),
    pytest.param("C:/Windows/system32", id="windows-drive-absolute", marks=_WINDOWS_ONLY),
    pytest.param("..", id="bare-parent"),
    pytest.param("../", id="bare-parent-slash"),
    pytest.param("Movies/../../escape.txt", id="nested-double-dotdot-escape"),
]


class TestResolveConfinedDirectBypass:
    """DL-071: `resolve_confined` is the actual proof — it must reject an
    escape even when `compute_dest_path`'s sanitizer is skipped entirely."""

    @pytest.mark.parametrize("rel_path", DIRECT_ATTACK_PATHS)
    def test_raises_path_confinement_error(self, download_dir, rel_path):
        with pytest.raises(PathConfinementError):
            resolve_confined(rel_path)
        # Nothing must exist on disk as a side effect of the attempted resolve.
        for _root, _dirs, files in os.walk(download_dir):
            assert files == [], "resolve_confined must never create files as a side effect"

    def test_internal_dotdot_that_still_lands_inside_base_is_accepted(self, download_dir):
        """`Movies/../escape.txt` cancels out to `<DOWNLOAD_DIR>/escape.txt` —
        still INSIDE DOWNLOAD_DIR (not a bypass, just not under `Movies/`).
        `resolve_confined` only proves the FINAL location is confined, so this
        must be accepted, not rejected."""
        resolved = resolve_confined("Movies/../escape.txt")
        assert _under_base(resolved, download_dir)

    def test_legit_nested_path_accepted(self, download_dir):
        resolved = resolve_confined("Movies/Foo (2020)/Foo (2020).mkv")
        assert _under_base(resolved, download_dir)
        assert resolved != Path(os.path.realpath(download_dir))


# ─── DL-072: symlink escape (defense in depth) ─────────────────────────────


class TestSymlinkEscape:
    """A symlink placed INSIDE DOWNLOAD_DIR pointing OUTSIDE it must still be
    caught by the realpath-based containment check — confinement is not
    bypassable by a link. Skipped where the host/user can't create symlinks
    (e.g. Windows without Developer Mode / elevated privileges) — a
    testability limitation documented in the test plan (§6.3), not a pass/
    fail signal for the code under test."""

    def test_symlink_inside_pointing_outside_is_rejected(self, download_dir, tmp_path):
        outside = tmp_path / "outside_secret"
        outside.mkdir()
        link = download_dir / "escape_link"
        try:
            os.symlink(outside, link, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"symlink creation not permitted on this host: {exc}")

        with pytest.raises(PathConfinementError):
            resolve_confined("escape_link/evil.mkv")


# ─── DL-073: end-to-end filesystem proof — "0 fichier écrit hors DOWNLOAD_DIR" ─


class TestZeroWritesOutsideDownloadDir:
    """DL-073: the single test that proves the PRODUCT invariant end-to-end —
    a hostile title, run through the REAL sanitize -> confine -> stream-to-
    disk chain, must never touch anything outside `DOWNLOAD_DIR`. The canary
    sits at the PARENT of `DOWNLOAD_DIR` (not just anywhere under `tmp_path`)
    so a one-level `../` escape would actually be caught (test plan risk #5)."""

    async def test_hostile_title_writes_only_inside_download_dir(
        self, download_dir, xtream_mock,
    ):
        canary = download_dir.parent / "canary.txt"
        canary.write_text("do not touch", encoding="utf-8")
        canary_before = canary.read_bytes()

        dest_rel = compute_dest_path(
            media_type="movie",
            title="../../../etc/passwd\x00‮",
            year=1999, season=None, episode=None, ext="mkv",
        )
        dest = resolve_confined(dest_rel)

        url = "http://fake-xtream.example/movie/user/pass/1.mkv"
        body = b"video-bytes-payload"
        xtream_mock.get(url).mock(
            return_value=httpx.Response(200, content=body, headers={"Content-Length": str(len(body))})
        )

        result = await download_to_disk(url, dest)
        assert result.bytes_downloaded == len(body)

        # The canary at DOWNLOAD_DIR's parent must be byte-identical.
        assert canary.read_bytes() == canary_before

        # Walk the ENTIRE tmp_path tree: every file found must be either the
        # untouched canary or a file that lives under download_dir.
        base_real = Path(os.path.realpath(download_dir))
        for root, _dirs, files in os.walk(download_dir.parent):
            for name in files:
                full = Path(os.path.realpath(os.path.join(root, name)))
                if full == Path(os.path.realpath(canary)):
                    continue
                assert full == base_real or base_real in full.parents, (
                    f"file written OUTSIDE DOWNLOAD_DIR: {full}"
                )
        assert dest.exists()
        assert dest.read_bytes() == body


# ─── DL-074: DOWNLOAD_DIR unset guard ──────────────────────────────────────


class TestDownloadDisabledGuard:
    def test_resolve_confined_raises_disabled_not_confinement_error(self, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "DOWNLOAD_DIR", "")
        with pytest.raises(DownloadDisabledError):
            resolve_confined("Movies/x/x.mkv")


# ─── DL-075: no client-suppliable path field, anywhere on the wire ─────────


class TestNoClientSuppliedPathField:
    """Anti-regression for the exact CR-S01 pattern (`outputDir` verbatim on
    `POST /api/plex/generate`) — the download feature must never accept a
    path/destination field from the client, on EITHER the JSON or the HTMX
    surface."""

    def test_download_enqueue_request_schema_has_no_path_field(self):
        from app.models.schemas import DownloadEnqueueRequest

        fields = set(DownloadEnqueueRequest.model_fields.keys())
        forbidden = {"path", "dest_path", "destPath", "output_dir", "outputDir", "dir"}
        assert not (fields & forbidden), f"a path-like field leaked into the enqueue schema: {fields}"
        assert fields == {"type", "unification_id", "server_id", "rating_key", "scope"}

    def test_admin_downloads_enqueue_form_has_no_path_field(self):
        import inspect

        from app.api.admin_downloads import admin_downloads_enqueue

        sig = inspect.signature(admin_downloads_enqueue)
        forbidden = {"path", "dest_path", "output_dir", "dir"}
        assert not (set(sig.parameters) & forbidden)
