"""Invariant: every environment variable read by `app/config.py` is documented
in `.env.example` (commented-out placeholders count).

CLAUDE.md §4 states this as a rule ("`.env.example` couvre 100 % des
`os.getenv` de `config.py`") that had only ever been checked by hand, which is
how three keys went missing between two lots. This makes it a test.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_READERS = re.compile(r'(?:os\.getenv|_safe_int|_safe_float)\(\s*"([A-Z][A-Z0-9_]*)"')


def test_every_config_env_var_is_documented_in_env_example():
    config_src = (ROOT / "app" / "config.py").read_text(encoding="utf-8")
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")

    names = set(_READERS.findall(config_src))
    assert len(names) > 50, "the scan regex no longer matches config.py's style"

    missing = sorted(
        n for n in names
        if not re.search(rf"^\s*#?\s*{re.escape(n)}\s*=", env_example, re.MULTILINE)
    )
    assert missing == [], f"undocumented in .env.example: {missing}"
