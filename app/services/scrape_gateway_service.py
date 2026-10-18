"""Orchestration of the scrape gateway `/api/scrape/v1` (lot B1, ticket B1-11).

ADR 0009 D2.4-D2.12, PRD FR-016..FR-024 and FR-032..FR-035, contract
`15-contrat-b1-backend.md` sections 4-6.

This module glues the pieces built by B1-07..B1-10 together:

    scrape_keys          canonical keys, bounded settings
    tmdb_service /       strict provider access (raise `ProviderError`)
    omdb_service
    scrape_mappers       raw JSON -> contract dicts (cache form)
    scrape_gateway_cache versioned persistent cache (`scrape_cache`)

and adds what none of them owns: the per-request deadline, the single-flight,
the OMDb composition, `refresh`, the one translation table from provider
errors to gateway errors, and the `sinceStart` counters.

Single-flight (D2.7) — do not undo
----------------------------------
* `_in_flight` maps `(key text, refresh)` to ONE `asyncio.Task`. The lookup
  and the insertion happen with NO `await` in between (`_join_or_start`),
  otherwise two requests could start two flights.
* The task is created by `create_background_task` (strong reference): a
  client that disconnects cancels its own wait, never the shared call, and the
  result still lands in the cache. Waiters use `asyncio.shield(task)` under
  `asyncio.timeout(remaining)`; past the deadline they get 504
  `upstream_timeout` while the task keeps running.
* The task removes itself in `add_done_callback`, comparing the task IDENTITY
  (a newer flight of the same key is never removed by an older one).
* `create_background_task` logs a failed task with `exc_info`, and an httpx
  `HTTPStatusError` embeds the request URL (`api_key=` / `apikey=`). The flight
  body (`_guarded`) therefore catches EVERY exception and re-raises a fresh,
  context-free `GatewayError` (type + HTTP status only, never `str(exc)`, no
  traceback text, no locals) — CLAUDE.md pitfalls 21c / 25c.
* `/search` is not coalesced (nothing persisted, cheap).

Counters (`scrape_gateway_cache.counters`): one `hit`, `miss` or `coalesced`
per data request (a `/search` is a `miss`); `upstream_errors` once per request
that ends on an error coming from a provider (or its deadline).
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from app.config import settings
from app.db import database as _database
from app.models import scrape_schemas as S
from app.services import scrape_gateway_cache as cache
from app.services import scrape_keys
from app.services import scrape_mappers as mappers
from app.services.omdb_service import OmdbRatings, omdb_service
from app.services.scrape_gateway_cache import CachedEntry, counters
from app.services.scrape_keys import CacheKey, EmptyNormalizedTitle, InvalidCacheKey
from app.services.scrape_mappers import ScrapeMappingError
from app.services.scrape_provider_errors import ProviderError
from app.services.tmdb_service import tmdb_service
from app.utils import time as clock
from app.utils.tasks import create_background_task

logger = logging.getLogger("plexhub.scrape_gateway")

MediaType = Literal["movie", "show"]
Outcome = Literal["hit", "miss", "coalesced"]

RETRY_AFTER_CAP_SECONDS = 300
DEFAULT_RETRY_AFTER_SECONDS = 30

# Strict ASCII + fullmatch (B1-08 security review): `\d` would accept other
# scripts' digits and `$` a trailing newline.
_IMDB_PARAM = re.compile(r"tt[0-9]{7,10}")
_TVDB_PARAM = re.compile(r"[0-9]{1,12}")


# --- Errors -----------------------------------------------------------------


class GatewayError(Exception):
    """An error body of the gateway (contract 4.6): HTTP status, `code`,
    a fixed generic `message` and an optional `retry_after` (seconds, already
    capped). `upstream` marks the errors caused by a provider (or its
    deadline) — they feed `counters.upstream_errors`.

    `__str__` is the status and the code only: this object is what the
    background task logger may print."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        retry_after: int | None = None,
        upstream: bool = False,
    ) -> None:
        self.status = status
        self.code = code
        self.message = message
        self.retry_after = retry_after
        self.upstream = upstream
        super().__init__(status, code)

    def __str__(self) -> str:
        return f"{self.status} {self.code}"

    def clone(self) -> GatewayError:
        return GatewayError(
            self.status, self.code, self.message,
            retry_after=self.retry_after, upstream=self.upstream,
        )


