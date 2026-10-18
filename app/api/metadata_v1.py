"""Metadata edit API `/api/metadata/v1` (lot B2).

Read, simulate, write, journal and revert the metadata of `media` items for an
external client (plexhubTMM). Contract `16-contrat-b2-backend.md`; PRD
`docs/10-prd-metadata-api.md`; ADR 0010 (scopes), 0011 (write path), 0012 (lock).

Mounting (Pattern C, same as `scrape_v1`): self-prefixed router mounted bare in
`app/main.py` WITHOUT a router-level dependency. Every route declares its own
guard — `verify_media_read_scope` on reads AND on every simulation (`…/preview`,
a distinct route: a guard reading a query flag could diverge from the route's own
reading of it), `verify_media_write_scope` ALONE on the three routes that validate a
write (`media.write` does not include `media.read`) — followed by the per-key rate
limits, declared AFTER the guard so `request.state.principal` is already set.
`route_audit` does not tell the two guards apart: `tests/test_metadata_v1_routes.py`
checks the expected guard route by route.

Validation + delegation only (CLAUDE.md § 2): decisions live in
`app/services/metadata_service.py` and the pure `metadata_fields`/`metadata_plan`.
Errors are `{"detail": {"code", "message", …}}` like the scrape gateway; type and
bound errors of the declared parameters are FastAPI's native 422.
"""
from __future__ import annotations

import logging
import math
import re
import time
from collections.abc import Awaitable
from typing import Any, Literal

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Path, Query, Request, Response, status

from app.api.deps import Principal, verify_media_read_scope, verify_media_write_scope
from app.db import database as db_module
from app.db.immediate_tx import DatabaseBusy
from app.models import metadata_schemas as S
from app.services import metadata_limits
from app.services import metadata_service as svc
from app.services.metadata_columns import FIELDS_BY_TYPE, LIMITS
from app.services.metadata_fields import FieldError
from app.services.metadata_views import item_summary, item_view
from app.utils import rate_limit
from app.utils.request_context import request_id_var

logger = logging.getLogger("plexhub.api.metadata_v1")

PREFIX = "/api/metadata/v1"
RATE_LIMIT_WINDOW_SECONDS = 60.0
BUSY_RETRY_AFTER_SECONDS = 10

router = APIRouter(prefix=PREFIX, tags=["metadata"])

_REVISION_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_IMDB_RE = re.compile(r"^tt\d{7,10}$")
_TMDB_RE = re.compile(r"^\d{1,9}$")
_TVDB_RE = re.compile(r"^\d{1,12}$")


# ─── Errors ──────────────────────────────────────────────────────────────────


def _error(
    status_code: int, code: str, message: str, *, retry_after: int | None = None, **extra: Any
) -> HTTPException:
    detail: dict[str, Any] = {"code": code, "message": message, **extra}
    headers = None
    if retry_after is not None:
        detail["retryAfterSeconds"] = retry_after
        headers = {"Retry-After": str(retry_after)}
    return HTTPException(status_code=status_code, detail=detail, headers=headers)


def _map_error(exc: Exception) -> HTTPException | None:
    if isinstance(exc, svc.ItemNotFound):
        return _error(404, "item_not_found", "Item not found")
    if isinstance(exc, svc.EditNotFound):
        return _error(404, "edit_not_found", "Edit not found")
    if isinstance(exc, FieldError):
        return _error(400, "invalid_field", exc.message, field=exc.field)
    if isinstance(exc, svc.InvalidParameter):
        return _error(400, "invalid_parameter", exc.message)
    if isinstance(exc, svc.RevisionMismatch):
        return _error(412, "revision_mismatch", "The item changed since it was read",
                      currentRevision=exc.current)
    if isinstance(exc, svc.IdempotencyKeyReused):
        return _error(409, "idempotency_key_reused", "Idempotency-Key already used with another request")
    if isinstance(exc, svc.RegroupNotAllowed):
        return _error(
            409, "regroup_not_allowed", "The write would change the unification group",
            unificationIdBefore=exc.before, unificationIdAfter=exc.after,
            membersBefore=exc.members_before, membersAfter=exc.members_after,
        )
    if isinstance(exc, svc.RevertNotAllowed):
        return _error(409, "revert_conflict", "The edit cannot be reverted as is", fields=exc.fields)
    if isinstance(exc, DatabaseBusy):
        return _error(503, "database_busy", "The database is busy, nothing was written",
                      retry_after=BUSY_RETRY_AFTER_SECONDS)
    return None


