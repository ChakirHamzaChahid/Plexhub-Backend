"""S5.4 (AUDIT-P8-002) — `plexhub_omdb_requests_total{result}` and
`plexhub_plex_sync_total{result}` wiring.

`app/utils/metrics.py` (S1.4) already DECLARES + zero-initialises both
counters' closed label enumerations (see `tests/test_metrics_registry.py`,
`TestNewlyDeclaredMetricsSurface::test_omdb_requests_total_zero_init_per_result`
/ `test_plex_sync_total_zero_init_per_result`). This module tests the actual
CALL SITES wired by S5.4 in `app.services.omdb_service` and
`app.services.plex_sync_service` — one test per label value, plus the
non-negotiable secret-leak guards (OMDb `apikey`, Plex `access_token` /
`base_uri` must never reach a metric, a log line, or an exception message).

See `docs/plans/2026-07-26-refacto-audit-v1-plan.md` §VAGUE 5 S5.4.
"""
from __future__ import annotations

import httpx
import pytest_asyncio
from prometheus_client import REGISTRY, generate_latest

from app.config import settings
from app.services.omdb_service import OMDbService
from app.services.plex_api_service import PlexApiService
from app.services import plex_sync_service as svc_mod
from app.utils import metrics

# pytest-asyncio auto mode (pyproject.toml) treats every `async def test_*`
# as an asyncio test with no marker needed.


def _registry_body() -> str:
    return generate_latest(REGISTRY).decode("utf-8")


def _omdb_count(result: str) -> float:
    return metrics.omdb_requests_total.labels(result=result)._value.get()


def _plex_count(result: str) -> float:
    return metrics.plex_sync_total.labels(result=result)._value.get()


def _payload(**overrides):
    base = {
        "Response": "True",
        "Title": "The Matrix",
        "Year": "1999",
        "Runtime": "136 min",
        "Genre": "Action, Sci-Fi",
        "Director": "Lana Wachowski, Lilly Wachowski",
        "Actors": "Keanu Reeves, Laurence Fishburne",
        "Plot": "A computer hacker learns the truth about reality.",
        "imdbRating": "8.7",
        "imdbVotes": "2,000,000",
        "Type": "movie",
        "imdbID": "tt0133093",
    }
    base.update(overrides)
    return base


# ─── OMDb: `configured_omdb`/`omdb_mock` fixtures come from tests/conftest.py ──


@pytest_asyncio.fixture
async def configured_omdb(monkeypatch):
    """Mirrors `test_omdb_service.py`'s fixture of the same name (kept local
    here rather than shared, same convention as that module)."""
    from app.services import omdb_service as mod

    monkeypatch.setattr(mod.settings, "OMDB_API_KEY", "test_key")
    svc = OMDbService()
    try:
        yield svc
    finally:
        await svc.close()


