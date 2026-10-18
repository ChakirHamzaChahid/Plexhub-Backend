"""Scrape gateway `/api/scrape/v1` (lot B1, ticket B1-11).

JSON routes of TMDB/OMDb scraping for an external client (plexhubTMM), with
a versioned cache. Contract `15-contrat-b1-backend.md` section 5; PRD
FR-015..FR-036; ADR 0008 D2.3/D2.4 and ADR 0009 D2.8/D2.12.

Mounting (Pattern C): self-prefixed router, mounted bare in `app/main.py`,
and — unlike every other Pattern C router — WITHOUT any router-level
dependency. Each route declares its own guard (ADR 0008 D2.4):

    verify_scrape_scope        the 7 read routes (incl. GET /cache/stats)
    verify_scrape_admin_scope  ALONE on POST /cache/invalidate and /cache/purge
                               (a key carrying only `scrape.admin` must pass)

then `enforce_scrape_rate_limit`, declared AFTER the guard so that
`request.state.principal` is already set. `route_audit` does not tell the two
scope guards apart: `tests/test_scrape_v1_routes.py` checks the expected
guard route by route.

This module is validation + delegation only (CLAUDE.md section 2): every
decision lives in `app/services/scrape_gateway_service.py`.

Validation (PRD FR-015b): types and declared bounds are FastAPI's native 422;
the semantic checks (language tag, empty normalised title, /find ids, the
invalidate body) are 400 `invalid_parameter`, raised by the service.
"""
from __future__ import annotations

import logging
import math
import time
from collections.abc import Awaitable
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status

from app.api.deps import Principal, verify_scrape_admin_scope, verify_scrape_scope
from app.models import scrape_schemas as S
from app.services import scrape_gateway_service as gateway
from app.services import scrape_keys
from app.services.scrape_gateway_service import GatewayError
from app.utils import rate_limit
from app.utils.metrics import scrape_gateway_requests_total

logger = logging.getLogger("plexhub.api.scrape_v1")

PREFIX = "/api/scrape/v1"
RATE_LIMIT_WINDOW_SECONDS = 60.0

router = APIRouter(prefix=PREFIX, tags=["scrape-gateway"])


# --- Errors -------------------------------------------------------------------


def _error(status_code: int, code: str, message: str, retry_after: int | None = None) -> HTTPException:
    detail: dict[str, Any] = {"code": code, "message": message}
    headers = None
    if retry_after is not None:
        retry_after = max(1, min(gateway.RETRY_AFTER_CAP_SECONDS, int(retry_after)))
        detail["retryAfterSeconds"] = retry_after
        headers = {"Retry-After": str(retry_after)}
    return HTTPException(status_code=status_code, detail=detail, headers=headers)


def _http_error(err: GatewayError) -> HTTPException:
    return _error(err.status, err.code, err.message, err.retry_after)


_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    code: {"model": S.ErrorResponse}
    for code in (400, 401, 403, 429, 502, 503, 504)
}


# --- Per-key rate limit (ADR 0009 D2.8, FR-033) --------------------------------


async def enforce_scrape_rate_limit(request: Request) -> None:
    """Sliding window of 60 s per API key (`key_id`, "master" for the master
    secret), `SCRAPE_RATE_LIMIT_PER_MINUTE` (`<= 0` disables it). Declared
    AFTER the route's guard: it reads the `Principal` the guard put on
    `request.state` and never assumes one (absent => 401, refused)."""
    principal = getattr(request.state, "principal", None)
    if not isinstance(principal, Principal):
        raise _error(
            status.HTTP_401_UNAUTHORIZED, "invalid_api_key",
            "Missing, unknown, revoked or expired API key",
        )
    limit = scrape_keys.rate_limit_per_minute()
    if limit <= 0:
        return
    bucket = "master" if principal.is_master else principal.key_id
    if not bucket:
        raise _error(
            status.HTTP_401_UNAUTHORIZED, "invalid_api_key",
            "Missing, unknown, revoked or expired API key",
        )
    try:
        rate_limit.scrape_gateway_rate_limiter.hit(
            bucket, max_events=limit, window_seconds=RATE_LIMIT_WINDOW_SECONDS,
        )
    except rate_limit.RateLimitExceeded as exc:
        raise _error(
            status.HTTP_429_TOO_MANY_REQUESTS, "rate_limited",
            "Too many requests for this API key",
            retry_after=math.ceil(exc.retry_after_seconds),
        ) from None


_READ_GUARD = [Depends(verify_scrape_scope), Depends(enforce_scrape_rate_limit)]
_ADMIN_GUARD = [Depends(verify_scrape_admin_scope), Depends(enforce_scrape_rate_limit)]


async def _run(route: str, call: Awaitable[Any]) -> Any:
    """Await the service, count and log the outcome, map `GatewayError` to
    its body. The log line carries the route name, the outcome and the
    duration only — never a title, a key or a provider URL (ADR D2.12)."""
    started = time.monotonic()
    try:
        result = await call
    except GatewayError as err:
        scrape_gateway_requests_total.labels(route=route, outcome="error").inc()
        logger.info("scrape gateway: route=%s outcome=error status=%s code=%s %.0fms",
                    route, err.status, err.code, (time.monotonic() - started) * 1000)
        raise _http_error(err) from None
    except Exception as exc:
        scrape_gateway_requests_total.labels(route=route, outcome="error").inc()
        logger.warning("scrape gateway: route=%s outcome=error (%s)", route, type(exc).__name__)
        raise
    if isinstance(result, tuple):
        body, outcome = result
    else:
        body, outcome = result, "hit"  # /cache/*: no provider call
    scrape_gateway_requests_total.labels(route=route, outcome=outcome).inc()
    logger.info("scrape gateway: route=%s outcome=%s %.0fms",
                route, outcome, (time.monotonic() - started) * 1000)
    return body