_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    code: {"model": S.ErrorResponse} for code in (400, 401, 403, 404, 409, 412, 428, 429, 503)
}


# ─── Guards and per-key rate limits ──────────────────────────────────────────


def _principal(request: Request) -> Principal:
    principal = getattr(request.state, "principal", None)
    if not isinstance(principal, Principal):
        raise _error(401, "invalid_api_key", "Missing, unknown, revoked or expired API key")
    return principal


def _hit(limiter: rate_limit.SlidingWindowLimiter, request: Request, limit: int) -> None:
    principal = _principal(request)
    if limit <= 0:
        return
    bucket = "master" if principal.is_master else principal.key_id
    if not bucket:
        raise _error(401, "invalid_api_key", "Missing, unknown, revoked or expired API key")
    try:
        limiter.hit(bucket, max_events=limit, window_seconds=RATE_LIMIT_WINDOW_SECONDS)
    except rate_limit.RateLimitExceeded as exc:
        raise _error(429, "rate_limited", "Too many requests for this API key",
                     retry_after=max(1, math.ceil(exc.retry_after_seconds))) from None


async def enforce_metadata_rate_limit(request: Request) -> None:
    """Every authenticated request (reads, simulations, writes): 60 s sliding
    window per key, `METADATA_RATE_LIMIT_PER_MINUTE` (`<= 0` disables). Refused
    BEFORE any processing: nothing is written on a 429."""
    _hit(rate_limit.metadata_rate_limiter, request, metadata_limits.rate_limit_per_minute())


async def enforce_metadata_write_rate_limit(request: Request) -> None:
    """The routes that validate a write only: `METADATA_WRITE_RATE_LIMIT_PER_MINUTE`."""
    _hit(rate_limit.metadata_write_rate_limiter, request, metadata_limits.write_rate_limit_per_minute())


_READ_GUARD = [Depends(verify_media_read_scope), Depends(enforce_metadata_rate_limit)]
_WRITE_GUARD = [
    Depends(verify_media_write_scope),
    Depends(enforce_metadata_rate_limit),
    Depends(enforce_metadata_write_rate_limit),
]

_SERVER_ID = Path(alias="serverId", min_length=1, max_length=300)
_RATING_KEY = Path(alias="ratingKey", min_length=1, max_length=300)
_EDIT_ID = Path(alias="editId", min_length=1, max_length=64)


# ─── Request helpers ─────────────────────────────────────────────────────────


def _if_match(value: str | None, *, required: bool) -> str | None:
    if value is None:
        if required:
            raise _error(428, "precondition_required", "If-Match is required", header="If-Match")
        return None
    cleaned = value.strip()
    if cleaned.startswith("W/"):
        raise _error(400, "invalid_parameter", "A weak validator (W/) is not accepted in If-Match")
    if len(cleaned) >= 2 and cleaned[0] == '"' and cleaned[-1] == '"':
        cleaned = cleaned[1:-1]
    if not _REVISION_RE.match(cleaned):
        raise _error(400, "invalid_parameter", "Malformed If-Match")
    return cleaned


def _idempotency_key(value: str | None) -> str:
    if value is None:
        raise _error(428, "precondition_required", "Idempotency-Key is required",
                     header="Idempotency-Key")
    if not _IDEMPOTENCY_RE.match(value):
        raise _error(400, "invalid_parameter", "Malformed Idempotency-Key")
    return value


def _request_id() -> str | None:
    rid = request_id_var.get()
    return None if rid == "-" else rid


def _etag(response: Response, revision: str) -> None:
    response.headers["ETag"] = f'"{revision}"'


async def _guarded(route: str, call: Awaitable[Any]) -> Any:
    """Await a service call, map its errors to the HTTP body, log the outcome
    (route, outcome, duration only — never a title, a key or a request body)."""
    started = time.monotonic()
    try:
        result = await call
    except HTTPException:
        raise
    except Exception as exc:
        mapped = _map_error(exc)
        if mapped is None:
            logger.warning("metadata api: route=%s outcome=error (%s)", route, type(exc).__name__)
            raise
        logger.info("metadata api: route=%s outcome=%s %.0fms", route, mapped.detail["code"],
                    (time.monotonic() - started) * 1000)
        raise mapped from None
    logger.info("metadata api: route=%s outcome=ok %.0fms", route, (time.monotonic() - started) * 1000)
    return result


