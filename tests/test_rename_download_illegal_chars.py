"""tests/test_rename_download_illegal_chars.py — the one-shot on-disk rename
migration for NTFS-illegal characters (app/scripts/rename_download_illegal_chars.py).

The filesystem tests are POSIX-only: creating the very fixtures this migrates
(names with `: " ? * < > |`) is impossible on NTFS — which is exactly why the
bug only exists on a Linux volume served over Samba. The pure-string
`_fixed_name` check runs everywhere.
"""
from __future__ import annotations

import os

import pytest

from app.scripts.rename_download_illegal_chars import _fixed_name, main

_POSIX_ONLY = pytest.mark.skipif(
    os.name == "nt", reason="NTFS cannot create the illegal-named fixtures this migrates",
)


def test_fixed_name_matches_sanitizer_illegal_char_step():
    # Same output as download_service._sanitize_segment's illegal-char step.
    assert _fixed_name("Wistoria: Wand and Sword (2024)") == "Wistoria - Wand and Sword (2024)"
    assert _fixed_name('a"b*c?d<e>f|g') == "abcdefg"
    assert _fixed_name("The Matrix (1999)") == "The Matrix (1999)"  # clean -> unchanged


def _make_tree(root):
    movie = root / "Wistoria: Wand and Sword (2024)"
    movie.mkdir(parents=True)
    (movie / "Wistoria: Wand and Sword (2024).mkv").write_bytes(b"x")

    ep_dir = root / "Series" / "Code Geass: Lelouch" / "Season 01"
    ep_dir.mkdir(parents=True)
    (ep_dir / 'Code Geass: Lelouch - S01E01 "Pilot"?.mkv').write_bytes(b"y")

    clean = root / "The Matrix (1999)"
    clean.mkdir()
    (clean / "The Matrix (1999).mkv").write_bytes(b"z")


@_POSIX_ONLY
class TestDryRun:
    def test_dry_run_changes_nothing(self, tmp_path, capsys):
        _make_tree(tmp_path)
        rc = main(["--root", str(tmp_path)])
        assert rc == 0
        # The original illegal-named paths are all still present.
        assert (tmp_path / "Wistoria: Wand and Sword (2024)").is_dir()
        assert (tmp_path / "Series" / "Code Geass: Lelouch").is_dir()
        out = capsys.readouterr().out
        assert "DRY-RUN" in out
        assert "Re-run with --apply" in out


@_POSIX_ONLY
class TestApply:
    def test_apply_renames_illegal_and_leaves_clean_untouched(self, tmp_path):
        _make_tree(tmp_path)
        assert main(["--root", str(tmp_path), "--apply"]) == 0

        # Movie: colon -> ' - ' on BOTH the folder and the file.
        newdir = tmp_path / "Wistoria - Wand and Sword (2024)"
        assert newdir.is_dir()
        assert (newdir / "Wistoria - Wand and Sword (2024).mkv").is_file()
        assert not (tmp_path / "Wistoria: Wand and Sword (2024)").exists()

        # Series: show folder + episode file both cleaned (deepest-first worked).
        show = tmp_path / "Series" / "Code Geass - Lelouch"
        assert show.is_dir()
        season_files = list((show / "Season 01").iterdir())
        assert len(season_files) == 1
        assert not (set('<>:"|?*') & set(season_files[0].name))

        # A clean tree is left exactly as-is.
        assert (tmp_path / "The Matrix (1999)" / "The Matrix (1999).mkv").is_file()

    def test_collision_never_clobbers_existing_target(self, tmp_path, capsys):
        (tmp_path / "A: B").mkdir()      # illegal-named source
        (tmp_path / "A - B").mkdir()     # its sanitized target already exists
        assert main(["--root", str(tmp_path), "--apply"]) == 0
        assert (tmp_path / "A: B").is_dir(), "source must be left intact on collision"
        assert (tmp_path / "A - B").is_dir()
        assert "COLLISION" in capsys.readouterr().out


def test_missing_root_returns_error_code(capsys, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "DOWNLOAD_DIR", "")
    assert main([]) == 2
    assert "DOWNLOAD_DIR is unset" in capsys.readouterr().err
