"""Prometheus metrics — HTTP instrumentation + business counters/gauges.

Exposed at GET /metrics. The instrumentator wraps every request automatically;
business metrics below are incremented from the workers/services/routers.

Audit v1 remediation (AUDIT-P8-001, AUDIT-P8-002 — see
`docs/audit/v1/60-release-observability.md` and
`docs/plans/2026-07-26-refacto-audit-v1-plan.md` §VAGUE 1 S1.4):

1. AUDIT-P8-001 — labelled metrics with no zero-init have no series at all
   until the first event fires, so `rate(...) == 0` (pipeline dead) is
   indistinguishable from "no data" (metric never touched). Every metric
   below whose label set is a KNOWN, CLOSED enumeration is zero-initialised
   at import time (`.labels(...)` called for every combination, or the bare
   metric constructed with no labels — either way a series exists at `0`
   before any business event happens).

   Labels with OPEN cardinality (`account_id`, `rating_key`, `server_id`,
   any Xtream/Plex identifier) are deliberately left UN-initialised — and, in
   the new metrics below, avoided entirely as label dimensions. Blowing up
   cardinality to fix an absence-of-data problem would trade one operational
   blind spot for a worse one. This is also why the S5.1 freshness gauges
   (`*_last_success_timestamp_seconds`) are keyed by a short closed list of
   job names, never by instance/account.

2. AUDIT-P8-002 — only sync/enrichment/health/tmdb call sites emit metrics
   today; downloads, the DAV relay, OMDb and Plex catalogue sync are
   completely invisible. This module DECLARES the full metric surface for
   all of those areas in one place (V-3 in the plan, §1.2) so that later
   steps (S2.4, S2.6, S5.1, S5.2, S5.3, S5.4) only need to *import and
   increment* from their own module — `metrics.py` stops being a shared
   hotspot for the rest of the remediation lot.

   This file only DECLARES those new metrics and zero-initialises their
   closed label sets. **No call site for them is wired here** — that is the
   explicit responsibility of the steps listed above; wiring them here would
   defeat the whole point of the decoupling.

No label anywhere on this page carries a secret, a URL, a title, a
rating_key or a full server_id (piège §9-10) — several of the metrics below
guard exactly that (downloads, DAV relay, OMDb).
"""
from __future__ import annotations

import itertools

from prometheus_client import Counter, Gauge, Histogram
from prometheus_fastapi_instrumentator import Instrumentator


# ─── Business metrics (pre-existing) ─────────────────────────────────────

sync_duration_seconds = Histogram(
    "plexhub_sync_duration_seconds",
    "Wall time of a per-account Xtream sync.",
    labelnames=("account_id", "result"),  # result: success | error
    buckets=(1, 5, 15, 30, 60, 120, 300, 600, 1800, 3600, 7200),
)
# NOTE: `account_id` is open cardinality -> deliberately NOT zero-initialised.

tmdb_requests_total = Counter(
    "plexhub_tmdb_requests_total",
    "TMDB HTTP requests, partitioned by outcome.",
    labelnames=("kind", "result"),  # kind: search_movie|search_tv|details|other, result: ok|error|rate_limited
)

tmdb_match_total = Counter(
    "plexhub_tmdb_match_total",
    "TMDB enrichment outcomes, to track auto-match rate before/after.",
    labelnames=("media_type", "result"),  # result: matched | nomatch | ambiguous
)

streams_alive_ratio = Gauge(
    "plexhub_streams_alive_ratio",
    "Ratio (0-1) of non-broken streams per account, computed at health check end.",
    labelnames=("account_id",),
)
# NOTE: `account_id` is open cardinality -> deliberately NOT zero-initialised.

enrichment_queue_size = Gauge(
    "plexhub_enrichment_queue_size",
    "Pending/skipped/failed item count in enrichment_queue.",
    labelnames=("status",),  # status: pending|processing|done|failed|skipped (models/database.py EnrichmentQueue.status)
)

# Closed label enumerations for the 3 pre-existing metrics above whose full
# value set is known and bounded (AUDIT-P8-001). `.labels(...)` alone (no
# `.inc()`/`.set()`) is enough to make prometheus_client materialise the
# series at 0.
_TMDB_REQUEST_KINDS = ("search_movie", "search_tv", "details", "other")
_TMDB_REQUEST_RESULTS = ("ok", "error", "rate_limited")
for _kind, _result in itertools.product(_TMDB_REQUEST_KINDS, _TMDB_REQUEST_RESULTS):
    tmdb_requests_total.labels(kind=_kind, result=_result)

_TMDB_MATCH_MEDIA_TYPES = ("movie", "tv")
_TMDB_MATCH_RESULTS = ("matched", "nomatch", "ambiguous")
for _media_type, _match_result in itertools.product(_TMDB_MATCH_MEDIA_TYPES, _TMDB_MATCH_RESULTS):
    tmdb_match_total.labels(media_type=_media_type, result=_match_result)

_ENRICHMENT_QUEUE_STATUSES = ("pending", "processing", "done", "failed", "skipped")
for _status in _ENRICHMENT_QUEUE_STATUSES:
    enrichment_queue_size.labels(status=_status)


# ─── Audit v1 remediation — declared surface, no call sites yet ─────────
#
# Everything below is consumed by later steps of the same remediation lot
# (see module docstring). Declared here, zero-initialised where the label
# set is closed, and left at 0 until the owning step wires an increment.

