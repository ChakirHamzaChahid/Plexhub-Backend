"""Shared filesystem path-segment sanitization helpers (CR-A06)."""
from __future__ import annotations

import re
import unicodedata

_PATH_SEP_RE = re.compile(r"[\\/]+")
_PARENT_DIR_RE = re.compile(r"\.\.+")
_WIN_COLON_RE = re.compile(r"\s*:\s*")
_WIN_ILLEGAL_RE = re.compile(r'[*?"<>|]')
_ALL_INVALID_CHARS_RE = re.compile(r'[\\/:*?"<>|]')

MAX_SEGMENT_LEN = 180
RESERVED_WINDOWS_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)


def strip_unicode_control(text: str) -> str:
    """Drop ASCII and Unicode control/format/surrogate characters (Cc/Cf/Cs)."""
    return "".join(ch for ch in text if unicodedata.category(ch) not in ("Cc", "Cf", "Cs"))


def remap_ntfs_illegal_chars(text: str) -> str:
    """Remap NTFS-illegal characters (`:` -> ` - `, drop `* ? " < > |`)."""
    text = _WIN_COLON_RE.sub(" - ", text)
    return _WIN_ILLEGAL_RE.sub("", text)


def sanitize_path_segment(
    name: str,
    *,
    fallback: str = "Unknown",
    ntfs_remap: bool = False,
    max_len: int | None = None,
) -> str:
    """Sanitize a single path segment safely for POSIX and Windows/SMB."""
    text = unicodedata.normalize("NFC", name or "")
    text = strip_unicode_control(text)
    text = _PATH_SEP_RE.sub(" ", text)
    if ntfs_remap:
        text = remap_ntfs_illegal_chars(text)
    else:
        text = _ALL_INVALID_CHARS_RE.sub(" ", text)
    text = _PARENT_DIR_RE.sub(".", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = text.strip(". ")
    if max_len is not None:
        text = text[:max_len].strip(". ")
    if not text:
        return fallback
    if text.lower() in RESERVED_WINDOWS_NAMES:
        text = f"{text}_"
    return text
