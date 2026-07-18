"""OMDb HTTP client — imdb-id consistency validator support.

OMDb (https://www.omdbapi.com) is consulted ONLY by `imdb_id` here — this
service never searches by title. It exists to cross-check that a
`media.tmdb_id`/`media.imdb_id` pair genuinely refers to the same title (see
`docs/plans/2026-07-17-omdb-id-consistency-validator-design.md`); the actual
detector/corrector script (Wave 3) is out of scope for this module.

Architectural mirror of `app.services.tmdb_service` (client pooling,
retry/backoff shape, real-call-count budgeting) — see
`TMDBService._request` (tmdb_service.py:174-220) for the semantics this
mirrors. One deliberate deviation from that mirror: the OMDb key rides on
every request as an `apikey` query param, and `httpx.HTTPStatusError.__str__`
embeds the full request URL (including query string) — so, unlike
`tmdb_service.find_by_imdb_id` (which logs `exc` verbatim), this module NEVER
logs the raw exception text; only the exception type / HTTP status code is
logged (see `get_by_imdb_id`), so the API key can never leak into logs.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

import httpx

from app.config import settings

logger = logging.getLogger("plexhub.omdb")

_RETRY_DELAYS = (1, 2, 4)
_RETRYABLE = (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError)


@dataclass
class OMDbData:
    """Parsed OMDb `?i=<imdb_id>` response (found case only)."""
    title: str
    year: str
    runtime_minutes: int | None
    genre: str | None
    director: str | None
    actors: str | None
    plot: str | None
    imdb_rating: float | None
    imdb_votes: int | None
    type: str  # "movie" | "series" (OMDb also returns "episode", passed through as-is)


def _clean_str(value) -> str | None:
    """OMDb's "N/A" sentinel -> None; blank/missing -> None; else stripped str."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or value == "N/A":
        return None
    return value


def _parse_runtime_minutes(value) -> int | None:
    """"123 min" -> 123. "N/A" / unparseable -> None."""
    cleaned = _clean_str(value)
    if cleaned is None:
        return None
    digits = cleaned.split(" ", 1)[0]
    return int(digits) if digits.isdigit() else None


def _parse_imdb_rating(value) -> float | None:
    """"8.3" -> 8.3. "N/A" / unparseable -> None."""
    cleaned = _clean_str(value)
    if cleaned is None:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_imdb_votes(value) -> int | None:
    """"1,234,567" -> 1234567. "N/A" / unparseable -> None."""
    cleaned = _clean_str(value)
    if cleaned is None:
        return None
    digits = cleaned.replace(",", "")
    return int(digits) if digits.isdigit() else None


class OMDbService:
    BASE_URL = "https://www.omdbapi.com"

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None
        # Real outbound HTTP attempts made by `_request` (every retry counts,
        # not just the logical `get_by_imdb_id` call) — mirrors
        # `tmdb_service.TMDBService.real_request_count` (CR-F03 semantics)
        # so callers can budget against `OMDB_DAILY_LIMIT` the same way.
        self.real_request_count: int = 0

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=10.0,
                params={"apikey": settings.OMDB_API_KEY},
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
        # Read fresh each call (not cached at construction) so callers/tests
        # can flip `settings.OMDB_API_KEY` at runtime — mirrors
        # `TMDBService.is_configured`.
        return bool(settings.OMDB_API_KEY)

    def get_request_count(self) -> int:
        """Real OMDb HTTP attempts made so far (every retry inside
        `_request` counts). Mirrors `tmdb_service.get_request_count` — used
        the same way to budget against `OMDB_DAILY_LIMIT`."""
        return self.real_request_count

    def reset_request_count(self) -> None:
        """Reset the real-call counter. Same in-process-only, per-run-budget
        caveat as `tmdb_service.reset_request_count` (not a persisted 24h
        quota — see that method's docstring)."""
        self.real_request_count = 0

    async def _request(self, path: str, params: dict | None = None) -> dict:
        """GET with retry + exponential backoff + 429 rate-limit handling.

        Mirrors `TMDBService._request` line for line (same retry shape, same
        real-call counting semantics — no metrics counters here, OMDb has
        none wired). Logging never includes the raw exception text (see
        module docstring) so `apikey` cannot leak via a log line."""
        client = await self._get_client()
        url = f"{self.BASE_URL}{path}"
        last_exc: Exception | None = None
        for attempt, delay in enumerate((*_RETRY_DELAYS, None)):
            # Every loop iteration is a real outbound HTTP attempt (initial +
            # up to 3 retries) — count it here, not once per logical
            # `get_by_imdb_id()` call, mirroring tmdb_service's CR-F03 fix.
            self.real_request_count += 1
            try:
                resp = await client.get(url, params=params)
                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", delay or 4))
                    if delay is not None:
                        logger.warning("OMDb 429 rate limited, waiting %ss", retry_after)
                        await asyncio.sleep(retry_after)
                        continue
                    resp.raise_for_status()  # last attempt: raise
                resp.raise_for_status()
                return resp.json()
            except _RETRYABLE as e:
                last_exc = e
                if delay is not None:
                    logger.warning(
                        "OMDb %s attempt %d failed (%s), retrying in %ss",
                        path, attempt + 1, type(e).__name__, delay,
                    )
                    await asyncio.sleep(delay)
            except httpx.HTTPStatusError as e:
                if e.response.status_code in (502, 503, 504) and delay is not None:
                    last_exc = e
                    logger.warning(
                        "OMDb %s got %s, retrying in %ss", path, e.response.status_code, delay,
                    )
                    await asyncio.sleep(delay)
                else:
                    raise
        raise last_exc  # type: ignore[misc]

    async def get_by_imdb_id(self, imdb_id: str) -> OMDbData | None:
        """Look up a title by its imdb_id. Validation-only flow — this is
        never a title search.

        Returns None when: OMDb is unconfigured, `imdb_id` is blank, OMDb
        reports "not found" (`Response: "False"`), or any transport/HTTP
        failure occurs. Failures are logged with exception type / HTTP
        status only — never the raw exception text (see module docstring),
        so the semantics mirror `tmdb_service.find_by_imdb_id` (graceful
        None on failure) rather than `search_movie` (which propagates)."""
        if not imdb_id:
            return None
        if not self.is_configured:
            return None
        try:
            data = await self._request("/", params={"i": imdb_id, "plot": "full"})
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "OMDb get_by_imdb_id failed for %s (HTTP %s)", imdb_id, exc.response.status_code,
            )
            return None
        except Exception as exc:
            logger.warning(
                "OMDb get_by_imdb_id failed for %s (%s)", imdb_id, type(exc).__name__,
            )
            return None

        if data.get("Response") != "True":
            return None

        return OMDbData(
            title=data.get("Title") or "",
            year=data.get("Year") or "",
            runtime_minutes=_parse_runtime_minutes(data.get("Runtime")),
            genre=_clean_str(data.get("Genre")),
            director=_clean_str(data.get("Director")),
            actors=_clean_str(data.get("Actors")),
            plot=_clean_str(data.get("Plot")),
            imdb_rating=_parse_imdb_rating(data.get("imdbRating")),
            imdb_votes=_parse_imdb_votes(data.get("imdbVotes")),
            type=data.get("Type") or "",
        )


# Singleton
omdb_service = OMDbService()
