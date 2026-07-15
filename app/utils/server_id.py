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


# --- Plex shared-servers download source (feature "Télécharger Plex") ------
# Additive: a Plex-sourced download_job.server_id is prefixed "plex_" instead
# of "xtream_" — the two namespaces never collide since PLEX_SERVER_ID_PREFIX
# != SERVER_ID_PREFIX and neither is a prefix of the other.
PLEX_SERVER_ID_PREFIX = "plex_"


def build_plex_server_id(client_identifier: str) -> str:
    """Build server_id from a Plex machine clientIdentifier."""
    return f"{PLEX_SERVER_ID_PREFIX}{client_identifier}"


def parse_plex_server_id(server_id: str) -> str | None:
    """Extract clientIdentifier from 'plex_{clientIdentifier}'. None if not a Plex server_id."""
    if server_id and server_id.startswith(PLEX_SERVER_ID_PREFIX):
        return server_id[len(PLEX_SERVER_ID_PREFIX):]
    return None


def is_plex_server_id(server_id: str) -> bool:
    return bool(server_id) and server_id.startswith(PLEX_SERVER_ID_PREFIX)
