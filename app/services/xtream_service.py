import asyncio
import logging
import os
from typing import Any, Optional

import httpx

from app.config import settings

logger = logging.getLogger("plexhub.xtream")

_RETRY_DELAYS = (1, 2, 4)  # Exponential backoff: 1s, 2s, 4s
_RETRYABLE = (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError)


class XmltvTooLargeError(Exception):
    """The XMLTV body (declared or actually streamed) exceeds the byte cap."""


class XtreamService:
    """Client for Xtream Codes player_api.php endpoints."""

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=30.0,
                headers={"User-Agent": settings.XTREAM_USER_AGENT},
                limits=httpx.Limits(
                    max_connections=50,
                    max_keepalive_connections=30,
                    keepalive_expiry=30,
                ),
            )
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    def _build_base_url(self, base_url: str, port: int) -> str:
        url = base_url.rstrip("/")
        is_default = (
            (url.startswith("http://") and port == 80)
            or (url.startswith("https://") and port == 443)
        )
        return f"{url}/" if is_default else f"{url}:{port}/"

    def _api_url(self, base_url: str, port: int) -> str:
        return f"{self._build_base_url(base_url, port)}player_api.php"

    async def _get(
        self, base_url: str, port: int, username: str, password: str,
        action: str | None = None, **extra_params,
    ) -> dict[str, Any]:
        client = await self._get_client()
        params = {"username": username, "password": password}
        if action:
            params["action"] = action
        params.update(extra_params)

        url = self._api_url(base_url, port)
        last_exc: Exception | None = None
        for attempt, delay in enumerate((*_RETRY_DELAYS, None)):
            try:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                return resp.json()
            except _RETRYABLE as e:
                last_exc = e
                if delay is not None:
                    logger.warning(f"Xtream API {action} attempt {attempt+1} failed ({e}), retrying in {delay}s")
                    await asyncio.sleep(delay)
            except httpx.HTTPStatusError as e:
                if e.response.status_code in (429, 502, 503, 504) and delay is not None:
                    last_exc = e
                    # Honor Retry-After (seconds form) when present, capped at 60s.
                    retry_after = e.response.headers.get("Retry-After")
                    wait = delay
                    if retry_after:
                        try:
                            wait = min(60, max(delay, int(retry_after)))
                        except (TypeError, ValueError):
                            pass
                    logger.warning(
                        f"Xtream API {action} got {e.response.status_code}, retrying in {wait}s"
                    )
                    await asyncio.sleep(wait)
                else:
                    raise
        raise last_exc  # type: ignore[misc]

    async def authenticate(self, account_or_url, port: int = None, username: str = None, password: str = None) -> dict[str, Any]:
        """Authenticate and get account info. Accepts account object or individual params."""
        if hasattr(account_or_url, 'base_url'):
            # Account object
            base_url, port, username, password = account_or_url.base_url, account_or_url.port, account_or_url.username, account_or_url.password
        else:
            base_url = account_or_url
        data = await self._get(base_url, port, username, password)
        return data

    async def get_vod_categories(self, account_or_url, port: int = None, username: str = None, password: str = None) -> list[dict]:
        if hasattr(account_or_url, 'base_url'):
            base_url, port, username, password = account_or_url.base_url, account_or_url.port, account_or_url.username, account_or_url.password
        else:
            base_url = account_or_url
        data = await self._get(
            base_url, port, username, password,
            action="get_vod_categories",
        )
        return data if isinstance(data, list) else []

    async def get_vod_streams(
        self, account_or_url, port: int = None, username: str = None, password: str = None,
        category_id: str | None = None,
    ) -> list[dict]:
        if hasattr(account_or_url, 'base_url'):
            base_url, port, username, password = account_or_url.base_url, account_or_url.port, account_or_url.username, account_or_url.password
        else:
            base_url = account_or_url
        kwargs = {}
        if category_id:
            kwargs["category_id"] = category_id
        data = await self._get(
            base_url, port, username, password,
            action="get_vod_streams",
            **kwargs,
        )
        return data if isinstance(data, list) else []

    async def get_vod_info(self, account_or_url, port: int = None, username: str = None, password: str = None, vod_id: int = None) -> dict:
        if hasattr(account_or_url, 'base_url'):
            base_url, port, username, password = account_or_url.base_url, account_or_url.port, account_or_url.username, account_or_url.password
        else:
            base_url = account_or_url
        data = await self._get(
            base_url, port, username, password,
            action="get_vod_info",
            vod_id=str(vod_id),
        )
        return data if isinstance(data, dict) else {}

    async def get_series_categories(self, account_or_url, port: int = None, username: str = None, password: str = None) -> list[dict]:
        if hasattr(account_or_url, 'base_url'):
            base_url, port, username, password = account_or_url.base_url, account_or_url.port, account_or_url.username, account_or_url.password
        else:
            base_url = account_or_url
        data = await self._get(
            base_url, port, username, password,
            action="get_series_categories",
        )
        return data if isinstance(data, list) else []

    async def get_series(
        self, account_or_url, port: int = None, username: str = None, password: str = None,
        category_id: str | None = None,
    ) -> list[dict]:
        if hasattr(account_or_url, 'base_url'):
            base_url, port, username, password = account_or_url.base_url, account_or_url.port, account_or_url.username, account_or_url.password
        else:
            base_url = account_or_url
        kwargs = {}
        if category_id:
            kwargs["category_id"] = category_id
        data = await self._get(
            base_url, port, username, password,
            action="get_series",
            **kwargs,
        )
        return data if isinstance(data, list) else []

    async def get_series_info(self, account_or_url, port: int = None, username: str = None, password: str = None, series_id: int = None) -> dict:
        if hasattr(account_or_url, 'base_url'):
            base_url, port, username, password = account_or_url.base_url, account_or_url.port, account_or_url.username, account_or_url.password
        else:
            base_url = account_or_url
        data = await self._get(
            base_url, port, username, password,
            action="get_series_info",
            series_id=str(series_id),
        )
        return data if isinstance(data, dict) else {}

    def build_movie_url(
        self, base_url: str, port: int, username: str, password: str,
        stream_id: int, extension: str,
    ) -> str:
        base = self._build_base_url(base_url, port)
        return f"{base}movie/{username}/{password}/{stream_id}.{extension}"

    def build_episode_url(
        self, base_url: str, port: int, username: str, password: str,
        episode_id: str, extension: str,
    ) -> str:
        base = self._build_base_url(base_url, port)
        return f"{base}series/{username}/{password}/{episode_id}.{extension}"

    # --- Live TV Methods ---

    async def get_live_categories(self, account_or_url, port: int = None, username: str = None, password: str = None) -> list[dict]:
        if hasattr(account_or_url, 'base_url'):
            base_url, port, username, password = account_or_url.base_url, account_or_url.port, account_or_url.username, account_or_url.password
        else:
            base_url = account_or_url
        data = await self._get(
            base_url, port, username, password,
            action="get_live_categories",
        )
        return data if isinstance(data, list) else []

    async def get_live_streams(
        self, account_or_url, port: int = None, username: str = None, password: str = None,
        category_id: str | None = None,
    ) -> list[dict]:
        if hasattr(account_or_url, 'base_url'):
            base_url, port, username, password = account_or_url.base_url, account_or_url.port, account_or_url.username, account_or_url.password
        else:
            base_url = account_or_url
        kwargs = {}
        if category_id:
            kwargs["category_id"] = category_id
        data = await self._get(
            base_url, port, username, password,
            action="get_live_streams",
            **kwargs,
        )
        return data if isinstance(data, list) else []

    async def get_short_epg(
        self, account_or_url, port: int = None, username: str = None, password: str = None,
        stream_id: int = None, limit: int | None = None,
    ) -> dict:
        if hasattr(account_or_url, 'base_url'):
            base_url, port, username, password = account_or_url.base_url, account_or_url.port, account_or_url.username, account_or_url.password
        else:
            base_url = account_or_url
        kwargs = {"stream_id": str(stream_id)}
        if limit is not None:
            kwargs["limit"] = str(limit)
        data = await self._get(
            base_url, port, username, password,
            action="get_short_epg",
            **kwargs,
        )
        return data if isinstance(data, dict) else {}

    async def download_xmltv(
        self, account_or_url, dest: str | os.PathLike, *, max_bytes: int,
        total_timeout: float = 600.0,
        port: int = None, username: str = None, password: str = None,
    ) -> int:
        """Stream the full XMLTV (`xmltv.php`) to the file ``dest`` and return
        the number of bytes written.

        Replaces the former ``get_xmltv`` (FR-BE-560, Lot 10 sub-lot D), which
        returned the WHOLE document as one ``str``: a large panel's XMLTV
        weighs tens to hundreds of MB, and the container is capped at 2 GB of
        RAM shared with the ONNX models. Here the body goes to disk in 256 KiB
        chunks (never fully in memory) and is parsed from the file afterwards.

        * ``max_bytes`` caps the DECOMPRESSED size (httpx transparently
          inflates ``Content-Encoding: gzip``, so a gzip bomb is cut by the
          same counter). A declared ``Content-Length`` above the cap fails
          before any byte is read. Exceeding it raises ``XmltvTooLargeError``
          (never retried) and leaves a partial file the caller must delete.
        * Same retry/backoff policy as ``_get`` (1/2/4 s on timeouts, connect
          and protocol errors, and on 429/502/503/504 honouring
          ``Retry-After``); each attempt restarts the file from scratch and
          gets its own ``total_timeout`` wall-clock ceiling.
        * The URL carries the account credentials as query parameters: it is
          NEVER logged, and httpx exception text (which embeds the URL for
          ``HTTPStatusError``) is never interpolated into a message — only the
          exception type and the HTTP status code.
        """
        if hasattr(account_or_url, 'base_url'):
            base_url, port, username, password = account_or_url.base_url, account_or_url.port, account_or_url.username, account_or_url.password
        else:
            base_url = account_or_url
        client = await self._get_client()
        url = f"{self._build_base_url(base_url, port)}xmltv.php"
        params = {"username": username, "password": password}

        last_exc: Exception | None = None
        for attempt, delay in enumerate((*_RETRY_DELAYS, None)):
            try:
                async with asyncio.timeout(total_timeout):
                    return await self._stream_to_file(client, url, params, dest, max_bytes)
            except XmltvTooLargeError:
                raise
            except _RETRYABLE as e:
                last_exc = e
                if delay is not None:
                    logger.warning(
                        "Xtream xmltv attempt %d failed (%s), retrying in %ss",
                        attempt + 1, type(e).__name__, delay,
                    )
                    await asyncio.sleep(delay)
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                if status in (429, 502, 503, 504) and delay is not None:
                    last_exc = e
                    wait = delay
                    retry_after = e.response.headers.get("Retry-After")
                    if retry_after:
                        try:
                            wait = min(60, max(delay, int(retry_after)))
                        except (TypeError, ValueError):
                            pass
                    logger.warning(
                        "Xtream xmltv got HTTP %d, retrying in %ss", status, wait,
                    )
                    await asyncio.sleep(wait)
                else:
                    raise
        raise last_exc  # type: ignore[misc]

    @staticmethod
    async def _stream_to_file(
        client: httpx.AsyncClient, url: str, params: dict, dest, max_bytes: int,
    ) -> int:
        """One download attempt: GET ``url`` and write the body to ``dest``."""
        async with client.stream(
            "GET", url, params=params,
            timeout=httpx.Timeout(30.0, read=120.0),
        ) as resp:
            resp.raise_for_status()
            declared = resp.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise XmltvTooLargeError(
                    f"declared Content-Length {declared} exceeds max_bytes={max_bytes}"
                )
            written = 0
            with open(dest, "wb") as fh:
                async for chunk in resp.aiter_bytes(chunk_size=256 * 1024):
                    written += len(chunk)
                    if written > max_bytes:
                        raise XmltvTooLargeError(
                            f"XMLTV body exceeds max_bytes={max_bytes}"
                        )
                    # Disk write off the event loop (piège §9-11).
                    await asyncio.to_thread(fh.write, chunk)
            return written

    def build_live_url(
        self, base_url: str, port: int, username: str, password: str,
        stream_id: int, extension: str = "ts",
    ) -> str:
        base = self._build_base_url(base_url, port)
        return f"{base}live/{username}/{password}/{stream_id}.{extension}"


# Singleton
xtream_service = XtreamService()
