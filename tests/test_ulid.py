"""B2 — `new_ulid` : identifiant d'édition trié par le temps et monotone (ADR 0011 § D2.5)."""
from __future__ import annotations

import re

import pytest

from app.utils import ulid

ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


@pytest.fixture(autouse=True)
def _fresh_generator(monkeypatch):
    """The generator is process-local state: isolate every test from the others."""
    monkeypatch.setattr(ulid, "_last_ms", -1)
    monkeypatch.setattr(ulid, "_last_rand", 0)


def test_format_is_a_26_char_crockford_ulid():
    assert ULID_RE.match(ulid.new_ulid())


def test_ids_are_strictly_increasing_even_within_one_millisecond():
    ids = [ulid.new_ulid(1_700_000_000_000) for _ in range(1000)]
    assert ids == sorted(ids) and len(set(ids)) == 1000


def test_a_later_millisecond_sorts_after_an_earlier_one():
    assert ulid.new_ulid(1_700_000_000_000) < ulid.new_ulid(1_700_000_000_001)


def test_the_clock_going_backwards_never_breaks_monotonicity():
    first = ulid.new_ulid(1_700_000_005_000)
    second = ulid.new_ulid(1_700_000_000_000)  # clock stepped back
    assert second > first


def test_timestamp_prefix_encodes_the_milliseconds():
    a = ulid.new_ulid(1_000)
    assert ulid.decode_time_ms(a) == 1_000