def invalid_parameter(message: str) -> GatewayError:
    return GatewayError(400, "invalid_parameter", message)


def _cap_retry_after(value: int | None) -> int:
    seconds = DEFAULT_RETRY_AFTER_SECONDS if value is None else int(value)
    return max(1, min(RETRY_AFTER_CAP_SECONDS, seconds))


def _bad_response(provider: str = "TMDB") -> GatewayError:
    return GatewayError(502, "upstream_error", f"Invalid response from {provider}", upstream=True)


def _timeout_error() -> GatewayError:
    return GatewayError(504, "upstream_timeout", "The provider did not answer in time", upstream=True)


def translate_provider_error(err: ProviderError) -> GatewayError:
    """THE translation table provider error -> gateway error (ADR D2.6).

    `not_found` is a negative result for the routes that expect it; it only
    reaches this function when it is unexpected (e.g. the details of a title
    the search just matched), hence 502."""
    name = "TMDB" if err.provider == "tmdb" else "OMDb"
    if err.kind == "not_configured":
        return GatewayError(503, "provider_not_configured", f"{name} is not configured on the backend")
    if err.kind == "rate_limited":
        return GatewayError(
            429, "rate_limited", f"{name} rate limit reached",
            retry_after=_cap_retry_after(err.retry_after), upstream=True,
        )
    if err.kind == "unavailable":
        return GatewayError(
            503, "upstream_unavailable", f"{name} is unreachable",
            retry_after=DEFAULT_RETRY_AFTER_SECONDS, upstream=True,
        )
    if err.kind == "timeout":
        return _timeout_error()
    return _bad_response(name)


# --- Plumbing -----------------------------------------------------------------


def _session_factory():
    """Read at call time (tests repoint `app.db.database.async_session_factory`;
    the cache module binds its default at import)."""
    return _database.async_session_factory


def _deadline() -> float:
    return time.monotonic() + scrape_keys.request_timeout_seconds()


def _language(value: str | None) -> str:
    language = settings.TMDB_LANGUAGE if value is None else value
    try:
        return scrape_keys.validate_language(language)
    except InvalidCacheKey:
        raise invalid_parameter("language must match ^[a-z]{2}-[A-Z]{2}$") from None


async def _read(key: CacheKey) -> CachedEntry | None:
    """Cache read; a database failure is a miss (logged, type only)."""
    try:
        return await cache.get(key, session_factory=_session_factory())
    except Exception as exc:  # noqa: BLE001 - the provider is still reachable
        logger.warning("scrape gateway: cache read failed, treated as a miss (family=%s, %s)",
                       key.family, type(exc).__name__)
        return None


PutItem = tuple[CacheKey, str, "dict | None", int]


async def _store(items: list[PutItem]) -> list[CachedEntry]:
    """Write `items` in ONE transaction (all or nothing, `put_many`) and return
    the entries as served. A failed write is logged (type only) and does not
    prevent the response (FR-018/D2.5, `cache: "miss"`)."""
    built = [
        CachedEntry(
            key=key, result=result, payload=payload, fetched_at=fetched_at,
            expires_at=cache.compute_expires_at(result, fetched_at),
        )
        for key, result, payload, fetched_at in items
    ]
    try:
        await cache.put_many(items, session_factory=_session_factory())
    except Exception as exc:  # noqa: BLE001 - serve the fetched data anyway
        logger.warning(
            "scrape gateway: cache write failed, response served uncached (family=%s, %s)",
            items[0][0].family if items else "?", type(exc).__name__,
        )
    return built


async def _guarded(coro: Awaitable[Any]) -> Any:
    """Body of every flight: no exception other than a fresh `GatewayError`
    (and cancellation) ever leaves it. The new error is raised OUTSIDE the
    `except` blocks: neither `__cause__` nor `__context__` can carry the
    original exception (whose message may embed a provider URL)."""
    failure: GatewayError
    try:
        return await coro
    except GatewayError as exc:
        failure = exc.clone()
    except ProviderError as exc:
        failure = translate_provider_error(exc)
    except (ScrapeMappingError, ValidationError):
        failure = _bad_response()
    except Exception as exc:  # noqa: BLE001 - anything else is a bad response
        logger.warning("scrape gateway: unexpected error during a provider call (%s)",
                       type(exc).__name__)
        failure = _bad_response()
    raise failure


