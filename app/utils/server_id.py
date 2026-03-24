"""Utilities for parsing server_id format."""

SERVER_ID_PREFIX = "xtream_"


def parse_server_id(server_id: str) -> str | None:
    """Extract account_id from 'xtream_{account_id}' format. Returns None if invalid."""
    if server_id and server_id.startswith(SERVER_ID_PREFIX):
        return server_id[len(SERVER_ID_PREFIX):]
    return None


def build_server_id(account_id: str) -> str:
    """Build server_id from account_id."""
    return f"{SERVER_ID_PREFIX}{account_id}"