# ─── Reads ───────────────────────────────────────────────────────────────────


@router.get("/lookup", response_model=S.LookupResponse, response_model_by_alias=True,
            response_model_exclude_unset=True, dependencies=_READ_GUARD, responses=_ERROR_RESPONSES)
async def lookup(
    type: Literal["movie", "show"] = Query(),
    imdb_id: str | None = Query(default=None, alias="imdbId"),
    tmdb_id: str | None = Query(default=None, alias="tmdbId"),
    tvdb_id: str | None = Query(default=None, alias="tvdbId"),
    unification_id: str | None = Query(default=None, alias="unificationId", max_length=300),
):
    """Find every item carrying EXACTLY ONE external id (or a unification id)."""

    async def _run():
        given = {
            k: v for k, v in (
                ("imdb", imdb_id), ("tmdb", tmdb_id), ("tvdb", tvdb_id), ("unification", unification_id),
            ) if v is not None
        }
        if len(given) != 1:
            raise svc.InvalidParameter("Provide exactly one of imdbId, tmdbId, tvdbId, unificationId")
        ((kind, value),) = given.items()
        regex = {"imdb": _IMDB_RE, "tmdb": _TMDB_RE, "tvdb": _TVDB_RE}.get(kind)
        if (regex is not None and not regex.match(value)) or not value:
            raise svc.InvalidParameter(f"Malformed {kind} id")
        async with db_module.async_session_factory() as session:
            items, truncated = await svc.lookup(session, type, kind, value)
        return {
            "schemaVersion": S.SCHEMA_VERSION,
            "items": [item_summary(i.variants, i.ctx, i.revision) for i in items],
            "truncated": truncated,
        }

    return await _guarded("lookup", _run())


@router.get("/items", response_model=S.ItemListResponse, response_model_by_alias=True,
            response_model_exclude_unset=True, dependencies=_READ_GUARD, responses=_ERROR_RESPONSES)
async def list_items(
    type: Literal["movie", "show"] = Query(),
    missing_ids: Literal["any", "imdb", "tmdb"] | None = Query(default=None, alias="missingIds"),
    server_id: str | None = Query(default=None, alias="serverId", max_length=300),
    limit: int = Query(default=100, ge=1, le=500),
    cursor: str | None = Query(default=None, max_length=1024),
):
    """Items to complete, e.g. those with no external id at all."""

    async def _run():
        async with db_module.async_session_factory() as session:
            items, next_cursor = await svc.list_items(session, type, missing_ids, server_id, limit, cursor)
        return {
            "schemaVersion": S.SCHEMA_VERSION,
            "items": [item_summary(i.variants, i.ctx, i.revision) for i in items],
            "nextCursor": next_cursor,
        }

    return await _guarded("items", _run())


@router.get("/items/{serverId}/{ratingKey}", response_model=S.ItemView, response_model_by_alias=True,
            response_model_exclude_unset=True, dependencies=_READ_GUARD, responses=_ERROR_RESPONSES)
async def get_item(response: Response, server_id: str = _SERVER_ID, rating_key: str = _RATING_KEY):
    async def _run():
        async with db_module.async_session_factory() as session:
            item = await svc.load_item(session, server_id, rating_key)
        if item is None:
            raise svc.ItemNotFound
        return item

    item = await _guarded("item", _run())
    _etag(response, item.revision)
    return item_view(item.variants, item.ctx, item.revision)


@router.get("/items/{serverId}/{ratingKey}/episodes", response_model=S.EpisodesResponse,
            response_model_by_alias=True, response_model_exclude_unset=True,
            dependencies=_READ_GUARD, responses=_ERROR_RESPONSES)
async def get_episodes(
    server_id: str = _SERVER_ID,
    rating_key: str = _RATING_KEY,
    season: int | None = Query(default=None, ge=0, le=65535),
    episode: int | None = Query(default=None, ge=1, le=65535),
):
    async def _run():
        if episode is not None and season is None:
            raise svc.InvalidParameter("episode requires season")
        async with db_module.async_session_factory() as session:
            items, truncated = await svc.list_episodes(session, server_id, rating_key, season, episode)
        return {
            "schemaVersion": S.SCHEMA_VERSION,
            "items": [item_summary(i.variants, i.ctx, i.revision) for i in items],
            "truncated": truncated,
        }

    return await _guarded("episodes", _run())


