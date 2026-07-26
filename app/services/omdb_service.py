"""OMDb HTTP client — imdb-id consistency validation + enrichment support.

OMDb (https://www.omdbapi.com) is consulted by `imdb_id` (`get_by_imdb_id`,
used to cross-check that a `media.tmdb_id`/`media.imdb_id` pair genuinely
refers to the same title, see
`docs/plans/2026-07-17-omdb-id-consistency-validator-design.md`) and, since
`docs/plans/2026-07-20-omdb-rating-enrichment-design.md`, by title
(`search_by_title`, a fallback OMDb-by-title scrape for items TMDB failed to
match — the caller decides how strong a title match must be before trusting
it for identity, this module only returns OMDb's raw best `?t=` hit). The
detector/corrector script (`app/scripts/validate_id_consistency.py`) and the
enrichment worker are out of scope for this module.

Architectural mirror of `app.services.tmdb_service` (client pooling,
retry/backoff shape, real-call-count budgeting) — see
`TMDBService._request` (tmdb_service.py:174-220) for the semantics this
mirrors. One deliberate deviation from that mirror: the OMDb key rides on
every request as an `apikey` query param, and `httpx.HTTPStatusError.__str__`
embeds the full request URL (including query string) — so, unlike
`tmdb_service.find_by_imdb_id` (which logs `exc` verbatim), this module NEVER
logs the raw exception text; only the exception type / HTTP status code is
logged (see `get_by_imdb_id`), so the API key can never leak into logs.

Observability (AUDIT-P8-002 / S5.4, `docs/plans/2026-07-26-refacto-audit-v1-plan.md`
§VAGUE 5): every terminal outcome of `get_by_imdb_id`/`search_by_title`
increments `plexhub_omdb_requests_total{result}` (declared + zero-init'd in
`app.utils.metrics`, `result in {ok, not_found, error, rate_limited,
budget_exhausted}`) so that OMDb's fail-open `OMDB_DAILY_LIMIT` exhaustion
(silent today — enrichment just proceeds with TMDB-only data) becomes
visible. Never labelled with the imdb_id/title/URL/key (closed, secret-free
label set — same guard as the log lines above).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

import httpx

from app.config import settings
from app.utils.metrics import omdb_requests_total

logger = logging.getLogger("plexhub.omdb")

_RETRY_DELAYS = (1, 2, 4)
_RETRYABLE = (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError)


@dataclass
class OMDbData:
    """Parsed OMDb response (`?i=<imdb_id>` lookup or `?t=<title>` search)."""
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
    # Additive (defaulted so old `omdb_scrape_cache` payloads deserialize via
    # `OMDbData(**json.loads(payload))` in `omdb_scrape_cache_service.py`
    # without an `imdb_id` key — see tests/test_omdb_service.py back-compat
    # case). Populated on both `get_by_imdb_id` (echoes the looked-up id) and
    # `search_by_title` (the id OMDb resolved the title to).
    imdb_id: str | None = None


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

    def _budget_exhausted(self) -> bool:
        """`True` once this run's `real_request_count` has reached
        `OMDB_DAILY_LIMIT`.

        `enrichment_worker`/`enrichment_backfill_worker` already gate calls
        externally (`if omdb_service.get_request_count() >= OMDB_DAILY_LIMIT:
        skip`) before ever calling `get_by_imdb_id`/`search_by_title` — this
        is a second, self-contained gate on the SAME counter so that (a) any
        caller of this module gets the same fail-open guarantee without
        having to duplicate the check itself, and (b)
        `plexhub_omdb_requests_total{result="budget_exhausted"}` (AUDIT-P8-002
        / S5.4) has one single, always-reachable emission point that does not
        require touching `app/workers/*`. No behaviour change for the
        existing worker call sites: their own pre-check already stops them
        from reaching this branch."""
        return self.real_request_count >= settings.OMDB_DAILY_LIMIT

    async def _request(self, path: str, params: dict | None = None) -> dict:
        """GET with retry + exponential backoff + 429 rate-limit handling.

        Mirrors `TMDBService._request` line for line (same retry shape, same
        real-call counting semantics on `real_request_count` — every retry
        attempt bumps it, not just the logical call). No Prometheus counter
        is incremented HERE: `plexhub_omdb_requests_total{result}`
        (AUDIT-P8-002 / S5.4) is emitted once per logical call by
        `get_by_imdb_id`/`search_by_title` instead, since only they know
        whether a successful HTTP response was actually a match
        (`result="ok"`) or an OMDb-level "not found" (`result="not_found"`)
        — a distinction `_request` itself cannot make (it only sees raw
        JSON). Logging never includes the raw exception text (see module
        docstring) so `apikey` cannot leak via a log line."""
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

        Returns None when: OMDb is unconfigured, `imdb_id` is blank, the
        `OMDB_DAILY_LIMIT` budget is already exhausted, OMDb reports "not
        found" (`Response: "False"`), or any transport/HTTP failure occurs.
        Failures are logged with exception type / HTTP status only — never
        the raw exception text (see module docstring), so the semantics
        mirror `tmdb_service.find_by_imdb_id` (graceful None on failure)
        rather than `search_movie` (which propagates).

        Every terminal outcome increments
        `plexhub_omdb_requests_total{result=...}` (AUDIT-P8-002 / S5.4) —
        exactly once per call, never per retry attempt inside `_request`
        (that finer-grained count is `real_request_count`, used for the
        `OMDB_DAILY_LIMIT` budget itself, not for this Prometheus counter).
        Blank id / unconfigured short-circuits emit nothing (no HTTP call was
        even considered — same as before this instrumentation)."""
        if not imdb_id:
            return None
        if not self.is_configured:
            return None
        if self._budget_exhausted():
            omdb_requests_total.labels(result="budget_exhausted").inc()
            return None
        try:
            data = await self._request("/", params={"i": imdb_id, "plot": "full"})
        except httpx.HTTPStatusError as exc:
            omdb_requests_total.labels(
                result="rate_limited" if exc.response.status_code == 429 else "error"
            ).inc()
            logger.warning(
                "OMDb get_by_imdb_id failed for %s (HTTP %s)", imdb_id, exc.response.status_code,
            )
            return None
        except Exception as exc:
            omdb_requests_total.labels(result="error").inc()
            logger.warning(
                "OMDb get_by_imdb_id failed for %s (%s)", imdb_id, type(exc).__name__,
            )
            return None

        if data.get("Response") != "True":
            omdb_requests_total.labels(result="not_found").inc()
            return None

        omdb_requests_total.labels(result="ok").inc()
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
            # Consistency: echo the id we looked up, falling back to OMDb's
            # own `imdbID` field (they should always agree on a match).
            imdb_id=data.get("imdbID") or imdb_id,
        )

    async def search_by_title(
        self, title: str, year: int | None, media_type: str
    ) -> OMDbData | None:
        """OMDb `?t=<title>&y=<year>&type=movie|series&plot=full` — single
        best match (title search, not a validation-only lookup).

        `media_type` "movie"/"show" maps to OMDb's "movie"/"series"; any
        other value omits the `type` filter rather than guessing. Returns
        `OMDbData` with `imdb_id` populated (from `imdbID`), or None when:
        OMDb is unconfigured, `title` is blank, the `OMDB_DAILY_LIMIT` budget
        is already exhausted, OMDb reports "not found" (`Response: "False"`),
        or any transport/HTTP failure occurs — same graceful-None shape as
        `get_by_imdb_id`. Counts real HTTP attempts via `_request` (same
        `OMDB_DAILY_LIMIT` budget). The API key is never logged (see module
        docstring): only exception type / HTTP status, never `str(exc)`.

        Same `plexhub_omdb_requests_total{result=...}` instrumentation as
        `get_by_imdb_id` (AUDIT-P8-002 / S5.4) — one increment per call, on
        every terminal outcome."""
        if not title:
            return None
        if not self.is_configured:
            return None
        if self._budget_exhausted():
            omdb_requests_total.labels(result="budget_exhausted").inc()
            return None

        params: dict = {"t": title, "plot": "full"}
        if year is not None:
            params["y"] = str(year)
        omdb_type = {"movie": "movie", "show": "series"}.get(media_type)
        if omdb_type is not None:
            params["type"] = omdb_type

        try:
            data = await self._request("/", params=params)
        except httpx.HTTPStatusError as exc:
            omdb_requests_total.labels(
                result="rate_limited" if exc.response.status_code == 429 else "error"
            ).inc()
            logger.warning(
                "OMDb search_by_title failed for %r (HTTP %s)", title, exc.response.status_code,
            )
            return None
        except Exception as exc:
            omdb_requests_total.labels(result="error").inc()
            logger.warning(
                "OMDb search_by_title failed for %r (%s)", title, type(exc).__name__,
            )
            return None

        if data.get("Response") != "True":
            omdb_requests_total.labels(result="not_found").inc()
            return None

        omdb_requests_total.labels(result="ok").inc()
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
            imdb_id=_clean_str(data.get("imdbID")),
        )


# Singleton
omdb_service = OMDbService()
