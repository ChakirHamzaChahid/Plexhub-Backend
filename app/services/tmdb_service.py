import logging
from dataclasses import dataclass
from typing import Optional

import httpx
from rapidfuzz import fuzz

from app.config import settings
from app.utils.string_normalizer import normalize_for_sorting

logger = logging.getLogger("plexhub.tmdb")


@dataclass
class TMDBMatch:
    tmdb_id: int
    title: str
    year: int | None
    confidence: float


class TMDBService:
    BASE_URL = "https://api.themoviedb.org/3"

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=10.0,
                headers={"Authorization": f"Bearer {settings.TMDB_API_KEY}"},
            )
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    @property
    def is_configured(self) -> bool:
        return bool(settings.TMDB_API_KEY)

    async def search_movie(
        self, title: str, year: int | None,
    ) -> TMDBMatch | None:
        if not self.is_configured:
            return None
        client = await self._get_client()
        params: dict = {"query": title, "language": "en-US"}
        if year:
            params["year"] = year
        resp = await client.get(f"{self.BASE_URL}/search/movie", params=params)
        resp.raise_for_status()
        results = resp.json().get("results", [])
        return self._best_match(results, title, year, title_key="title", date_key="release_date")

    async def search_tv(
        self, title: str, year: int | None,
    ) -> TMDBMatch | None:
        if not self.is_configured:
            return None
        client = await self._get_client()
        params: dict = {"query": title, "language": "en-US"}
        if year:
            params["first_air_date_year"] = year
        resp = await client.get(f"{self.BASE_URL}/search/tv", params=params)
        resp.raise_for_status()
        results = resp.json().get("results", [])
        return self._best_match(results, title, year, title_key="name", date_key="first_air_date")

    async def get_movie_external_ids(self, tmdb_id: int) -> dict:
        """Get IMDb ID from TMDB with guaranteed 'tt' prefix."""
        client = await self._get_client()
        resp = await client.get(f"{self.BASE_URL}/movie/{tmdb_id}/external_ids")
        resp.raise_for_status()
        data = resp.json()
        # Ensure IMDB ID has 'tt' prefix
        imdb_id = data.get("imdb_id")
        if imdb_id and not imdb_id.startswith("tt"):
            data["imdb_id"] = f"tt{imdb_id}"
        return data

    async def get_tv_external_ids(self, tmdb_id: int) -> dict:
        """Get IMDb ID from TMDB with guaranteed 'tt' prefix."""
        client = await self._get_client()
        resp = await client.get(f"{self.BASE_URL}/tv/{tmdb_id}/external_ids")
        resp.raise_for_status()
        data = resp.json()
        # Ensure IMDB ID has 'tt' prefix
        imdb_id = data.get("imdb_id")
        if imdb_id and not imdb_id.startswith("tt"):
            data["imdb_id"] = f"tt{imdb_id}"
        return data
        self,
        results: list[dict],
        title: str,
        year: int | None,
        title_key: str,
        date_key: str,
    ) -> TMDBMatch | None:
        """Fuzzy match by title + year, return best with confidence score."""
        if not results:
            return None

        normalized_query = normalize_for_sorting(title).lower()
        best: TMDBMatch | None = None
        best_confidence = 0.0

        for r in results[:10]:  # Check top 10 results
            r_title = r.get(title_key, "")
            r_normalized = normalize_for_sorting(r_title).lower()

            # Title similarity (0-100 from rapidfuzz, normalize to 0-1)
            title_sim = fuzz.ratio(normalized_query, r_normalized) / 100.0

            # Year factor
            r_date = r.get(date_key, "")
            r_year = int(r_date[:4]) if r_date and len(r_date) >= 4 else None
            year_factor = 1.0
            if year and r_year:
                if year == r_year:
                    year_factor = 1.0
                elif abs(year - r_year) <= 1:
                    year_factor = 0.95
                else:
                    year_factor = 0.85
            elif year and not r_year:
                year_factor = 0.9

            confidence = title_sim * year_factor

            if confidence > best_confidence:
                best_confidence = confidence
                best = TMDBMatch(
                    tmdb_id=r["id"],
                    title=r_title,
                    year=r_year,
                    confidence=confidence,
                )

        # Threshold: >= 0.85
        if best and best.confidence >= 0.85:
            return best
        return None


# Singleton
tmdb_service = TMDBService()