@router.get("/items/{serverId}/{ratingKey}/history", response_model=S.HistoryResponse,
            response_model_by_alias=True, response_model_exclude_unset=True,
            dependencies=_READ_GUARD, responses=_ERROR_RESPONSES)
async def get_history(
    server_id: str = _SERVER_ID,
    rating_key: str = _RATING_KEY,
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=1024),
):
    async def _run():
        async with db_module.async_session_factory() as session:
            edits, next_cursor = await svc.history(session, server_id, rating_key, limit, cursor)
        return {"schemaVersion": S.SCHEMA_VERSION, "edits": edits, "nextCursor": next_cursor}

    return await _guarded("history", _run())


@router.get("/edits/{editId}", response_model=S.EditResponse, response_model_by_alias=True,
            response_model_exclude_unset=True, dependencies=_READ_GUARD, responses=_ERROR_RESPONSES)
async def get_edit(edit_id: str = _EDIT_ID):
    async def _run():
        async with db_module.async_session_factory() as session:
            return {"schemaVersion": S.SCHEMA_VERSION, "edit": await svc.get_edit(session, edit_id)}

    return await _guarded("edit", _run())


@router.get("/fields", response_model=S.FieldsResponse, response_model_by_alias=True,
            dependencies=_READ_GUARD, responses=_ERROR_RESPONSES)
async def get_fields():
    """The writable fields per type (dotted names, byte order) and their bounds, so a
    client can detect contract drift before writing."""
    return {
        "schemaVersion": S.SCHEMA_VERSION,
        "types": {t: sorted(names) for t, names in FIELDS_BY_TYPE.items()},
        "limits": {k: dict(v) for k, v in LIMITS.items()},
    }


# ─── Simulations and writes ──────────────────────────────────────────────────


async def _simulate(
    route: str,
    *,
    server_id: str,
    rating_key: str,
    plan_fn: svc.PlanFn,
    if_match: str | None,
    allow_regroup: bool,
    response: Response,
) -> dict[str, Any]:
    async def _run():
        async with db_module.async_session_factory() as session:
            return await svc.preview(
                session, server_id=server_id, rating_key=rating_key, plan_fn=plan_fn,
                if_match=_if_match(if_match, required=False), allow_regroup=allow_regroup,
            )

    result = await _guarded(route, _run())
    _etag(response, result["revisionAfter"])
    return result


async def _commit(
    route: str,
    request: Request,
    response: Response,
    *,
    server_id: str,
    rating_key: str,
    plan_fn: svc.PlanFn,
    if_match: str | None,
    idempotency_key: str | None,
    body_fingerprint: dict[str, Any],
    reason: str | None,
    allow_regroup: bool,
    reverts: str | None = None,
) -> dict[str, Any]:
    required_if_match = _if_match(if_match, required=True)
    key = _idempotency_key(idempotency_key)
    principal = _principal(request)
    sha = svc.body_sha256(f"{request.method} {request.url.path}", body_fingerprint)

    result = await _guarded(route, svc.write(
        session_factory=db_module.async_session_factory,
        server_id=server_id, rating_key=rating_key, plan_fn=plan_fn,
        if_match=required_if_match or "", idempotency_key=key, sha=sha,
        key_id=principal.key_id or svc.MASTER_KEY_ID, request_id=_request_id(), reason=reason,
        allow_regroup=allow_regroup, deadline_s=metadata_limits.request_timeout_seconds(),
        reverts=reverts,
    ))
    _etag(response, result["revisionAfter"])
    return result


_RESULT = dict(response_model=S.PatchResult, response_model_by_alias=True,
               response_model_exclude_unset=True, responses=_ERROR_RESPONSES)


@router.post("/items/{serverId}/{ratingKey}/preview", dependencies=_READ_GUARD, **_RESULT)
async def preview_patch(
    response: Response,
    server_id: str = _SERVER_ID,
    rating_key: str = _RATING_KEY,
    body: S.PatchBody = Body(),
    if_match: str | None = Header(default=None, alias="If-Match"),
):
    """Simulate `PATCH`: same plan, nothing written (no journal row, no lock)."""
    return await _simulate(
        "preview", server_id=server_id, rating_key=rating_key, plan_fn=svc.patch_plan_fn(body),
        if_match=if_match, allow_regroup=body.allow_regroup, response=response,
    )