_in_flight: dict[tuple[str, bool], asyncio.Task] = {}


def _running(fkey: tuple[str, bool]) -> asyncio.Task | None:
    task = _in_flight.get(fkey)
    return task if task is not None and not task.done() else None


def _join_or_start(
    fkey: tuple[str, bool], make: Callable[[], Awaitable[Any]],
) -> tuple[asyncio.Task, bool]:
    """(task, joined). NO await in here: lookup and insertion are atomic for
    the event loop."""
    task = _running(fkey)
    if task is not None:
        return task, True
    task = create_background_task(_guarded(make()), name="scrape-gateway-flight")
    _in_flight[fkey] = task

    def _forget(done: asyncio.Task, key: tuple[str, bool] = fkey) -> None:
        if _in_flight.get(key) is done:
            del _in_flight[key]

    task.add_done_callback(_forget)
    return task, False


async def _wait(task: asyncio.Task, deadline: float) -> Any:
    """Wait for a shared flight until the request deadline. The flight is
    shielded: neither the deadline nor a disconnected client cancels it."""
    remaining = deadline - time.monotonic()
    try:
        async with asyncio.timeout(max(remaining, 0.0)):
            return await asyncio.shield(task)
    except TimeoutError:
        pass
    raise _timeout_error()


async def _wait_counted(task: asyncio.Task, joined: bool, deadline: float) -> tuple[Any, Outcome]:
    if joined:
        counters.coalesce()
    else:
        counters.miss()
    try:
        value = await _wait(task, deadline)
    except GatewayError as exc:
        if exc.upstream:
            counters.upstream_error()
        raise
    return value, ("coalesced" if joined else "miss")


async def _resolve(
    key: CacheKey, refresh: bool, deadline: float, fetch: Callable[[], Awaitable[CachedEntry]],
) -> tuple[CachedEntry, Outcome]:
    """Cached entry of `key` (unless `refresh`), else the result of a shared
    flight running `fetch`."""
    fkey = (key.to_text(), refresh)
    task = _running(fkey)
    joined = task is not None
    if task is None:
        if not refresh:
            entry = await _read(key)
            if entry is not None:
                counters.hit()
                return entry, "hit"
        task, joined = _join_or_start(fkey, fetch)
    return await _wait_counted(task, joined, deadline)


def _envelope(
    key: CacheKey | None, language: str, fetched_at: int, expires_at: int, outcome: Outcome,
) -> dict[str, Any]:
    return {
        "schemaVersion": scrape_keys.SCHEMA_VERSION,
        "cacheKey": None if key is None else key.to_text(),
        "language": language,
        "fetchedAt": fetched_at,
        "expiresAt": expires_at,
        "cache": "hit" if outcome == "hit" else "miss",
    }


def _validated(model: type[BaseModel], body: dict) -> Any:
    try:
        return model.model_validate(body)
    except ValidationError:
        # Never log the error itself (it quotes the input values).
        logger.warning("scrape gateway: response failed its schema (%s)", model.__name__)
        raise _bad_response() from None


# --- OMDb composition (D2.10) -------------------------------------------------

_OMDB_PAYLOAD_KEYS = ("imdb", "rottenTomatoes", "metacritic")


def _omdb_payload(ratings: OmdbRatings) -> dict:
    by_source = {r["source"]: r for r in mappers.map_omdb_ratings(ratings)}
    return {name: by_source.get(name) for name in _OMDB_PAYLOAD_KEYS}


def _ratings_of(payload: dict | None) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    return [r for r in (payload.get(k) for k in _OMDB_PAYLOAD_KEYS) if isinstance(r, dict)]


OmdbOutcome = tuple[str, list[dict]]


