import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

import httpx
from rapidfuzz import fuzz

from app.config import settings
from app.utils.string_normalizer import normalize_for_sorting
from app.utils.ttl_cache import TTLCache

logger = logging.getLogger("plexhub.tmdb")

_RETRY_DELAYS = (1, 2, 4)
_RETRYABLE = (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError)

POSTER_BASE = "https://image.tmdb.org/t/p/w342"
BACKDROP_BASE = "https://image.tmdb.org/t/p/w1280"

# Search results rarely change within a day; bounded to keep memory predictable.
_SEARCH_CACHE_SIZE = 5000
_SEARCH_CACHE_TTL = 24 * 3600  # 24 h

# Sentinel distinct from None (a real cached "no match" result).
_MISSING = object()


@dataclass
class TMDBMatch:
    tmdb_id: int
    title: str
    year: int | None
    confidence: float


@dataclass
class TMDBEnrichmentData:
    """Rich metadata from TMDB movie/{id} or tv/{id} with append_to_response=credits,external_ids."""
    tmdb_id: int
    imdb_id: str | None
    overview: str | None
    poster_url: str | None
    backdrop_url: str | None
    vote_average: float | None
    genres: str | None  # comma-separated
    year: int | None
    cast: str | None  # comma-separated actor names


class TMDBService:
    BASE_URL = "https://api.themoviedb.org/3"

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None
        self._search_cache: TTLCache[tuple[str, str, int | None], TMDBMatch | None] = TTLCache(
            max_size=_SEARCH_CACHE_SIZE, ttl_seconds=_SEARCH_CACHE_TTL,
        )

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=10.0,
                params={"api_key": settings.TMDB_API_KEY},
                limits=httpx.Limits(
                    max_connections=50,
                    max_keepalive_connections=35,
                    keepalive_expiry=30,
                ),
            )
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    @property
    def is_configured(self) -> bool:
        return bool(settings.TMDB_API_KEY)

    @staticmethod
    def _metric_kind(path: str) -> str:
        """Map a TMDB path to a coarse `kind` label for metrics."""
        if path.startswith("/search/movie"):
            return "search_movie"
        if path.startswith("/search/tv"):
            return "search_tv"
        if path.startswith("/movie/") or path.startswith("/tv/"):
            return "details"
        return "other"

    async def _request(self, path: str, params: dict | None = None) -> dict:
        """GET with retry + exponential backoff + 429 rate-limit handling."""
        from app.utils.metrics import tmdb_requests_total

        client = await self._get_client()
        url = f"{self.BASE_URL}{path}"
        kind = self._metric_kind(path)
        rate_limited = False
        last_exc: Exception | None = None
        for attempt, delay in enumerate((*_RETRY_DELAYS, None)):
            try:
                resp = await client.get(url, params=params)
                # Handle 429 rate limit with Retry-After header
                if resp.status_code == 429:
                    rate_limited = True
                    retry_after = int(resp.headers.get("Retry-After", delay or 4))
                    if delay is not None:
                        logger.warning(f"TMDB 429 rate limited, waiting {retry_after}s")
                        await asyncio.sleep(retry_after)
                        continue
                    resp.raise_for_status()  # last attempt: raise
                resp.raise_for_status()
                tmdb_requests_total.labels(
                    kind=kind,
                    result="rate_limited" if rate_limited else "ok",
                ).inc()
                return resp.json()
            except _RETRYABLE as e:
                last_exc = e
                if delay is not None:
                    logger.warning(f"TMDB {path} attempt {attempt+1} failed ({e}), retrying in {delay}s")
                    await asyncio.sleep(delay)
            except httpx.HTTPStatusError as e:
                if e.response.status_code in (502, 503, 504) and delay is not None:
                    last_exc = e
                    logger.warning(f"TMDB {path} got {e.response.status_code}, retrying in {delay}s")
                    await asyncio.sleep(delay)
                else:
                    tmdb_requests_total.labels(kind=kind, result="error").inc()
                    raise
        tmdb_requests_total.labels(kind=kind, result="error").inc()
        raise last_exc  # type: ignore[misc]

    async def search_movie(
        self, title: str, year: int | None,
    ) -> TMDBMatch | None:
        if not self.is_configured:
            return None
        cache_key = ("movie", title, year)
        cached = self._search_cache.get(cache_key, default=_MISSING)
        if cached is not _MISSING:
            return cached
        params: dict = {"query": title, "language": settings.TMDB_LANGUAGE}
        if year:
            params["year"] = year
        data = await self._request("/search/movie", params=params)
        results = data.get("results", [])
        match = self._best_match(results, title, year, title_key="title", date_key="release_date")
        self._search_cache.set(cache_key, match)
        return match

    async def search_tv(
        self, title: str, year: int | None,
    ) -> TMDBMatch | None:
        if not self.is_configured:
            return None
        cache_key = ("tv", title, year)
        cached = self._search_cache.get(cache_key, default=_MISSING)
        if cached is not _MISSING:
            return cached
        params: dict = {"query": title, "language": settings.TMDB_LANGUAGE}
        if year:
            params["first_air_date_year"] = year
        data = await self._request("/search/tv", params=params)
        results = data.get("results", [])
        match = self._best_match(results, title, year, title_key="name", date_key="first_air_date")
        self._search_cache.set(cache_key, match)
        return match

    async def get_movie_details(self, tmdb_id: int) -> TMDBEnrichmentData:
        """Fetch movie details + external_ids in a single API call."""
        data = await self._request(
            f"/movie/{tmdb_id}",
            params={"append_to_response": "credits,external_ids", "language": settings.TMDB_LANGUAGE},
        )
        return self._parse_details(data, tmdb_id, date_key="release_date")

    async def get_tv_details(self, tmdb_id: int) -> TMDBEnrichmentData:
        """Fetch TV details + external_ids in a single API call."""
        data = await self._request(
            f"/tv/{tmdb_id}",
            params={"append_to_response": "credits,external_ids", "language": settings.TMDB_LANGUAGE},
        )
        return self._parse_details(data, tmdb_id, date_key="first_air_date")

    def _parse_details(self, data: dict, tmdb_id: int, date_key: str) -> TMDBEnrichmentData:
        """Parse TMDB detail response into TMDBEnrichmentData."""
        # IMDB ID with guaranteed 'tt' prefix
        imdb_id = data.get("external_ids", {}).get("imdb_id")
        if imdb_id and not imdb_id.startswith("tt"):
            imdb_id = f"tt{imdb_id}"

        poster = data.get("poster_path")
        backdrop = data.get("backdrop_path")
        genres_list = data.get("genres", [])
        genres = ", ".join(g["name"] for g in genres_list if g.get("name")) or None

        release_date = data.get(date_key, "")
        year = int(release_date[:4]) if release_date and len(release_date) >= 4 else None

        # Extract cast (top 20 actors)
        credits = data.get("credits", {})
        cast_list = credits.get("cast", [])
        cast = ", ".join(
            a["name"] for a in cast_list[:20] if a.get("name")
        ) or None

        return TMDBEnrichmentData(
            tmdb_id=tmdb_id,
            imdb_id=imdb_id,
            overview=data.get("overview") or None,
            poster_url=f"{POSTER_BASE}{poster}" if poster else None,
            backdrop_url=f"{BACKDROP_BASE}{backdrop}" if backdrop else None,
            vote_average=data.get("vote_average"),
            genres=genres,
            year=year,
            cast=cast,
        )

    def _best_match(
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
