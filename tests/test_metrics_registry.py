"""Guards AUDIT-P8-001: enumerable-label business metrics must expose every
known series at 0 as soon as the module is imported (i.e. from process boot),
not only after the first business event.

See `docs/plans/2026-07-26-refacto-audit-v1-plan.md` §VAGUE 1 S1.4 and
`app/utils/metrics.py` module docstring.
"""
from __future__ import annotations

import itertools
import pathlib
import subprocess
import sys

from prometheus_client import REGISTRY, generate_latest

from app.utils import metrics


def _scrape() -> str:
    return generate_latest(REGISTRY).decode("utf-8")


def _assert_series_present(body: str, metric: str, labels: dict[str, str]) -> None:
    label_str = ",".join(f'{k}="{v}"' for k, v in labels.items())
    prefix = f"{metric}{{{label_str}}} " if labels else f"{metric} "
    assert prefix in body, f"expected zero-init series missing: {prefix!r}"


def _assert_no_series(body: str, metric: str) -> None:
    # Any line starting with the bare metric name followed by `{` or a space
    # would be an actual series. `# HELP`/`# TYPE` header lines don't count.
    for line in body.splitlines():
        if line.startswith("#"):
            continue
        if line.startswith(metric + "{") or line.startswith(metric + " "):
            raise AssertionError(
                f"metric {metric!r} has an un-expected series before any event: {line!r}"
            )


class TestPreExistingMetricsZeroInit:
    """The 3 pre-existing metrics whose label set is closed (AUDIT-P8-001)."""

    def test_tmdb_requests_total_all_combinations_at_zero(self):
        body = _scrape()
        for kind, result in itertools.product(
            metrics._TMDB_REQUEST_KINDS, metrics._TMDB_REQUEST_RESULTS
        ):
            _assert_series_present(
                body, "plexhub_tmdb_requests_total", {"kind": kind, "result": result}
            )

    def test_tmdb_match_total_all_combinations_at_zero(self):
        body = _scrape()
        for media_type, result in itertools.product(
            metrics._TMDB_MATCH_MEDIA_TYPES, metrics._TMDB_MATCH_RESULTS
        ):
            _assert_series_present(
                body, "plexhub_tmdb_match_total", {"media_type": media_type, "result": result}
            )

    def test_enrichment_queue_size_all_statuses_at_zero(self):
        body = _scrape()
        for status in metrics._ENRICHMENT_QUEUE_STATUSES:
            _assert_series_present(body, "plexhub_enrichment_queue_size", {"status": status})


class TestOpenCardinalityMetricsStayUnlabelled:
    """`account_id` is open cardinality: these must NOT be zero-initialised
    (that would be an unbounded blow-up, not a fix) — see piège §9-10.

    These assertions are about the state of the registry **at process boot**,
    i.e. purely as a consequence of importing `app.utils.metrics`. They cannot
    be checked against the ambient global REGISTRY of a full-suite run: any
    earlier test that legitimately exercises a health-check or a sync emits
    `.labels(account_id=...)` and creates a series, which made this assertion
    pass alone and fail in-suite (order-dependent, not a real regression).
    So we scrape a FRESH interpreter — that is exactly the property claimed.
    """

    @staticmethod
    def _boot_scrape() -> str:
        """Import the metrics module in a clean process and return its scrape."""
        code = (
            "import app.utils.metrics\n"
            "from prometheus_client import REGISTRY, generate_latest\n"
            "import sys; sys.stdout.write(generate_latest(REGISTRY).decode('utf-8'))\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(pathlib.Path(__file__).resolve().parent.parent),
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert proc.returncode == 0, f"boot scrape failed: {proc.stderr}"
        return proc.stdout

    def test_sync_duration_seconds_has_no_series_at_boot(self):
        _assert_no_series(self._boot_scrape(), "plexhub_sync_duration_seconds_bucket")

    def test_streams_alive_ratio_has_no_series_at_boot(self):
        _assert_no_series(self._boot_scrape(), "plexhub_streams_alive_ratio")


class TestNewlyDeclaredMetricsSurface:
    """S1.4 declares the full metric surface for the remainder of the audit
    v1 remediation lot (V-3, plan §1.2). No call site is wired yet — this
    only asserts the declaration + zero-init contract."""

    def test_unified_path_total_zero_init(self):
        body = _scrape()
        for path in metrics._UNIFIED_PATHS:
            _assert_series_present(body, "plexhub_unified_path_total", {"path": path})

    def test_media_episodes_missing_server_id_total_declared(self):
        _assert_series_present(_scrape(), "plexhub_media_episodes_missing_server_id_total", {})

    def test_pipeline_last_success_timestamp_zero_init_per_job(self):
        body = _scrape()
        for job in metrics._PIPELINE_JOB_NAMES:
            _assert_series_present(
                body, "plexhub_pipeline_last_success_timestamp_seconds", {"job": job}
            )

    def test_is_master_declared_unlabelled(self):
        _assert_series_present(_scrape(), "plexhub_is_master", {})

    def test_download_jobs_zero_init_per_state(self):
        body = _scrape()
        for state in metrics._DOWNLOAD_JOB_STATES:
            _assert_series_present(body, "plexhub_download_jobs", {"state": state})

    def test_download_bytes_total_declared_unlabelled(self):
        _assert_series_present(_scrape(), "plexhub_download_bytes_total", {})

    def test_download_failures_total_zero_init_per_reason(self):
        body = _scrape()
        for reason in metrics._DOWNLOAD_FAILURE_REASONS:
            _assert_series_present(body, "plexhub_download_failures_total", {"reason": reason})

    def test_dav_metrics_declared_unlabelled(self):
        body = _scrape()
        _assert_series_present(body, "plexhub_dav_throttle_rejections_total", {})
        _assert_series_present(body, "plexhub_dav_relayed_bytes_total", {})
        # Histogram: the _count series is the cheapest reliable existence check.
        _assert_series_present(body, "plexhub_dav_permit_wait_seconds_count", {})

    def test_omdb_requests_total_zero_init_per_result(self):
        body = _scrape()
        for result in metrics._OMDB_RESULTS:
            _assert_series_present(body, "plexhub_omdb_requests_total", {"result": result})

    def test_plex_sync_total_zero_init_per_result(self):
        body = _scrape()
        for result in metrics._PLEX_SYNC_RESULTS:
            _assert_series_present(body, "plexhub_plex_sync_total", {"result": result})


def test_fresh_instance_metrics_endpoint_is_not_just_help_and_type_lines():
    """DoD (S1.4): `curl /metrics` on a fresh instance returns real `0`
    series, not only `# HELP`/`# TYPE` header lines (AUDIT-P8-001)."""
    body = _scrape()
    data_lines = [
        line for line in body.splitlines()
        if line.startswith("plexhub_") and not line.startswith("#")
    ]
    assert len(data_lines) > 0