_LANGUAGE = Query(
    default=None,
    description="Language tag ^[a-z]{2}-[A-Z]{2}$ (default TMDB_LANGUAGE); other value: 400",
)
_REFRESH = Query(default=False, description="Ignore the cached entry (explicit user action)")
_YEAR = Query(default=None, ge=1850, le=2200)
_MEDIA_TYPE = Query(alias="type")
_TMDB_ID = Path(alias="tmdbId", ge=1, le=999_999_999)


# --- Data routes (contract 5.2 - 5.7) ------------------------------------------


@router.get(
    "/match", response_model=S.MatchResponse, response_model_by_alias=True,
    dependencies=_READ_GUARD, responses=_ERROR_RESPONSES,
)
async def match(
    media_type: Literal["movie", "show"] = _MEDIA_TYPE,
    title: str = Query(min_length=1, max_length=300),
    year: int | None = _YEAR,
    language: str | None = _LANGUAGE,
    refresh: bool = _REFRESH,
):
    """Match (type, cleaned title, year) to a TMDB record (contract 5.2)."""
    return await _run("match", gateway.match(media_type, title, year, language, refresh))


@router.get(
    "/search", response_model=S.ScrapeSearchResponse, response_model_by_alias=True,
    dependencies=_READ_GUARD, responses=_ERROR_RESPONSES,
)
async def search(
    media_type: Literal["movie", "show"] = _MEDIA_TYPE,
    query: str = Query(min_length=1, max_length=300),
    year: int | None = _YEAR,
    language: str | None = _LANGUAGE,
):
    """Candidates for a free query (contract 5.3); nothing is persisted."""
    return await _run("search", gateway.search(media_type, query, year, language))


@router.get(
    "/movie/{tmdbId}", response_model=S.MovieResponse, response_model_by_alias=True,
    dependencies=_READ_GUARD, responses=_ERROR_RESPONSES,
)
async def movie(
    tmdb_id: int = _TMDB_ID,
    language: str | None = _LANGUAGE,
    refresh: bool = _REFRESH,
):
    """Movie details + OMDb ratings (contract 5.4)."""
    return await _run("movie", gateway.details("movie", tmdb_id, language, refresh))


@router.get(
    "/tv/{tmdbId}", response_model=S.ShowResponse, response_model_by_alias=True,
    dependencies=_READ_GUARD, responses=_ERROR_RESPONSES,
)
async def tv(
    tmdb_id: int = _TMDB_ID,
    language: str | None = _LANGUAGE,
    refresh: bool = _REFRESH,
):
    """Show details + OMDb ratings (contract 5.5)."""
    return await _run("tv", gateway.details("show", tmdb_id, language, refresh))


@router.get(
    "/tv/{tmdbId}/season/{seasonNumber}", response_model=S.SeasonResponse,
    response_model_by_alias=True, dependencies=_READ_GUARD, responses=_ERROR_RESPONSES,
)
async def season(
    tmdb_id: int = _TMDB_ID,
    season_number: int = Path(alias="seasonNumber", ge=0, le=999),
    language: str | None = _LANGUAGE,
    refresh: bool = _REFRESH,
):
    """One season and its episodes, one TMDB call (contract 5.6)."""
    return await _run("season", gateway.season(tmdb_id, season_number, language, refresh))


@router.get(
    "/find", response_model=S.FindResponse, response_model_by_alias=True,
    dependencies=_READ_GUARD, responses=_ERROR_RESPONSES,
)
async def find(
    imdb_id: str | None = Query(default=None, alias="imdbId", description="^tt[0-9]{7,10}$"),
    tvdb_id: str | None = Query(default=None, alias="tvdbId", description="1 to 12 digits"),
):
    """External id -> TMDB records (contract 5.7). Exactly one id, else 400.
    No `refresh` here: contract 5.7 does not define one for this route."""
    return await _run("find", gateway.find(imdb_id, tvdb_id))


# --- Cache administration (contract 5.8 - 5.10) --------------------------------


@router.get(
    "/cache/stats", response_model=S.CacheStatsResponse, response_model_by_alias=True,
    dependencies=_READ_GUARD, responses=_ERROR_RESPONSES,
)
async def cache_stats():
    """Per (family, keyVersion) counts, TTLs and in-memory counters (5.8)."""
    return await _run("cache_stats", gateway.stats())


@router.post(
    "/cache/invalidate", response_model=S.InvalidateResponse, response_model_by_alias=True,
    dependencies=_ADMIN_GUARD, responses=_ERROR_RESPONSES,
)
async def cache_invalidate(body: S.InvalidateBody):
    """Delete entries by canonical keys, or by family + tmdbId (5.9)."""
    return await _run("cache_invalidate", gateway.invalidate(body.root))


@router.post(
    "/cache/purge", response_model=S.PurgeResponse, response_model_by_alias=True,
    dependencies=_ADMIN_GUARD, responses=_ERROR_RESPONSES,
)
async def cache_purge(body: S.PurgeRequest | None = None):
    """Delete up to `limit` expired / foreign-version entries (5.10)."""
    request = body or S.PurgeRequest()
    return await _run("cache_purge", gateway.purge(request.limit))
