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
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Generator, Optional

import httpx

from app.config import settings
from app.services.scrape_provider_errors import ProviderError
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


@dataclass(frozen=True)
class OMDbHit:
    """One entry of an OMDb `?s=` list search (ADR 0005 D3).

    Deliberately NOT an `OMDbData`: the list endpoint returns only five
    fields per hit (no plot/rating/runtime/genre), so a separate, honest
    shape avoids handing callers an `OMDbData` whose interesting columns are
    all `None` for a reason they cannot distinguish from "OMDb had no data".
    """
    imdb_id: str
    title: str
    year: int | None  # first 4 digits of "Year" ("2015–2019" -> 2015)
    type: str  # "movie" | "series" | "episode" — passed through as-is
    poster_url: str | None  # "N/A" -> None


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


@dataclass
class RequestTally:
    """Per-context counter of real OMDb HTTP attempts (ADR 0005 D2) — mirrors
    `app.services.tmdb_service.RequestTally`/`count_requests` exactly, as an
    independent counter (a caller measuring OMDb spend never shares state
    with a caller measuring TMDB spend, or vice versa). Nested blocks
    AGGREGATE, same as TMDB's: `add()` credits every enclosing tally."""
    count: int = 0
    parent: "RequestTally | None" = None

    def add(self, n: int = 1) -> None:
        tally: "RequestTally | None" = self
        while tally is not None:
            tally.count += n
            tally = tally.parent


_request_tally: ContextVar["RequestTally | None"] = ContextVar("omdb_request_tally", default=None)


@contextmanager
def count_requests() -> Generator[RequestTally, None, None]:
    """Count real OMDb HTTP attempts made in this context (and in asyncio
    tasks created from it). See
    `app.services.tmdb_service.count_requests` for the exact semantics this
    mirrors (aggregating on nesting, never touches
    `OMDbService.real_request_count`)."""
    tally = RequestTally(parent=_request_tally.get())
    token = _request_tally.set(tally)
    try:
        yield tally
    finally:
        _request_tally.reset(token)


def _parse_year_prefix(value) -> int | None:
    """OMDb's "Year" field -> leading 4-digit year. Series return ranges
    ("2015–2019", "2015–"), so only the first four digits are meaningful."""
    cleaned = _clean_str(value)
    if cleaned is None or len(cleaned) < 4 or not cleaned[:4].isdigit():
        return None
    return int(cleaned[:4])


def _parse_imdb_votes(value) -> int | None:
    """"1,234,567" -> 1234567. "N/A" / unparseable -> None."""
    cleaned = _clean_str(value)
    if cleaned is None:
        return None
    digits = cleaned.replace(",", "")
    return int(digits) if digits.isdigit() else None


@dataclass(frozen=True)
class OmdbRatings:
    """Raw parsed ratings of `OMDbService.fetch_ratings` (B1-08): IMDb 0..10 +
    votes, Rotten Tomatoes 0..100 and Metacritic 0..100 as ints. "N/A" or
    absent -> None."""
    imdb_rating: float | None
    imdb_votes: int | None
    rotten_tomatoes: int | None
    metacritic: int | None


_DEFAULT_RETRY_AFTER = 30
# Normalised (lower-case, trailing "." / "!" stripped) OMDb `Error` strings.
_OMDB_NOT_FOUND = frozenset({"incorrect imdb id", "movie not found", "error getting data"})
_OMDB_NOT_CONFIGURED = frozenset({"invalid api key", "no api key provided"})


def _parse_retry_after(value: str | None) -> int | None:
    """Integer `Retry-After` seconds, or None (absent / HTTP-date / garbage)."""
    if value is None:
        return None
    value = value.strip()
    return int(value) if value.isdigit() else None


def _score_0_100(text) -> int | None:
    """"87%" / "74/100" / "74" -> int in 0..100, else None."""
    cleaned = _clean_str(text)
    if cleaned is None:
        return None
    head = cleaned.split("/", 1)[0].strip().rstrip("%").strip()
    if not head.isdigit():
        return None
    value = int(head)
    return value if 0 <= value <= 100 else None


