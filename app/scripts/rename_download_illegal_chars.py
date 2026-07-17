"""One-shot migration: rename already-downloaded files/folders whose names
carry an NTFS-illegal character (`:` `*` `?` `"` `<` `>` `|`).

Context: `download_service._sanitize_segment` now remaps these at download time
(`remap_ntfs_illegal_chars`) — a `Title: Subtitle` folder would otherwise be
served over SMB/Samba to a Windows client under a mangled 8.3 short name (e.g.
`WISTO~1.MKV`). Files downloaded BEFORE that fix keep their old (illegal) names
on disk; this renames them in place to match. Renaming also realigns them with
what a fresh download now produces, so the worker's skip-if-exists keeps
recognizing them instead of re-downloading.

Reuses the CANONICAL `remap_ntfs_illegal_chars` so a renamed file is byte-for-
byte the name a fresh download would create for the same title.

SAFE BY DEFAULT — dry-run unless `--apply` is passed. Renames deepest-first
(files before their parent directories), never leaves the scanned root, and
skips (never clobbers) a rename whose target already exists.

    python -m app.scripts.rename_download_illegal_chars                   # dry-run
    python -m app.scripts.rename_download_illegal_chars --apply           # execute
    python -m app.scripts.rename_download_illegal_chars --root /data/download --apply
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

from app.config import settings
from app.services.download_service import remap_ntfs_illegal_chars

_WS_RE = re.compile(r"\s+")


def _fixed_name(name: str) -> str:
    """The NTFS-safe form of one path component (dir name or file name),
    matching `_sanitize_segment`'s illegal-char step: remap `:`/`* ? " < > |`,
    then collapse the whitespace the `:` -> ` - ` swap may introduce."""
    return _WS_RE.sub(" ", remap_ntfs_illegal_chars(name)).strip()


def _iter_paths_deepest_first(root: Path) -> list[Path]:
    """Every file and directory under `root`, DEEPEST paths first, so a child
    is always renamed before its parent directory's name changes. The whole
    tree is materialized up front (before any rename) so mutating it can't
    disturb the walk."""
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        base = Path(dirpath)
        out.extend(base / f for f in filenames)
        out.extend(base / d for d in dirnames)
    return out


def _escapes(root: Path, target: Path) -> bool:
    resolved = Path(os.path.realpath(target))
    return resolved != root and root not in resolved.parents


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Rename downloaded files/folders carrying NTFS-illegal characters.",
    )
    parser.add_argument(
        "--root", default=settings.DOWNLOAD_DIR or "",
        help="Directory to scan (default: DOWNLOAD_DIR).",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually rename. Omit for a dry-run (print planned renames only).",
    )
    args = parser.parse_args(argv)

    if not args.root:
        print("ERROR: no --root given and DOWNLOAD_DIR is unset.", file=sys.stderr)
        return 2
    root = Path(os.path.realpath(args.root))
    if not root.is_dir():
        print(f"ERROR: not a directory: {root}", file=sys.stderr)
        return 2

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] scanning {root}")

    renamed = collisions = skipped = 0
    for path in _iter_paths_deepest_first(root):
        old = path.name
        new = _fixed_name(old)
        if new == old:
            continue
        if not new:
            print(f"  SKIP (sanitizes to empty): {path}")
            skipped += 1
            continue
        target = path.with_name(new)
        if _escapes(root, target):
            print(f"  SKIP (escapes root): {path}")
            skipped += 1
            continue
        if target.exists():
            print(f"  COLLISION (target exists, left as-is): {old!r} -> {new!r}  in {path.parent}")
            collisions += 1
            continue
        print(f"  {old!r} -> {new!r}  in {path.parent}")
        if args.apply:
            os.rename(path, target)
        renamed += 1

    print(f"[{mode}] {renamed} rename(s), {collisions} collision(s), {skipped} skip(s)")
    if not args.apply and renamed:
        print("Re-run with --apply to perform the renames.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