# S2.4 (AUDIT-P3-002 volet visibilité) — distinguishes the precomputed
# `media_group` snapshot path from the live in-memory aggregation fallback
# in `media_service.get_unified_list`. `live_fallback` (snapshot missing/
# empty) is the anomalous case; `live_filtered` (search/genre/year query)
# is expected and normal.
unified_path_total = Counter(
    "plexhub_unified_path_total",
    "Which code path served a GET /api/media/{movies,shows}/unified request.",
    labelnames=("path",),  # path: snapshot | live_filtered | live_fallback
)
_UNIFIED_PATHS = ("snapshot", "live_filtered", "live_fallback")
for _path in _UNIFIED_PATHS:
    unified_path_total.labels(path=_path)

# S2.6 (AUDIT-P5-004) — counts requests to GET /api/media/episodes rejected
# with 400 for missing the now-mandatory `server_id`. Unlabelled: a single
# counter is enough to tell whether any legacy client is still hitting the
# old contract; it needs no per-caller dimension.
media_episodes_missing_server_id_total = Counter(
    "plexhub_media_episodes_missing_server_id_total",
    "GET /api/media/episodes requests rejected with 400 for a missing server_id.",
)

# S5.1 (AUDIT-P8-001 volet b / P8-005) — liveness of the scheduled jobs.
# `job` is a short, closed, hand-maintained list of pipeline job names (NOT
# an account/instance dimension) so alerting can do
# `time() - plexhub_pipeline_last_success_timestamp_seconds{job="..."}`
# per job from boot, without waiting for a first success.
pipeline_last_success_timestamp_seconds = Gauge(
    "plexhub_pipeline_last_success_timestamp_seconds",
    "Unix timestamp (seconds) of the last successful run of a scheduled job.",
    labelnames=("job",),
)
_PIPELINE_JOB_NAMES = (
    "pipeline",  # scheduled_sync_enrich_generate (sync+enrichment+validation+generation+snapshot)
    "health_check",
    "epg_cleanup",
    "subtitle_cache_cleanup",
    "db_backup",
    "plex_catalogue_sync",
)
for _job in _PIPELINE_JOB_NAMES:
    pipeline_last_success_timestamp_seconds.labels(job=_job)

# S5.1 — whether this process currently holds the fcntl master election
# (piège §9-7: POSIX-only, always 0/false under an un-shimmed Windows dev
# run). Single value, no labels: defaults to 0 until the lifespan sets it.
is_master = Gauge(
    "plexhub_is_master",
    "1 if this process holds the master election lock (fcntl.flock), else 0.",
)

# S5.2 (AUDIT-P8-002 / F-103) — the physical-download subsystem ("Télécharger"
# + "Télécharger Plex", shared `download_job` table/worker) is today totally
# invisible to Prometheus.
download_jobs = Gauge(
    "plexhub_download_jobs",
    "Number of download_job rows per state (queue depth), refreshed each drain tick.",
    labelnames=("state",),  # state: queued|running|completed|failed|canceled (DownloadJob.state)
)
_DOWNLOAD_JOB_STATES = ("queued", "running", "completed", "failed", "canceled")
for _state in _DOWNLOAD_JOB_STATES:
    download_jobs.labels(state=_state)

download_bytes_total = Counter(
    "plexhub_download_bytes_total",
    "Total bytes written to disk by the download worker (Xtream + Plex jobs).",
)

download_failures_total = Counter(
    "plexhub_download_failures_total",
    "Download job failures, partitioned by reason. Never labelled with a URL, "
    "rating_key, filename or server_id (piège 17c).",
    labelnames=("reason",),  # reason: http_403 | disk_full | timeout | confinement
)
_DOWNLOAD_FAILURE_REASONS = ("http_403", "disk_full", "timeout", "confinement")
for _reason in _DOWNLOAD_FAILURE_REASONS:
    download_failures_total.labels(reason=_reason)

# S5.3 (AUDIT-P8-002) — DAV relay (`app/dav/{throttle,relay}.py`). All three
# are unlabelled by design: the per-account throttle state is process-local
# (piège 18c) and must never carry the upstream URL (piège 18f).
dav_throttle_rejections_total = Counter(
    "plexhub_dav_throttle_rejections_total",
    "503 rejections from the per-account DAV upstream throttle (queue timeout).",
)

dav_permit_wait_seconds = Histogram(
    "plexhub_dav_permit_wait_seconds",
    "Time spent waiting for a per-account DAV upstream permit before relaying.",
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10),
)

dav_relayed_bytes_total = Counter(
    "plexhub_dav_relayed_bytes_total",
    "Total bytes relayed to WebDAV clients (e.g. rclone) by the /dav GET handler.",
)

# S5.4 (AUDIT-P8-002) — OMDb budget consumption (fail-open: exhausting
# OMDB_DAILY_LIMIT degrades enrichment silently today) and Plex catalogue
# sync outcomes.
omdb_requests_total = Counter(
    "plexhub_omdb_requests_total",
    "OMDb HTTP requests, partitioned by outcome.",
    labelnames=("result",),  # result: ok | not_found | error | rate_limited | budget_exhausted
)
_OMDB_RESULTS = ("ok", "not_found", "error", "rate_limited", "budget_exhausted")
for _omdb_result in _OMDB_RESULTS:
    omdb_requests_total.labels(result=_omdb_result)

plex_sync_total = Counter(
    "plexhub_plex_sync_total",
    "Plex catalogue sync (plex_sync_service.run_full_sync) outcomes.",
    labelnames=("result",),  # result: ok | disabled | already_running | error (PlexSyncReport.status)
)
_PLEX_SYNC_RESULTS = ("ok", "disabled", "already_running", "error")
for _plex_sync_result in _PLEX_SYNC_RESULTS:
    plex_sync_total.labels(result=_plex_sync_result)


def setup_instrumentator(app) -> None:
    """Register the Prometheus instrumentator on the FastAPI app and expose /metrics."""
    Instrumentator(
        excluded_handlers=["/metrics"],
        should_group_status_codes=True,
    ).instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)