async def _fetch_omdb(key: CacheKey, imdb_id: str, deadline: float) -> OmdbOutcome:
    fetched_at = clock.now_ms()
    try:
        ratings = await omdb_service.fetch_ratings(imdb_id, deadline=deadline)
    except ProviderError as exc:
        if exc.kind == "not_found":
            await _store([(key, "not_found", None, fetched_at)])
            return "not_found", []
        if exc.kind in ("budget_exhausted", "not_configured"):
            return exc.kind, []
        logger.info("scrape gateway: OMDb unavailable for this response (%s)", exc.kind)
        return "error", []
    payload = _omdb_payload(ratings)
    await _store([(key, "found", payload, fetched_at)])
    return "ok", _ratings_of(payload)


async def _omdb_ratings(imdb_id: str | None, deadline: float) -> OmdbOutcome:
    """(omdbStatus, Rating dicts). Never raises: OMDb never fails a response."""
    if not imdb_id:
        return "no_imdb_id", []
    try:
        key = scrape_keys.omdb_key(imdb_id)
    except InvalidCacheKey:
        return "no_imdb_id", []
    fkey = (key.to_text(), False)
    task = _running(fkey)
    if task is None:
        # `refresh` never redoes a FRESH omdb entry (FR-024): always read.
        entry = await _read(key)
        if entry is not None:
            if entry.result == "found":
                return "ok", _ratings_of(entry.payload)
            return "not_found", []
        task, _ = _join_or_start(fkey, lambda: _fetch_omdb(key, imdb_id, deadline))
    try:
        return await _wait(task, deadline)
    except GatewayError:
        return "error", []


async def _compose_details(
    details: dict, fetched_at: int, expires_at: int, deadline: float,
) -> tuple[dict, int]:
    """Response form of cached movie/show details + the response `expiresAt`
    (contract 4.4: shortened to the negative TTL when OMDb failed)."""
    external = details.get("externalIds") if isinstance(details.get("externalIds"), dict) else {}
    status, ratings = await _omdb_ratings(external.get("imdb"), deadline)
    composed = mappers.with_omdb(details, ratings, status)
    if status in ("budget_exhausted", "error"):
        expires_at = min(expires_at, fetched_at + scrape_keys.ttl_negative_ms())
    return composed, expires_at


# --- Provider fetches run inside a flight ------------------------------------


def _details_key(media_type: MediaType, tmdb_id: int, language: str) -> CacheKey:
    if media_type == "movie":
        return scrape_keys.movie_key(tmdb_id, language)
    return scrape_keys.show_key(tmdb_id, language)


async def _fetch_details_raw(media_type: MediaType, tmdb_id: int, language: str, deadline: float) -> dict:
    if media_type == "movie":
        raw = await tmdb_service.fetch_movie(tmdb_id, language=language, deadline=deadline)
        details = mappers.map_movie(raw, language=language)
        S.MovieDetails.model_validate(mappers.with_omdb(details, None, "no_imdb_id"))
    else:
        raw = await tmdb_service.fetch_tv(tmdb_id, language=language, deadline=deadline)
        details = mappers.map_show(raw, language=language)
        S.ShowDetails.model_validate(mappers.with_omdb(details, None, "no_imdb_id"))
    return details


async def _fetch_details_entry(
    media_type: MediaType, tmdb_id: int, language: str, deadline: float,
) -> CachedEntry:
    key = _details_key(media_type, tmdb_id, language)
    fetched_at = clock.now_ms()
    try:
        details = await _fetch_details_raw(media_type, tmdb_id, language, deadline)
    except ProviderError as exc:
        if exc.kind != "not_found":
            raise
        (entry,) = await _store([(key, "not_found", None, fetched_at)])
        return entry
    (entry,) = await _store([(key, "found", details, fetched_at)])
    return entry


@dataclass(frozen=True)
class _MatchResolved:
    match: CachedEntry
    details: CachedEntry | None


def _matched_tmdb_id(entry: CachedEntry) -> int | None:
    payload = entry.payload if isinstance(entry.payload, dict) else {}
    tmdb_id = payload.get("tmdbId")
    if isinstance(tmdb_id, bool) or not isinstance(tmdb_id, int) or tmdb_id < 1:
        return None
    return tmdb_id


