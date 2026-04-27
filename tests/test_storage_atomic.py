"""Tests for atomic write helpers in app.plex_generator.storage."""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from app.plex_generator.storage import (
    LocalStorage,
    _atomic_write_bytes,
    _atomic_write_text,
)


class TestAtomicWriteBytes:
    def test_creates_file(self, tmp_path):
        target = tmp_path / "sub" / "out.bin"
        _atomic_write_bytes(target, b"hello")
        assert target.read_bytes() == b"hello"
        # No .tmp leftover
        assert not (target.parent / "out.bin.tmp").exists()

    def test_overwrites_existing(self, tmp_path):
        target = tmp_path / "x.bin"
        target.write_bytes(b"old")
        _atomic_write_bytes(target, b"new")
        assert target.read_bytes() == b"new"

    def test_cleans_up_tmp_on_failure(self, tmp_path):
        target = tmp_path / "fail.bin"
        # Make os.replace blow up — simulates a cross-volume failure or perm issue.
        with patch("app.plex_generator.storage.os.replace", side_effect=OSError("boom")):
            with pytest.raises(OSError):
                _atomic_write_bytes(target, b"data")
        assert not target.exists()
        assert not (tmp_path / "fail.bin.tmp").exists()


class TestAtomicWriteText:
    def test_utf8_roundtrip(self, tmp_path):
        target = tmp_path / "t.txt"
        _atomic_write_text(target, "héllo — wörld")
        assert target.read_text(encoding="utf-8") == "héllo — wörld"


class TestLocalStorageAtomic:
    def test_write_strm_appends_newline_and_strips(self, tmp_path):
        storage = LocalStorage(tmp_path)
        storage.write_strm("Films/Foo (2020)/Foo (2020).strm", "  http://x/y.mp4  ")
        out = (tmp_path / "Films" / "Foo (2020)" / "Foo (2020).strm").read_text(encoding="utf-8")
        assert out == "http://x/y.mp4\n"

    def test_write_file_preserves_existing(self, tmp_path):
        storage = LocalStorage(tmp_path)
        target = tmp_path / "Films" / "Bar" / "movie.nfo"
        target.parent.mkdir(parents=True)
        target.write_text("MANUAL EDIT", encoding="utf-8")
        storage.write_file("Films/Bar/movie.nfo", "<would-overwrite/>")
        # Manual edit must survive.
        assert target.read_text(encoding="utf-8") == "MANUAL EDIT"

    def test_write_file_creates_when_missing(self, tmp_path):
        storage = LocalStorage(tmp_path)
        storage.write_file("Films/Baz/movie.nfo", "<movie/>")
        out = (tmp_path / "Films" / "Baz" / "movie.nfo").read_text(encoding="utf-8")
        assert out == "<movie/>"
