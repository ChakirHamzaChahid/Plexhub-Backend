import logging
from typing import Any, Optional

import httpx

logger = logging.getLogger("plexhub.xtream")


class XtreamService:
    """Client for Xtream Codes player_api.php endpoints."""

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=30.0)
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
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        return resp.json()

    async def authenticate(self, base_url: str, port: int, username: str, password: str) -> dict[str, Any]:
        """Authenticate and get account info."""
        data = await self._get(base_url, port, username, password)
        return data

    async def get_vod_categories(self, base_url: str, port: int, username: str, password: str) -> list[dict]:
        data = await self._get(
            base_url, port, username, password,
            action="get_vod_categories",
        )
        return data if isinstance(data, list) else []

    async def get_vod_streams(
        self, base_url: str, port: int, username: str, password: str,
        category_id: str | None = None,
    ) -> list[dict]:
        kwargs = {}
        if category_id:
            kwargs["category_id"] = category_id
        data = await self._get(
            base_url, port, username, password,
            action="get_vod_streams",
            **kwargs,
        )
        return data if isinstance(data, list) else []

    async def get_vod_info(self, base_url: str, port: int, username: str, password: str, vod_id: int) -> dict:
        data = await self._get(
            base_url, port, username, password,
            action="get_vod_info",
            vod_id=str(vod_id),
        )
        return data if isinstance(data, dict) else {}

    async def get_series_categories(self, base_url: str, port: int, username: str, password: str) -> list[dict]:
        data = await self._get(
            base_url, port, username, password,
            action="get_series_categories",
        )
        return data if isinstance(data, list) else []

    async def get_series(
        self, base_url: str, port: int, username: str, password: str,
        category_id: str | None = None,
    ) -> list[dict]:
        kwargs = {}
        if category_id:
            kwargs["category_id"] = category_id
        data = await self._get(
            base_url, port, username, password,
            action="get_series",
            **kwargs,
        )
        return data if isinstance(data, list) else []

    async def get_series_info(self, base_url: str, port: int, username: str, password: str, series_id: int) -> dict:
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


# Singleton
xtream_service = XtreamService()
