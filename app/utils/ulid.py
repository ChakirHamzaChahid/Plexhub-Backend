"""ULID generator for the metadata edit journal (ADR 0011 D2.5).

26 Crockford-base32 characters: 48 bits of milliseconds then 80 random bits, so
ids sort by time. Monotonic inside the process: two ids issued in the same
millisecond (or after the clock stepped back) still strictly increase, because
the random part is then incremented instead of redrawn.
"""
from __future__ import annotations

import secrets
import threading

from app.utils.time import now_ms

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_MASK_80 = (1 << 80) - 1
_lock = threading.Lock()
_last_ms = -1
_last_rand = 0


def _encode(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        chars.append(_ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(chars))


def new_ulid(ms: int | None = None) -> str:
    global _last_ms, _last_rand
    ms = now_ms() if ms is None else ms
    with _lock:
        if ms <= _last_ms:
            ms = _last_ms
            _last_rand = (_last_rand + 1) & _MASK_80
        else:
            _last_ms = ms
            _last_rand = secrets.randbits(80)
        return _encode(ms, 10) + _encode(_last_rand, 16)


def decode_time_ms(ulid: str) -> int:
    value = 0
    for ch in ulid[:10]:
        value = (value << 5) | _ALPHABET.index(ch)
    return value
