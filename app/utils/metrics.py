"""Prometheus metrics — HTTP instrumentation + business counters/gauges.

Exposed at GET /metrics. The instrumentator wraps every request automatically;
business metrics below are incremented from the workers.
"""
from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram
from prometheus_fastapi_instrumentator import Instrumentator


# ─── Business metrics ────────────────────────────────────────────────────

sync_duration_seconds = Histogram(
    "plexhub_sync_duration_seconds",
    "Wall time of a per-account Xtream sync.",
    labelnames=("account_id", "result"),  # result: success | error
    buckets=(1, 5, 15, 30, 60, 120, 300, 600, 1800, 3600, 7200),
)

tmdb_requests_total = Counter(
    "plexhub_tmdb_requests_total",
    "TMDB HTTP requests, partitioned by outcome.",
    labelnames=("kind", "result"),  # kind: search_movie|search_tv|details, result: ok|miss|error|rate_limited
)

streams_alive_ratio = Gauge(
    "plexhub_streams_alive_ratio",
    "Ratio (0-1) of non-broken streams per account, computed at health check end.",
    labelnames=("account_id",),
)

enrichment_queue_size = Gauge(
    "plexhub_enrichment_queue_size",
    "Pending/skipped/failed item count in enrichment_queue.",
    labelnames=("status",),
)


def setup_instrumentator(app) -> None:
    """Register the Prometheus instrumentator on the FastAPI app and expose /metrics."""
    Instrumentator(
        excluded_handlers=["/metrics"],
        should_group_status_codes=True,
    ).instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)