async def _match_flight(
    key: CacheKey, media_type: MediaType, title: str, year: int | None,
    language: str, refresh: bool, deadline: float,
) -> _MatchResolved:
    if not refresh:
        current = await _read(key)
        if current is not None:
            tmdb_id = _matched_tmdb_id(current) if current.result == "matched" else None
            if current.result != "matched":
                return _MatchResolved(current, None)
            if tmdb_id is not None:
                # Fresh match whose details expired or were invalidated:
                # re-fetch the details only, in this same flight (FR-018).
                details = await _read(_details_key(media_type, tmdb_id, language))
                if details is None or details.result != "found":
                    details = await _fetch_details_entry(media_type, tmdb_id, language, deadline)
                if details.result != "found":
                    raise _bad_response()
                return _MatchResolved(current, details)
            # A matched entry without a usable tmdbId: recompute below.

    fetched_at = clock.now_ms()
    scored = await tmdb_service.search_scored(
        media_type, title, year, language=language, deadline=deadline,
    )
    confidence = min(1.0, max(0.0, float(scored.confidence)))
    if scored.verdict == "matched":
        tmdb_id = scored.best_tmdb_id
        if tmdb_id is None:
            raise _bad_response()
        try:
            details = await _fetch_details_raw(media_type, tmdb_id, language, deadline)
        except ProviderError as exc:
            if exc.kind == "not_found":
                raise _bad_response() from None
            raise
        entries = await _store([
            (key, "matched", {"confidence": confidence, "tmdbId": tmdb_id}, fetched_at),
            (_details_key(media_type, tmdb_id, language), "found", details, fetched_at),
        ])
        return _MatchResolved(entries[0], entries[1])
    if scored.verdict == "ambiguous":
        payload = {"confidence": confidence, "candidates": mappers.map_candidates(scored.candidates)}
        (entry,) = await _store([(key, "ambiguous", payload, fetched_at)])
        return _MatchResolved(entry, None)
    (entry,) = await _store([(key, "nomatch", None, fetched_at)])
    return _MatchResolved(entry, None)


# --- Data routes -------------------------------------------------------------


async def match(
    media_type: MediaType, title: str, year: int | None, language: str | None, refresh: bool,
) -> tuple[S.MatchResponse, Outcome]:
    lang = _language(language)
    try:
        key = scrape_keys.match_key(media_type, title, year, lang)
    except EmptyNormalizedTitle:
        raise invalid_parameter("title normalizes to an empty string") from None
    except InvalidCacheKey:
        raise invalid_parameter("invalid match parameters") from None
    deadline = _deadline()
    fkey = (key.to_text(), refresh)

    resolved: _MatchResolved | None = None
    outcome: Outcome = "hit"
    task = _running(fkey)
    joined = task is not None
    if task is None and not refresh:
        current = await _read(key)
        if current is not None and current.result != "matched":
            resolved = _MatchResolved(current, None)
        elif current is not None:
            tmdb_id = _matched_tmdb_id(current)
            if tmdb_id is not None:
                details = await _read(_details_key(media_type, tmdb_id, lang))
                if details is not None and details.result == "found":
                    resolved = _MatchResolved(current, details)
        if resolved is not None:
            counters.hit()
        else:
            task = _running(fkey)
            joined = task is not None
    if resolved is None:
        if task is None:
            task, joined = _join_or_start(
                fkey, lambda: _match_flight(key, media_type, title, year, lang, refresh, deadline),
            )
        resolved, outcome = await _wait_counted(task, joined, deadline)

    entry = resolved.match
    payload = entry.payload if isinstance(entry.payload, dict) else {}
    fetched_at, expires_at = entry.fetched_at, entry.expires_at
    details_body = None
    tmdb_id = None
    if entry.result == "matched" and resolved.details is not None:
        tmdb_id = _matched_tmdb_id(entry)
        expires_at = min(expires_at, resolved.details.expires_at)
        details_body, expires_at = await _compose_details(
            resolved.details.payload or {}, fetched_at, expires_at, deadline,
        )
    confidence = payload.get("confidence") if entry.result != "nomatch" else None
    body = {
        **_envelope(key, lang, fetched_at, expires_at, outcome),
        "result": entry.result,
        "confidence": confidence,
        "tmdbId": tmdb_id,
        "mediaType": media_type,
        "candidates": (payload.get("candidates") or []) if entry.result == "ambiguous" else [],
        "details": details_body,
    }
    return _validated(S.MatchResponse, body), outcome