class TestOmdbGetByImdbIdMetrics:
    async def test_ok_increments_ok(self, configured_omdb, omdb_mock):
        omdb_mock.get("/").respond(200, json=_payload())
        before = _omdb_count("ok")
        data = await configured_omdb.get_by_imdb_id("tt0133093")
        assert data is not None
        assert _omdb_count("ok") == before + 1

    async def test_not_found_increments_not_found(self, configured_omdb, omdb_mock):
        omdb_mock.get("/").respond(200, json={"Response": "False", "Error": "Incorrect IMDb ID."})
        before = _omdb_count("not_found")
        data = await configured_omdb.get_by_imdb_id("tt0000000")
        assert data is None
        assert _omdb_count("not_found") == before + 1

    async def test_hard_error_increments_error(self, configured_omdb, omdb_mock):
        omdb_mock.get("/").respond(404)
        before = _omdb_count("error")
        data = await configured_omdb.get_by_imdb_id("tt0133093")
        assert data is None
        assert _omdb_count("error") == before + 1

    async def test_persistent_429_increments_rate_limited_not_error(
        self, configured_omdb, omdb_mock, monkeypatch,
    ):
        """A 429 that survives every retry must land on `rate_limited`, never
        on the generic `error` bucket — the two need to stay distinguishable
        (a real quota/backoff signal vs. an actual transport/HTTP failure)."""
        from app.services import omdb_service as mod

        async def _no_sleep(_):
            return None

        monkeypatch.setattr(mod.asyncio, "sleep", _no_sleep)
        route = omdb_mock.get("/")
        route.side_effect = [httpx.Response(429, headers={"Retry-After": "1"})] * 4

        before_rl = _omdb_count("rate_limited")
        before_err = _omdb_count("error")
        data = await configured_omdb.get_by_imdb_id("tt0133093")

        assert data is None
        assert _omdb_count("rate_limited") == before_rl + 1
        assert _omdb_count("error") == before_err

    async def test_budget_exhausted_short_circuits_and_increments(
        self, configured_omdb, omdb_mock, monkeypatch,
    ):
        """The most important value: `budget_exhausted` is the ONLY signal
        that OMDb's fail-open `OMDB_DAILY_LIMIT` policy is silently degrading
        enrichment. Must fire WITHOUT making any HTTP call, and must never be
        confused with `error`."""
        from app.services import omdb_service as mod

        monkeypatch.setattr(mod.settings, "OMDB_DAILY_LIMIT", 0)
        route = omdb_mock.get("/").respond(200, json=_payload())

        before_be = _omdb_count("budget_exhausted")
        before_err = _omdb_count("error")
        before_ok = _omdb_count("ok")

        data = await configured_omdb.get_by_imdb_id("tt0133093")

        assert data is None
        assert route.call_count == 0, "budget_exhausted must short-circuit before any HTTP call"
        assert _omdb_count("budget_exhausted") == before_be + 1
        assert _omdb_count("error") == before_err
        assert _omdb_count("ok") == before_ok

    async def test_budget_not_yet_exhausted_still_calls_through(
        self, configured_omdb, omdb_mock, monkeypatch,
    ):
        """Sanity counterpart: a budget that has room left must not trip the
        guard (no false positive on `budget_exhausted`)."""
        from app.services import omdb_service as mod

        monkeypatch.setattr(mod.settings, "OMDB_DAILY_LIMIT", 100)
        route = omdb_mock.get("/").respond(200, json=_payload())

        before_be = _omdb_count("budget_exhausted")
        data = await configured_omdb.get_by_imdb_id("tt0133093")

        assert data is not None
        assert route.call_count == 1
        assert _omdb_count("budget_exhausted") == before_be


class TestOmdbSearchByTitleMetrics:
    """Same instrumentation as `get_by_imdb_id`, spot-checked on this second
    call site so both are provably wired (not just one of the two)."""

    async def test_ok_increments_ok(self, configured_omdb, omdb_mock):
        omdb_mock.get("/").respond(200, json=_payload())
        before = _omdb_count("ok")
        data = await configured_omdb.search_by_title("The Matrix", 1999, "movie")
        assert data is not None
        assert _omdb_count("ok") == before + 1

    async def test_not_found_increments_not_found(self, configured_omdb, omdb_mock):
        omdb_mock.get("/").respond(200, json={"Response": "False", "Error": "Movie not found!"})
        before = _omdb_count("not_found")
        data = await configured_omdb.search_by_title("Some Obscure Title", 2020, "movie")
        assert data is None
        assert _omdb_count("not_found") == before + 1

    async def test_budget_exhausted_short_circuits(self, configured_omdb, omdb_mock, monkeypatch):
        from app.services import omdb_service as mod

        monkeypatch.setattr(mod.settings, "OMDB_DAILY_LIMIT", 0)
        route = omdb_mock.get("/").respond(200, json=_payload())
        before = _omdb_count("budget_exhausted")

        data = await configured_omdb.search_by_title("The Matrix", 1999, "movie")

        assert data is None
        assert route.call_count == 0
        assert _omdb_count("budget_exhausted") == before + 1


class TestOmdbKeyNeverLeaksViaMetrics:
    async def test_error_path_metrics_and_registry_never_contain_api_key(
        self, configured_omdb, omdb_mock,
    ):
        """The 401-invalid-key response body itself never carries the key
        (it's a query param, not an echoed value) but the REQUEST url did —
        `httpx.HTTPStatusError.__str__` embeds it. Assert the key/`apikey=`
        marker is absent from BOTH the metric label space (closed enum, so
        trivially true) and the full `/metrics` scrape (defence in depth)."""
        omdb_mock.get("/").respond(401, json={"Response": "False", "Error": "Invalid API key!"})
        before = _omdb_count("error")

        data = await configured_omdb.get_by_imdb_id("tt0133093")

        assert data is None
        assert _omdb_count("error") == before + 1
        body = _registry_body()
        assert "test_key" not in body
        assert "apikey" not in body

    async def test_exception_object_never_stringified_into_a_metric_label(
        self, configured_omdb, omdb_mock,
    ):
        """Belt-and-suspenders: the label passed to `.labels(result=...)` is
        always one of the 5 closed enum values — never `str(exc)` — so even a
        future refactor accidentally doing `result=str(exc)` would be caught
        by scraping the registry for the literal `https://www.omdbapi.com`
        upstream host (which `str(exc)` would embed via the request URL)."""
        omdb_mock.get("/").respond(500)
        data = await configured_omdb.get_by_imdb_id("tt0133093")
        assert data is None
        body = _registry_body()
        assert "omdbapi.com" not in body


