"""Guard tests for the Plex server_id helpers (feature "Télécharger Plex",
Tâche C1 fondations).

Mirrors the existing Xtream `build_server_id`/`parse_server_id` pair but
under the "plex_" namespace. The two namespaces must never collide: a
download_job discriminated by `server_id` prefix ("xtream_<id>" vs
"plex_<clientIdentifier>") relies on that isolation.
"""
from __future__ import annotations

from app.utils.server_id import (
    PLEX_SERVER_ID_PREFIX,
    SERVER_ID_PREFIX,
    build_plex_server_id,
    build_server_id,
    is_plex_server_id,
    parse_plex_server_id,
    parse_server_id,
)


def test_prefixes_are_distinct_and_not_substrings_of_each_other():
    assert PLEX_SERVER_ID_PREFIX != SERVER_ID_PREFIX
    assert not SERVER_ID_PREFIX.startswith(PLEX_SERVER_ID_PREFIX)
    assert not PLEX_SERVER_ID_PREFIX.startswith(SERVER_ID_PREFIX)


def test_build_plex_server_id_round_trip():
    client_identifier = "abc123-def456-machine-id"
    server_id = build_plex_server_id(client_identifier)
    assert server_id == "plex_abc123-def456-machine-id"
    assert parse_plex_server_id(server_id) == client_identifier


def test_build_plex_server_id_with_uuid_like_identifier():
    client_identifier = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
    server_id = build_plex_server_id(client_identifier)
    assert parse_plex_server_id(server_id) == client_identifier


def test_parse_plex_server_id_returns_none_for_xtream_server_id():
    xtream_id = build_server_id("some-account")
    assert xtream_id.startswith("xtream_")
    assert parse_plex_server_id(xtream_id) is None


def test_parse_server_id_returns_none_for_plex_server_id():
    """The pre-existing Xtream parser must NOT match a plex_-prefixed id —
    critical for the download_job.server_id discriminant to stay unambiguous.
    """
    plex_id = build_plex_server_id("machine-id")
    assert parse_server_id(plex_id) is None


def test_parse_plex_server_id_returns_none_for_empty_string():
    assert parse_plex_server_id("") is None


def test_parse_plex_server_id_returns_none_for_unrelated_string():
    assert parse_plex_server_id("not-a-server-id") is None


def test_is_plex_server_id():
    assert is_plex_server_id(build_plex_server_id("machine-id")) is True
    assert is_plex_server_id(build_server_id("account-id")) is False
    assert is_plex_server_id("") is False
    assert is_plex_server_id("plex_") is True  # prefix-only edge case: still "starts with"


def test_plex_and_xtream_ids_for_the_same_raw_value_never_collide():
    raw = "shared-raw-value"
    xtream_id = build_server_id(raw)
    plex_id = build_plex_server_id(raw)
    assert xtream_id != plex_id
    assert parse_server_id(xtream_id) == raw
    assert parse_plex_server_id(plex_id) == raw
    assert parse_plex_server_id(xtream_id) is None
    assert parse_server_id(plex_id) is None