async def details(
    media_type: MediaType, tmdb_id: int, language: str | None, refresh: bool,
) -> tuple[S.MovieResponse | S.ShowResponse, Outcome]:
    lang = _language(language)
    try:
        key = _details_key(media_type, tmdb_id, lang)
    except InvalidCacheKey:
        raise invalid_parameter("tmdbId must be a positive integer") from None
    deadline = _deadline()
    entry, outcome = await _resolve(
        key, refresh, deadline, lambda: _fetch_details_entry(media_type, tmdb_id, lang, deadline),
    )
    expires_at = entry.expires_at
    body_details = None
    if entry.result == "found":
        body_details, expires_at = await _compose_details(
            entry.payload or {}, entry.fetched_at, expires_at, deadline,
        )
    body = {
        **_envelope(key, lang, entry.fetched_at, expires_at, outcome),
        "result": entry.result,
        "details": body_details,
    }
    model = S.MovieResponse if media_type == "movie" else S.ShowResponse
    return _validated(model, body), outcome


async def _fetch_season_entry(key: CacheKey, show_id: int, season: int, language: str, deadline: float) -> CachedEntry:
    fetched_at = clock.now_ms()
    try:
        raw = await tmdb_service.fetch_season(show_id, season, language=language, deadline=deadline)
    except ProviderError as exc:
        if exc.kind != "not_found":
            raise
        (entry,) = await _store([(key, "not_found", None, fetched_at)])
        return entry
    details = mappers.map_season(raw, show_tmdb_id=show_id, language=language)
    S.SeasonDetails.model_validate(details)
    (entry,) = await _store([(key, "found", details, fetched_at)])
    return entry


async def season(
    show_id: int, season_number: int, language: str | None, refresh: bool,
) -> tuple[S.SeasonResponse, Outcome]:
    lang = _language(language)
    try:
        key = scrape_keys.season_key(show_id, season_number, lang)
    except InvalidCacheKey:
        raise invalid_parameter("invalid season parameters") from None
    deadline = _deadline()
    entry, outcome = await _resolve(
        key, refresh, deadline,
        lambda: _fetch_season_entry(key, show_id, season_number, lang, deadline),
    )
    body = {
        **_envelope(key, lang, entry.fetched_at, entry.expires_at, outcome),
        "result": entry.result,
        "details": entry.payload if entry.result == "found" else None,
    }
    return _validated(S.SeasonResponse, body), outcome


async def _fetch_find_entry(key: CacheKey, source: str, external_id: str, deadline: float) -> CachedEntry:
    fetched_at = clock.now_ms()
    try:
        raw = await tmdb_service.find_external(source, external_id, deadline=deadline)  # type: ignore[arg-type]
    except ProviderError as exc:
        if exc.kind != "not_found":
            raise
        raw = {}
    matches = mappers.map_find(raw)
    for item in matches:
        S.FindMatch.model_validate(item)
    if not matches:
        (entry,) = await _store([(key, "not_found", None, fetched_at)])
    else:
        (entry,) = await _store([(key, "found", {"matches": matches}, fetched_at)])
    return entry


