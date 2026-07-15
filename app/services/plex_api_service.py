"""PH-PLEX-02: pure HTTP client for the Plex.tv discovery API and a Plex Media
Server's REST API (docs/10-prd-media-download.md — feature "Télécharger Plex").

Mirrors `app/services/xtream_service.py`'s shape: a module-level singleton
(`plex_api_service`), a lazily-created/reused `httpx.AsyncClient`, and the
same exponential backoff (`_RETRY_DELAYS = (1, 2, 4)`) on transport errors and
429/502/503/504 (honoring `Retry-After` when present).

Secrets invariant (house-law piège "secrets jamais loggés"): every token
(`account_token` for plex.tv discovery, the per-server `accessToken` for a
PMS) is sent ONLY as the `X-Plex-Token` HEADER — it is NEVER interpolated
into a URL or query string, so a URL logged/echoed anywhere in this module is
always token-free by construction. No method here ever logs request headers.
Every raised error is a `PlexApiError` carrying a message THIS module built
(operation name + exception class name / HTTP status) — never the raw httpx
exception `str()` (which, for `HTTPStatusError`, embeds the request URL) —
and always via `raise ... from None` so the original exception (and its
`__traceback__`/`request` attributes) is dropped from the propagated chain.
`probe()` additionally never raises at all: any failure (timeout, transport
error, non-200, non-JSON body) is reported as `False`.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from app.config import settings

logger = logging.getLogger("plexhub.plex_api")

_PLEX_TV_BASE = "https://plex.tv"
_RESOURCES_PATH = "/api/v2/resources"
_PAGE_SIZE = 500

_RETRY_DELAYS = (1, 2, 4)  # seconds: 1s, 2s, 4s
_RETRYABLE = (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError)
_RETRYABLE_STATUSES = (429, 502, 503, 504)


class PlexApiError(Exception):
    """Any non-recoverable Plex API failure.

    The message is always a bounded, secret-free description (operation name
    + error class/status) built by this module — never the raw httpx
    exception text, which can embed the request URL.
    """


@dataclass(frozen=True)
class PlexConnectionDTO:
    protocol: str
    address: str
    port: int
    uri: str
    local: bool
    relay: bool


@dataclass(frozen=True)
class PlexResourceDTO:
    """One `plex.tv` discovered resource that provides "server" (owned or
    shared with this account)."""

    name: str
    client_identifier: str
    owned: bool
    owner_title: Optional[str]
    access_token: str  # per-server secret — never logged
    connections: list[PlexConnectionDTO] = field(default_factory=list)


def _parse_connections(raw: Any) -> list[PlexConnectionDTO]:
    out: list[PlexConnectionDTO] = []
    if not isinstance(raw, list):
        return out
    for c in raw:
        if not isinstance(c, dict):
            continue
        try:
            port = int(c.get("port") or 0)
        except (TypeError, ValueError):
            port = 0
        out.append(
            PlexConnectionDTO(
                protocol=c.get("protocol") or "https",
                address=c.get("address") or "",
                port=port,
                uri=c.get("uri") or "",
                local=bool(c.get("local")),
                relay=bool(c.get("relay")),
            )
        )
    return out


def _extract_list(data: Any, key: str) -> list[dict]:
    if not isinstance(data, dict):
        return []
    container = data.get("MediaContainer")
    if not isinstance(container, dict):
        return []
    items = container.get(key)
    if not isinstance(items, list):
        return []
    return [it for it in items if isinstance(it, dict)]


def parse_guids(metadata: dict) -> dict:
    """Extract `{imdb_id, tmdb_id, tvdb_id}` from a Plex Metadata's `Guid[]`
    (e.g. `[{"id": "imdb://tt123"}, {"id": "tmdb://456"}]`). Missing/absent
    `Guid` yields all-`None`. Never raises."""
    result: dict[str, Optional[str]] = {"imdb_id": None, "tmdb_id": None, "tvdb_id": None}
    if not isinstance(metadata, dict):
        return result
    guids = metadata.get("Guid")
    if not isinstance(guids, list):
        return result
    for g in guids:
        if not isinstance(g, dict):
            continue
        gid = g.get("id") or ""
        if not isinstance(gid, str):
            continue
        if gid.startswith("imdb://"):
            result["imdb_id"] = gid[len("imdb://"):] or None
        elif gid.startswith("tmdb://"):
            result["tmdb_id"] = gid[len("tmdb://"):] or None
        elif gid.startswith("tvdb://"):
            result["tvdb_id"] = gid[len("tvdb://"):] or None
    return result


def _int_or_none(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def best_media(metadata: dict) -> Optional[dict]:
    """Pick the best `Media[]` element (max height, tie-break max bitrate) and
    its `Part[0]`. Returns `None` if there is no usable `Media`/`Part`.

    Result shape: `{height, width, video_codec, audio_codec, container,
    bitrate, part_key, part_size}`.
    """
    if not isinstance(metadata, dict):
        return None
    media_list = metadata.get("Media")
    if not isinstance(media_list, list):
        return None
    candidates = [m for m in media_list if isinstance(m, dict)]
    if not candidates:
        return None

    def _sort_key(m: dict) -> tuple[int, int]:
        return (_int_or_none(m.get("height")) or 0, _int_or_none(m.get("bitrate")) or 0)

    best = max(candidates, key=_sort_key)
    parts = best.get("Part")
    if not isinstance(parts, list) or not parts or not isinstance(parts[0], dict):
        return None
    part = parts[0]
    return {
        "height": _int_or_none(best.get("height")),
        "width": _int_or_none(best.get("width")),
        "video_codec": best.get("videoCodec"),
        "audio_codec": best.get("audioCodec"),
        "container": best.get("container"),
        "bitrate": _int_or_none(best.get("bitrate")),
        "part_key": part.get("key"),
        "part_size": _int_or_none(part.get("size")),
    }


class PlexApiService:
    """Client for `plex.tv` resource discovery + a Plex Media Server's REST API."""

    def __init__(self) -> None:
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=30.0,
                headers={"User-Agent": "PlexHubBackend/plex-download-source"},
                limits=httpx.Limits(
                    max_connections=20,
                    max_keepalive_connections=10,
                    keepalive_expiry=30,
                ),
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    def _base_headers(self, access_token: str) -> dict[str, str]:
        return {
            "X-Plex-Token": access_token,
            "X-Plex-Client-Identifier": settings.PLEX_CLIENT_IDENTIFIER,
            "Accept": "application/json",
        }

    async def _get_json(
        self, url: str, *, headers: dict[str, str], params: dict | None = None, op: str = "plex_api",
    ) -> Any:
        """GET `url` and return the parsed JSON body, with the same
        exponential-backoff retry as `xtream_service._get`. Raises
        `PlexApiError` (never the raw httpx exception) on exhausted retries
        or a non-retryable failure."""
        client = await self._get_client()
        for attempt, delay in enumerate((*_RETRY_DELAYS, None)):
            try:
                resp = await client.get(url, headers=headers, params=params)
                resp.raise_for_status()
                try:
                    return resp.json()
                except ValueError:
                    logger.error("Plex API %s: response body is not valid JSON", op)
                    raise PlexApiError(f"{op}: invalid JSON response") from None
            except _RETRYABLE as e:
                kind = e.__class__.__name__
                if delay is not None:
                    logger.warning(
                        "Plex API %s attempt %d failed (%s); retrying in %ds",
                        op, attempt + 1, kind, delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                logger.error("Plex API %s failed after retries (%s)", op, kind)
                raise PlexApiError(f"{op}: network error ({kind})") from None
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                if status in _RETRYABLE_STATUSES and delay is not None:
                    retry_after = e.response.headers.get("Retry-After")
                    wait = delay
                    if retry_after:
                        try:
                            wait = min(60, max(delay, int(retry_after)))
                        except (TypeError, ValueError):
                            pass
                    logger.warning("Plex API %s got HTTP %d; retrying in %ds", op, status, wait)
                    await asyncio.sleep(wait)
                    continue
                logger.error("Plex API %s failed with HTTP %d", op, status)
                raise PlexApiError(f"{op}: HTTP {status}") from None
            except httpx.HTTPError as e:
                logger.error("Plex API %s transport error (%s)", op, e.__class__.__name__)
                raise PlexApiError(f"{op}: transport error ({e.__class__.__name__})") from None
        # Unreachable (loop always returns or raises), kept for type-checkers.
        raise PlexApiError(f"{op}: exhausted retries")  # pragma: no cover

    async def discover_servers(self, account_token: str) -> list[PlexResourceDTO]:
        """`GET plex.tv/api/v2/resources` — return every resource that
        `provides` "server" (owned AND shared)."""
        url = f"{_PLEX_TV_BASE}{_RESOURCES_PATH}"
        params = {"includeHttps": 1, "includeRelay": 1, "includeIPv6": 1}
        headers = self._base_headers(account_token)
        data = await self._get_json(url, headers=headers, params=params, op="discover_servers")
        if not isinstance(data, list):
            return []

        resources: list[PlexResourceDTO] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            provides = {p.strip() for p in str(item.get("provides") or "").split(",") if p.strip()}
            if "server" not in provides:
                continue
            client_identifier = item.get("clientIdentifier")
            if not client_identifier:
                continue
            connections = _parse_connections(item.get("connections") or item.get("Connection") or [])
            resources.append(
                PlexResourceDTO(
                    name=item.get("name") or "",
                    client_identifier=str(client_identifier),
                    owned=bool(item.get("owned")),
                    owner_title=item.get("sourceTitle") or None,
                    access_token=item.get("accessToken") or "",
                    connections=connections,
                )
            )
        return resources

    async def probe(self, uri: str, access_token: str) -> bool:
        """`GET {uri}/library/sections` with a short timeout. `True` only on
        HTTP 200 with a JSON `MediaContainer` body. Never raises — any
        failure (timeout/transport/parse) is reported as `False`."""
        if not uri:
            return False
        try:
            client = await self._get_client()
            resp = await client.get(
                f"{uri.rstrip('/')}/library/sections",
                headers=self._base_headers(access_token),
                timeout=settings.PLEX_PROBE_TIMEOUT,
            )
        except Exception:
            return False
        if resp.status_code != 200:
            return False
        try:
            data = resp.json()
        except ValueError:
            return False
        return isinstance(data, dict) and isinstance(data.get("MediaContainer"), dict)

    async def list_sections(self, base_uri: str, access_token: str) -> list[dict]:
        """`GET {base}/library/sections` -> `Directory[]` filtered to
        `type in ('movie', 'show')`."""
        url = f"{base_uri.rstrip('/')}/library/sections"
        data = await self._get_json(url, headers=self._base_headers(access_token), op="list_sections")
        directories = _extract_list(data, "Directory")
        return [d for d in directories if d.get("type") in ("movie", "show")]

    async def _paginated_metadata(
        self, url: str, access_token: str, params: dict, op: str,
    ) -> list[dict]:
        """Shared pagination loop (`X-Plex-Container-Start`/`-Size`,
        page=`_PAGE_SIZE`) for `list_section_items`/`list_children`."""
        items: list[dict] = []
        start = 0
        while True:
            headers = self._base_headers(access_token)
            headers["X-Plex-Container-Start"] = str(start)
            headers["X-Plex-Container-Size"] = str(_PAGE_SIZE)
            data = await self._get_json(url, headers=headers, params=params, op=op)
            container = data.get("MediaContainer") if isinstance(data, dict) else None
            if not isinstance(container, dict):
                break
            page_raw = container.get("Metadata")
            page = [m for m in page_raw if isinstance(m, dict)] if isinstance(page_raw, list) else []
            items.extend(page)

            total_size = container.get("totalSize")
            fetched = len(page)
            if fetched == 0 or not isinstance(total_size, int):
                break
            start += fetched
            if start >= total_size:
                break
        return items

    async def list_section_items(
        self, base_uri: str, access_token: str, section_key: str,
    ) -> list[dict]:
        """`GET {base}/library/sections/{key}/all?includeGuids=1&includeMeta=1`,
        fully paginated. Returns the raw `Metadata[]` entries."""
        url = f"{base_uri.rstrip('/')}/library/sections/{section_key}/all"
        params = {"includeGuids": 1, "includeMeta": 1}
        return await self._paginated_metadata(url, access_token, params, op="list_section_items")

    async def list_children(
        self, base_uri: str, access_token: str, rating_key: str,
    ) -> list[dict]:
        """`GET {base}/library/metadata/{rating_key}/children?includeGuids=1&includeMeta=1`,
        fully paginated. Used for a show's seasons, then a season's episodes."""
        url = f"{base_uri.rstrip('/')}/library/metadata/{rating_key}/children"
        params = {"includeGuids": 1, "includeMeta": 1}
        return await self._paginated_metadata(url, access_token, params, op="list_children")


# Singleton (mirrors `xtream_service`).
plex_api_service = PlexApiService()