@router.patch("/items/{serverId}/{ratingKey}", dependencies=_WRITE_GUARD, **_RESULT)
async def patch_item(
    request: Request,
    response: Response,
    server_id: str = _SERVER_ID,
    rating_key: str = _RATING_KEY,
    body: S.PatchBody = Body(),
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """Partial write on EVERY variant, one transaction. Requires `If-Match` and
    `Idempotency-Key`."""
    return await _commit(
        "patch", request, response, server_id=server_id, rating_key=rating_key,
        plan_fn=svc.patch_plan_fn(body), if_match=if_match, idempotency_key=idempotency_key,
        body_fingerprint={
            "fields": svc.patch_dict(body), "allowRegroup": body.allow_regroup, "reason": body.reason,
        },
        reason=body.reason, allow_regroup=body.allow_regroup,
    )


@router.post("/items/{serverId}/{ratingKey}/lock/preview", dependencies=_READ_GUARD, **_RESULT)
async def preview_lock(
    response: Response,
    server_id: str = _SERVER_ID,
    rating_key: str = _RATING_KEY,
    body: S.LockBody = Body(),
    if_match: str | None = Header(default=None, alias="If-Match"),
):
    return await _simulate(
        "lock_preview", server_id=server_id, rating_key=rating_key,
        plan_fn=svc.lock_plan_fn(body.locked), if_match=if_match, allow_regroup=True, response=response,
    )


@router.post("/items/{serverId}/{ratingKey}/lock", dependencies=_WRITE_GUARD, **_RESULT)
async def set_lock(
    request: Request,
    response: Response,
    server_id: str = _SERVER_ID,
    rating_key: str = _RATING_KEY,
    body: S.LockBody = Body(),
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """Lock / unlock every variant (journaled). Unlocking gives the row back to the
    automatic enrichment."""
    return await _commit(
        "lock", request, response, server_id=server_id, rating_key=rating_key,
        plan_fn=svc.lock_plan_fn(body.locked), if_match=if_match, idempotency_key=idempotency_key,
        body_fingerprint={"locked": body.locked, "reason": body.reason},
        reason=body.reason, allow_regroup=True,
    )


async def _edit_target(edit_id: str) -> tuple[str, str]:
    async def _run():
        async with db_module.async_session_factory() as session:
            row = await svc.get_edit_row(session, edit_id)
            return row.server_id, row.rating_key

    return await _guarded("edit_lookup", _run())


@router.post("/edits/{editId}/revert/preview", dependencies=_READ_GUARD, **_RESULT)
async def preview_revert(
    response: Response,
    edit_id: str = _EDIT_ID,
    body: S.RevertBody | None = Body(default=None),
    if_match: str | None = Header(default=None, alias="If-Match"),
):
    body = body or S.RevertBody()
    server_id, rating_key = await _edit_target(edit_id)
    return await _simulate(
        "revert_preview", server_id=server_id, rating_key=rating_key,
        plan_fn=svc.revert_plan_fn(edit_id), if_match=if_match,
        allow_regroup=body.allow_regroup, response=response,
    )


@router.post("/edits/{editId}/revert", dependencies=_WRITE_GUARD, **_RESULT)
async def revert_edit(
    request: Request,
    response: Response,
    edit_id: str = _EDIT_ID,
    body: S.RevertBody | None = Body(default=None),
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """Undo an edit: fields, variants, derived values and lock, or 409 `revert_conflict`."""
    body = body or S.RevertBody()
    server_id, rating_key = await _edit_target(edit_id)
    return await _commit(
        "revert", request, response, server_id=server_id, rating_key=rating_key,
        plan_fn=svc.revert_plan_fn(edit_id), if_match=if_match, idempotency_key=idempotency_key,
        body_fingerprint={"allowRegroup": body.allow_regroup, "reason": body.reason},
        reason=body.reason, allow_regroup=body.allow_regroup, reverts=edit_id,
    )


__all__ = ["router", "enforce_metadata_rate_limit", "enforce_metadata_write_rate_limit"]