async def find(
    imdb_id: str | None, tvdb_id: str | None, refresh: bool = False,
) -> tuple[S.FindResponse, Outcome]:
    """`refresh` stays available to callers of the service; the route does not
    expose it (contract 5.7 defines no `refresh` for /find)."""
    if (imdb_id is None) == (tvdb_id is None):
        raise invalid_parameter("exactly one of imdbId or tvdbId is required")
    if imdb_id is not None:
        if not isinstance(imdb_id, str) or _IMDB_PARAM.fullmatch(imdb_id) is None:
            raise invalid_parameter("imdbId must match ^tt[0-9]{7,10}$")
        source, raw_id = "imdb", imdb_id
    else:
        if not isinstance(tvdb_id, str) or _TVDB_PARAM.fullmatch(tvdb_id) is None:
            raise invalid_parameter("tvdbId must be 1 to 12 digits")
        source, raw_id = "tvdb", tvdb_id
    try:
        key = scrape_keys.find_key(source, raw_id)
    except InvalidCacheKey:
        raise invalid_parameter(f"invalid {source} id") from None
    external_id = key.key.split("|", 2)[2]  # normalised (tvdb "007" -> "7", X19)
    deadline = _deadline()
    entry, outcome = await _resolve(
        key, refresh, deadline, lambda: _fetch_find_entry(key, source, external_id, deadline),
    )
    payload = entry.payload if isinstance(entry.payload, dict) else {}
    body = {
        **_envelope(key, key.language, entry.fetched_at, entry.expires_at, outcome),
        "result": entry.result,
        "matches": (payload.get("matches") or []) if entry.result == "found" else [],
    }
    return _validated(S.FindResponse, body), outcome


async def search(
    media_type: MediaType, query: str, year: int | None, language: str | None,
) -> tuple[S.ScrapeSearchResponse, Outcome]:
    """Not persisted, not coalesced (D2.7): `cacheKey` null, `expiresAt` =
    `fetchedAt`, always `cache: "miss"`."""
    lang = _language(language)
    deadline = _deadline()
    counters.miss()
    fetched_at = clock.now_ms()
    failure: GatewayError | None = None
    candidates: list[dict] = []
    try:
        async with asyncio.timeout(max(deadline - time.monotonic(), 0.0)):
            scored = await tmdb_service.search_candidates_raw(
                media_type, query, year, language=lang, deadline=deadline,
            )
        candidates = mappers.map_candidates(scored.candidates)
    except TimeoutError:
        failure = _timeout_error()
    except ProviderError as exc:
        failure = translate_provider_error(exc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("scrape gateway: unexpected error during a search (%s)", type(exc).__name__)
        failure = _bad_response()
    if failure is not None:
        if failure.upstream:
            counters.upstream_error()
        raise failure
    body = {
        **_envelope(None, lang, fetched_at, fetched_at, "miss"),
        "result": "found" if candidates else "not_found",
        "candidates": candidates,
    }
    return _validated(S.ScrapeSearchResponse, body), "miss"


# --- Cache administration (D2.11) ---------------------------------------------

_DAY_MS = 86_400_000


async def stats() -> S.CacheStatsResponse:
    families = await cache.stats(session_factory=_session_factory())
    return S.CacheStatsResponse.model_validate({
        "generatedAt": clock.now_ms(),
        "families": families,
        "ttl": {
            "successDays": scrape_keys.ttl_success_ms() // _DAY_MS,
            "negativeDays": scrape_keys.ttl_negative_ms() // _DAY_MS,
        },
        "sinceStart": counters.snapshot(),
    })


INVALIDATE_FAMILIES = ("movie", "show", "season")


async def invalidate(request: S.InvalidateRequest) -> S.InvalidateResponse:
    by_keys = request.keys is not None
    by_family = request.family is not None or request.tmdb_id is not None
    if by_keys == by_family:
        raise invalid_parameter("send either keys, or family and tmdbId")
    if by_keys:
        try:
            deleted = await cache.invalidate_keys(request.keys or [], session_factory=_session_factory())
        except InvalidCacheKey:
            raise invalid_parameter("malformed cache key, nothing deleted") from None
        return S.InvalidateResponse(deleted=deleted)
    if request.family not in INVALIDATE_FAMILIES:
        raise invalid_parameter("family must be movie, show or season")
    if request.tmdb_id is None:
        raise invalid_parameter("tmdbId is required with family")
    deleted = await cache.invalidate_family(
        request.family, request.tmdb_id, session_factory=_session_factory(),  # type: ignore[arg-type]
    )
    return S.InvalidateResponse(deleted=deleted)


async def purge(limit: int) -> S.PurgeResponse:
    deleted, remaining = await cache.purge(limit, session_factory=_session_factory())
    return S.PurgeResponse(deleted=deleted, remaining=remaining)


def _iter_flights() -> Iterable[asyncio.Task]:
    """Test helper: the flights currently tracked."""
    return list(_in_flight.values())