def _parse_ratings(data: dict) -> OmdbRatings:
    rt: int | None = None
    mc: int | None = None
    for entry in data.get("Ratings") or []:
        if not isinstance(entry, dict):
            continue
        source = entry.get("Source")
        if source == "Rotten Tomatoes" and rt is None:
            rt = _score_0_100(entry.get("Value"))
        elif source == "Metacritic" and mc is None:
            mc = _score_0_100(entry.get("Value"))
    if mc is None:
        mc = _score_0_100(data.get("Metascore"))
    return OmdbRatings(
        imdb_rating=_parse_imdb_rating(data.get("imdbRating")),
        imdb_votes=_parse_imdb_votes(data.get("imdbVotes")),
        rotten_tomatoes=rt,
        metacritic=mc,
    )


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
            tally = _request_tally.get()
            if tally is not None:
                tally.add()
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

    async def _request_bounded(self, params: dict, *, deadline: float) -> tuple[int, dict]:
        """Strict sibling of `_request` (B1-08): same retry loop / counters,
        bounded by `deadline` (absolute `time.monotonic()`), every failure
        translated into `ProviderError` raised OUTSIDE the `except` blocks
        (no `__cause__`/`__context__`, so the `apikey=` URL cannot survive).

        Returns `(status_code, json_body)` for a 2xx, and ALSO for a 401 whose
        body is a JSON object: OMDb answers "Invalid API key!" and "Request
        limit reached!" with HTTP 401 + a JSON `Error`, which the caller must
        tell apart. Any other failure raises."""
        client = await self._get_client()
        url = f"{self.BASE_URL}/"
        final: ProviderError | None = None
        for attempt, delay in enumerate((*_RETRY_DELAYS, None)):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                final = ProviderError("timeout", provider="omdb")
                break
            self.real_request_count += 1
            tally = _request_tally.get()
            if tally is not None:
                tally.add()

            wait: float | None = None
            wait_kind = "timeout"
            wait_retry_after: int | None = None
            try:
                resp = await client.get(url, params=params, timeout=min(10.0, remaining))
                status = resp.status_code
                if status == 429:
                    header_ra = _parse_retry_after(resp.headers.get("Retry-After"))
                    if delay is not None:
                        wait = float(header_ra if header_ra is not None else delay)
                        wait_kind = "rate_limited"
                        wait_retry_after = int(wait)
                    else:
                        final = ProviderError(
                            "rate_limited", provider="omdb", status_code=429,
                            retry_after=header_ra if header_ra is not None else _DEFAULT_RETRY_AFTER,
                        )
                elif status in (502, 503, 504):
                    if delay is not None:
                        wait, wait_kind = float(delay), "unavailable"
                    else:
                        final = ProviderError("unavailable", provider="omdb", status_code=status)
                elif status == 401:
                    try:
                        body = resp.json()
                    except ValueError:
                        body = None
                    if isinstance(body, dict):
                        return status, body
                    final = ProviderError("not_configured", provider="omdb", status_code=status)
                elif status == 403:
                    final = ProviderError("not_configured", provider="omdb", status_code=status)
                elif status == 404:
                    final = ProviderError("not_found", provider="omdb", status_code=status)
                elif status >= 400:
                    final = ProviderError("bad_response", provider="omdb", status_code=status)
                else:
                    body = resp.json()
                    if not isinstance(body, dict):
                        final = ProviderError("bad_response", provider="omdb", status_code=status)
                    else:
                        return status, body
            except httpx.TimeoutException as e:
                logger.warning("OMDb attempt %d timed out (%s)", attempt + 1, type(e).__name__)
                if delay is not None:
                    wait, wait_kind = float(delay), "timeout"
                else:
                    final = ProviderError("timeout", provider="omdb")
            except (httpx.ConnectError, httpx.RemoteProtocolError) as e:
                logger.warning("OMDb attempt %d failed (%s)", attempt + 1, type(e).__name__)
                if delay is not None:
                    wait, wait_kind = float(delay), "unavailable"
                else:
                    final = ProviderError("unavailable", provider="omdb")
            except Exception as e:  # noqa: BLE001 - everything else is a bad response
                logger.warning("OMDb bad response (%s)", type(e).__name__)
                final = ProviderError("bad_response", provider="omdb")

            if final is not None:
                break
            if wait is not None:
                if wait >= deadline - time.monotonic():
                    if wait_kind == "rate_limited":
                        final = ProviderError(
                            "rate_limited", provider="omdb",
                            retry_after=wait_retry_after, status_code=429,
                        )
                    else:
                        final = ProviderError("timeout", provider="omdb")
                    break
                logger.warning("OMDb retrying in %ss (%s)", wait, wait_kind)
                await asyncio.sleep(wait)
        if final is None:  # defensive: the last attempt always sets `final`
            final = ProviderError("unavailable", provider="omdb")
        raise final

    async def fetch_ratings(self, imdb_id: str, *, deadline: float) -> OmdbRatings:
        """Strict ratings lookup for the gateway (B1-08, ADR 0009 D2.6).

        Raises `ProviderError` (never returns None): `not_configured` (no key,
        "Invalid API key!", "No API key provided."), `budget_exhausted`
        (`OMDB_DAILY_LIMIT` reached, or "Request limit reached!"),
        `not_found` ("Incorrect IMDb ID.", "Movie not found!", "Error getting
        data."), `rate_limited`/`unavailable`/`timeout` (transport),
        `bad_response` (anything else). Same counter and the same
        `plexhub_omdb_requests_total` metric as `get_by_imdb_id`, one
        increment per call. Nothing is cached here."""
        if not self.is_configured:
            raise ProviderError("not_configured", provider="omdb")
        if self._budget_exhausted():
            omdb_requests_total.labels(result="budget_exhausted").inc()
            raise ProviderError("budget_exhausted", provider="omdb")
        try:
            status, data = await self._request_bounded(
                {"i": imdb_id, "plot": "short"}, deadline=deadline,
            )
        except ProviderError as err:
            omdb_requests_total.labels(
                result="rate_limited" if err.kind == "rate_limited"
                else "not_found" if err.kind == "not_found" else "error"
            ).inc()
            raise

        response = data.get("Response")
        if response == "True" and status < 400:
            omdb_requests_total.labels(result="ok").inc()
            return _parse_ratings(data)

        error = data.get("Error") if isinstance(data.get("Error"), str) else ""
        norm = error.strip().rstrip(".!").strip().lower()
        if response == "False" and norm in _OMDB_NOT_FOUND:
            omdb_requests_total.labels(result="not_found").inc()
            raise ProviderError("not_found", provider="omdb", status_code=status)
        if response == "False" and norm == "request limit reached":
            omdb_requests_total.labels(result="budget_exhausted").inc()
            raise ProviderError("budget_exhausted", provider="omdb", status_code=status)
        if response == "False" and norm in _OMDB_NOT_CONFIGURED:
            omdb_requests_total.labels(result="error").inc()
            raise ProviderError("not_configured", provider="omdb", status_code=status)
        omdb_requests_total.labels(result="error").inc()
        raise ProviderError("bad_response", provider="omdb", status_code=status)

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

    async def search_list(
        self, query: str, year: int | None, media_type: str,
    ) -> list[OMDbHit]:
        """OMDb `?s=<query>[&y=][&type=movie|series]` — page 1 only (<=10
        hits), used by the manual scraper when TMDB returned nothing or the
        operator forced the OMDb provider (ADR 0005 D3).

        Same guarantees as `search_by_title`: no-op (`[]`) when OMDb is
        unconfigured or `query` is blank, `_budget_exhausted()` gate, `[]`
        on any transport/HTTP failure or on OMDb's `Response: "False"`, one
        `plexhub_omdb_requests_total{result}` increment per call. The API
        key never reaches a log line — exception TYPE / HTTP status only,
        never `str(exc)` (which embeds the `apikey` query param)."""
        if not query:
            return []
        if not self.is_configured:
            return []
        if self._budget_exhausted():
            omdb_requests_total.labels(result="budget_exhausted").inc()
            return []

        params: dict = {"s": query}
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
                "OMDb search_list failed for %r (HTTP %s)", query, exc.response.status_code,
            )
            return []
        except Exception as exc:
            omdb_requests_total.labels(result="error").inc()
            logger.warning("OMDb search_list failed for %r (%s)", query, type(exc).__name__)
            return []

        if data.get("Response") != "True":
            omdb_requests_total.labels(result="not_found").inc()
            return []

        omdb_requests_total.labels(result="ok").inc()
        hits: list[OMDbHit] = []
        for raw in data.get("Search") or []:
            if not isinstance(raw, dict):
                continue
            imdb_id = _clean_str(raw.get("imdbID"))
            if not imdb_id:
                continue  # an entry without an id is unusable as an identity
            hits.append(OMDbHit(
                imdb_id=imdb_id,
                title=_clean_str(raw.get("Title")) or "",
                year=_parse_year_prefix(raw.get("Year")),
                type=_clean_str(raw.get("Type")) or "",
                poster_url=_clean_str(raw.get("Poster")),
            ))
        return hits


# Singleton
omdb_service = OMDbService()