# ─── Plex sync ────────────────────────────────────────────────────────────

RESOURCES_URL = "https://plex.tv/api/v2/resources"
PMS = "http://192.168.1.50:32400"


@pytest_asyncio.fixture
async def sync_env(monkeypatch, db_factory):
    """Same fixture shape as `tests/test_plex_sync.py::sync_env` (kept local
    to this module, not shared, matching that file's own convention)."""
    monkeypatch.setattr(settings, "PLEX_ACCOUNT_TOKEN", "acct-secret-token")  # noqa: S105
    monkeypatch.setattr(settings, "PLEX_CLIENT_IDENTIFIER", "backend-client-id")
    monkeypatch.setattr(settings, "PLEX_PROBE_TIMEOUT", 5)
    fresh_api = PlexApiService()
    monkeypatch.setattr(svc_mod, "plex_api_service", fresh_api)
    try:
        yield db_factory
    finally:
        await fresh_api.close()


def _resource(client_id="cid-1"):
    return {
        "name": "My Server",
        "clientIdentifier": client_id,
        "owned": True,
        "sourceTitle": None,
        "accessToken": "srv-secret-token",  # noqa: S105
        "provides": "server",
        "connections": [{"protocol": "http", "address": "192.168.1.50", "port": 32400,
                          "uri": PMS, "local": True, "relay": False}],
    }


class TestPlexSyncMetrics:
    async def test_ok_increments_ok(self, sync_env, xtream_mock):
        xtream_mock.get(RESOURCES_URL).respond(200, json=[_resource()])
        xtream_mock.get(f"{PMS}/library/sections").respond(
            200, json={"MediaContainer": {"Directory": []}},
        )
        before = _plex_count("ok")

        report = await svc_mod.run_full_sync(sync_env)

        assert report.status == "ok"
        assert _plex_count("ok") == before + 1

    async def test_disabled_increments_disabled(self, db_factory, monkeypatch):
        monkeypatch.setattr(settings, "PLEX_ACCOUNT_TOKEN", "")
        before = _plex_count("disabled")

        report = await svc_mod.run_full_sync(db_factory)

        assert report.status == "disabled"
        assert _plex_count("disabled") == before + 1

    async def test_already_running_increments_already_running_not_error(self, sync_env):
        """Claim-based design: a concurrent caller finding the claim already
        held is NORMAL, not a failure — must land on its own label, never on
        `error`."""
        assert await svc_mod._claim_sync(sync_env) is True
        before_ar = _plex_count("already_running")
        before_err = _plex_count("error")

        report = await svc_mod.run_full_sync(sync_env)

        assert report.status == "already_running"
        assert _plex_count("already_running") == before_ar + 1
        assert _plex_count("error") == before_err

    async def test_error_increments_error(self, sync_env, xtream_mock, monkeypatch):
        from app.services import plex_api_service as api_mod

        async def _no_sleep(_):
            return None

        monkeypatch.setattr(api_mod.asyncio, "sleep", _no_sleep)
        xtream_mock.get(RESOURCES_URL).respond(500)
        before = _plex_count("error")

        report = await svc_mod.run_full_sync(sync_env)

        assert report.status == "error"
        assert _plex_count("error") == before + 1


class TestPlexSyncMetricsNeverLeakSecrets:
    async def test_registry_never_contains_account_token_or_server_token_or_base_uri(
        self, sync_env, xtream_mock, monkeypatch,
    ):
        """`plexhub_plex_sync_total` only ever carries the closed `result`
        label (never a token/server identifier), but this is a full-scrape
        defence-in-depth check that also covers the error path, where a
        careless future change could otherwise leak `_safe_error(exc)` text
        into a label."""
        from app.services import plex_api_service as api_mod

        async def _no_sleep(_):
            return None

        monkeypatch.setattr(api_mod.asyncio, "sleep", _no_sleep)
        xtream_mock.get(RESOURCES_URL).respond(200, json=[_resource()])
        xtream_mock.get(f"{PMS}/library/sections").respond(500)

        report = await svc_mod.run_full_sync(sync_env)

        assert report.status == "ok"  # an unreachable/failing single server isn't a run-level error
        body = _registry_body()
        assert settings.PLEX_ACCOUNT_TOKEN not in body
        assert "srv-secret-token" not in body
        assert PMS not in body
