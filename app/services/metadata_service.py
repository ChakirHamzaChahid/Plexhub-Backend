"""Database layer of the metadata API (lot B2, ADR 0011).

Reads build `VariantState`s from `media` rows; writes run INSIDE
`immediate_write_session` (writer lock taken up front, deadline-bound) and apply
the very plan that `preview` returns. Every decision (validation, derived values,
regrouping, no-op, revision) is made by the pure modules `metadata_fields` /
`metadata_plan`; this module only loads, counts, writes and journals.

Visibility: the API sees EVERY row — hidden categories, adult content and
provider-outage masking included (it is an operator tool, like `/admin`).
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, func, or_, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.immediate_tx import DatabaseBusy, immediate_write_session
from app.models.database import Media, MetadataEdit
from app.services.metadata_fields import FieldError, normalize_patch
from app.services.metadata_plan import (
    Plan,
    RevertConflict,
    VariantState,
    compute_revision,
    plan_lock,
    plan_patch,
    plan_revert,
    sort_variants,
    variant_from_columns,
)
from app.services.metadata_views import (
    ItemContext,
    context_from_rows,
    edit_view,
    journal_json,
    patch_result,
)
from app.utils.time import now_ms
from app.utils.ulid import new_ulid

logger = logging.getLogger("plexhub.metadata")

MASTER_KEY_ID = "master"
LOOKUP_LIMIT = 50
EPISODES_LIMIT = 2_000
_MAX_CURSOR_MS = 2**62  # a forged huge integer would overflow SQLite and surface as a 500

# Every column read to build a variant + the context of the canonical row.
_ROW_COLUMNS: tuple[str, ...] = (
    "rating_key", "server_id", "filter", "sort_order", "type", "is_adult",
    "is_in_allowed_categories", "parent_index", "index", "grandparent_rating_key",
    "grandparent_title", "updated_at", "duration",
    "title", "title_sortable", "original_title", "summary", "tagline", "genres", "studio",
    "country", "content_rating", "year", "premiered", "status", "imdb_id", "tmdb_id",
    "tvdb_id", "wikidata_id", "imdb_rating", "imdb_votes", "tmdb_rating", "tmdb_votes",
    "cast", "cast_json", "youtube_trailer", "unification_id", "history_group_key",
    "display_rating", "match_locked", "match_source",
)


# ─── Errors (mapped to HTTP by app/api/metadata_v1.py) ───────────────────────


class MetadataError(Exception):
    """Base of the service errors."""


class ItemNotFound(MetadataError):
    pass


class EditNotFound(MetadataError):
    pass


class InvalidParameter(MetadataError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class RevisionMismatch(MetadataError):
    def __init__(self, current: str) -> None:
        super().__init__("revision mismatch")
        self.current = current


class IdempotencyKeyReused(MetadataError):
    pass


class RegroupNotAllowed(MetadataError):
    def __init__(self, before: str, after: str, members_before: int, members_after: int) -> None:
        super().__init__("regroup not allowed")
        self.before, self.after = before, after
        self.members_before, self.members_after = members_before, members_after


class RevertNotAllowed(MetadataError):
    def __init__(self, fields: list[str]) -> None:
        super().__init__("revert conflict")
        self.fields = fields


# ─── Items ───────────────────────────────────────────────────────────────────


@dataclass
class Item:
    variants: list[VariantState]  # canonical first
    ctx: ItemContext
    revision: str

    @property
    def type(self) -> str:
        return self.variants[0].type

    @property
    def is_adult(self) -> bool:
        return any(v.is_adult for v in self.variants)

    @property
    def server_id(self) -> str:
        return self.variants[0].server_id

    @property
    def rating_key(self) -> str:
        return self.variants[0].rating_key


def _row_cols(row: Media) -> dict[str, Any]:
    return {name: getattr(row, name) for name in _ROW_COLUMNS}


def _build_item(rows: Sequence[Mapping[str, Any]]) -> Item:
    variants = sort_variants([variant_from_columns(r) for r in rows])
    return Item(variants=variants, ctx=context_from_rows(rows), revision=compute_revision(variants))


async def load_items(
    session: AsyncSession, keys: Sequence[tuple[str, str]]
) -> dict[tuple[str, str], Item]:
    """Items for `(server_id, rating_key)` pairs, in one query."""
    if not keys:
        return {}
    rows = (
        await session.execute(
            select(Media).where(tuple_(Media.server_id, Media.rating_key).in_(list(keys)))
        )
    ).scalars().all()
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault((row.server_id, row.rating_key), []).append(_row_cols(row))
    return {k: _build_item(v) for k, v in grouped.items()}


async def load_item(session: AsyncSession, server_id: str, rating_key: str) -> Item | None:
    rows = (
        await session.execute(
            select(Media).where(Media.server_id == server_id, Media.rating_key == rating_key)
        )
    ).scalars().all()
    if not rows:
        return None
    return _build_item([_row_cols(r) for r in rows])


async def members_count(session: AsyncSession, unification_id: str, server_id: str, rating_key: str) -> int:
    """OTHER items (distinct `(server_id, rating_key)`) sharing `unification_id`."""
    if not unification_id:
        return 0
    inner = (
        select(Media.server_id, Media.rating_key)
        .where(
            Media.unification_id == unification_id,
            ~and_(Media.server_id == server_id, Media.rating_key == rating_key),
        )
        .group_by(Media.server_id, Media.rating_key)
        .subquery()
    )
    return (await session.execute(select(func.count()).select_from(inner))).scalar_one()


def _sorted_items(items: dict[tuple[str, str], Item], order: Sequence[tuple[str, str]]) -> list[Item]:
    return [items[k] for k in order if k in items]


async def lookup(
    session: AsyncSession, type_: str, kind: str, value: str
) -> tuple[list[Item], bool]:
    column = {
        "imdb": Media.imdb_id, "tmdb": Media.tmdb_id, "tvdb": Media.tvdb_id,
        "unification": Media.unification_id,
    }[kind]
    pairs = (
        await session.execute(
            select(Media.server_id, Media.rating_key)
            .where(Media.type == type_, column == value)
            .group_by(Media.server_id, Media.rating_key)
            .order_by(Media.server_id, Media.rating_key)
            .limit(LOOKUP_LIMIT + 1)
        )
    ).all()
    keys = [(s, r) for s, r in pairs]
    truncated = len(keys) > LOOKUP_LIMIT
    keys = keys[:LOOKUP_LIMIT]
    return _sorted_items(await load_items(session, keys), keys), truncated


def _encode_cursor(*parts: Any) -> str:
    raw = json.dumps(list(parts), separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str, size: int) -> list[Any]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        parts = json.loads(raw)
    except (binascii.Error, ValueError, RecursionError):
        raise InvalidParameter("Invalid cursor") from None
    if not isinstance(parts, list) or len(parts) != size:
        raise InvalidParameter("Invalid cursor")
    return parts


async def list_items(
    session: AsyncSession,
    type_: str,
    missing: str | None,
    server_id: str | None,
    limit: int,
    cursor: str | None,
) -> tuple[list[Item], str | None]:
    conditions = [Media.type == type_]
    no_imdb = or_(Media.imdb_id.is_(None), Media.imdb_id == "")
    no_tmdb = or_(Media.tmdb_id.is_(None), Media.tmdb_id == "")
    if missing == "imdb":
        conditions.append(no_imdb)
    elif missing == "tmdb":
        conditions.append(no_tmdb)
    elif missing == "any":
        conditions.append(and_(no_imdb, no_tmdb))
    if server_id:
        conditions.append(Media.server_id == server_id)
    if cursor:
        after_server, after_key = _decode_cursor(cursor, 2)
        if not isinstance(after_server, str) or not isinstance(after_key, str):
            raise InvalidParameter("Invalid cursor")
        conditions.append(tuple_(Media.server_id, Media.rating_key) > tuple_(after_server, after_key))
    pairs = (
        await session.execute(
            select(Media.server_id, Media.rating_key)
            .where(*conditions)
            .group_by(Media.server_id, Media.rating_key)
            .order_by(Media.server_id, Media.rating_key)
            .limit(limit + 1)
        )
    ).all()
    keys = [(s, r) for s, r in pairs]
    has_more = len(keys) > limit
    keys = keys[:limit]
    next_cursor = _encode_cursor(*keys[-1]) if has_more and keys else None
    return _sorted_items(await load_items(session, keys), keys), next_cursor


async def list_episodes(
    session: AsyncSession, server_id: str, show_rating_key: str, season: int | None, episode: int | None
) -> tuple[list[Item], bool]:
    show = await load_item(session, server_id, show_rating_key)
    if show is None:
        raise ItemNotFound
    if show.type != "show":
        raise InvalidParameter("The item is not a show")
    conditions = [
        Media.type == "episode", Media.server_id == server_id,
        Media.grandparent_rating_key == show_rating_key,
    ]
    if season is not None:
        conditions.append(Media.parent_index == season)
    if episode is not None:
        conditions.append(Media.index == episode)
    rows = (await session.execute(select(Media).where(*conditions))).scalars().all()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row.rating_key, []).append(_row_cols(row))
    items = [_build_item(v) for v in grouped.values()]
    items.sort(key=lambda i: (
        i.ctx.season is None, i.ctx.season or 0, i.ctx.episode is None, i.ctx.episode or 0,
        i.rating_key.encode(),
    ))
    return items[:EPISODES_LIMIT], len(items) > EPISODES_LIMIT


# ─── Shared planning (preview AND write call exactly this) ───────────────────


def patch_dict(body: Any) -> dict[str, Any]:
    """The `fields` object as sent: only PRESENT keys, camelCase."""
    return body.fields.model_dump(by_alias=True, exclude_unset=True)


def body_sha256(path: str, body: Mapping[str, Any]) -> str:
    """Fingerprint of the canonical body, path included (idempotency, contract § 4.5)."""
    blob = json.dumps({"path": path, **body}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8", "surrogatepass")).hexdigest()


def _check_if_match(item: Item, if_match: str | None) -> None:
    if if_match is not None and if_match != item.revision:
        raise RevisionMismatch(item.revision)


async def _guard_regroup(
    session: AsyncSession, plan: Plan, item: Item, allow_regroup: bool
) -> tuple[int, int]:
    members_before = await members_count(session, plan.unification_before, item.server_id, item.rating_key)
    if plan.unification_after == plan.unification_before:
        members_after = members_before
    else:
        members_after = await members_count(session, plan.unification_after, item.server_id, item.rating_key)
    if plan.regroup_changes and not allow_regroup:
        raise RegroupNotAllowed(
            plan.unification_before, plan.unification_after, members_before, members_after
        )
    return members_before, members_after


async def _newer_live_edit_exists(session: AsyncSession, edit: MetadataEdit) -> bool:
    """A newer edit on the same item that is neither reverted nor the compensation
    of an edit newer than `edit` (such a pair cancels out)."""
    newer_ids = select(MetadataEdit.edit_id).where(
        MetadataEdit.server_id == edit.server_id,
        MetadataEdit.rating_key == edit.rating_key,
        MetadataEdit.edit_id > edit.edit_id,
    )
    count = (
        await session.execute(
            select(func.count())
            .select_from(MetadataEdit)
            .where(
                MetadataEdit.server_id == edit.server_id,
                MetadataEdit.rating_key == edit.rating_key,
                MetadataEdit.edit_id > edit.edit_id,
                MetadataEdit.reverted_by.is_(None),
                or_(MetadataEdit.kind != "revert", MetadataEdit.reverts.notin_(newer_ids)),
            )
        )
    ).scalar_one()
    return count > 0


async def _revert_plan(session: AsyncSession, item: Item, edit: MetadataEdit) -> Plan:
    if edit.reverted_by is not None:
        raise RevertNotAllowed([])
    record = {
        "before": json.loads(edit.before_json)["variants"],
        "after": json.loads(edit.after_json)["variants"],
    }
    try:
        return plan_revert(
            item.variants, record, lock_newer_edit=await _newer_live_edit_exists(session, edit)
        )
    except RevertConflict as conflict:
        raise RevertNotAllowed(conflict.fields) from None


async def get_edit_row(session: AsyncSession, edit_id: str) -> MetadataEdit:
    row = await session.get(MetadataEdit, edit_id)
    if row is None:
        raise EditNotFound
    return row


PlanFn = Callable[[AsyncSession, Item], Awaitable[Plan]]


def patch_plan_fn(body: Any) -> PlanFn:
    async def _plan(session: AsyncSession, item: Item) -> Plan:
        patch = normalize_patch(patch_dict(body), item.type, is_adult=item.is_adult)
        return plan_patch(item.variants, patch)

    return _plan


def lock_plan_fn(locked: bool) -> PlanFn:
    async def _plan(session: AsyncSession, item: Item) -> Plan:
        return plan_lock(item.variants, locked=locked)

    return _plan


def revert_plan_fn(edit_id: str) -> PlanFn:
    """The edit is re-read INSIDE the transaction that plans (and, for a write,
    applies) the revert: `reverted_by` may have changed since the route looked."""

    async def _plan(session: AsyncSession, item: Item) -> Plan:
        return await _revert_plan(session, item, await get_edit_row(session, edit_id))

    return _plan


# ─── Preview (reads only) ────────────────────────────────────────────────────


async def preview(
    session: AsyncSession,
    *,
    server_id: str,
    rating_key: str,
    plan_fn: PlanFn,
    if_match: str | None,
    allow_regroup: bool,
) -> dict[str, Any]:
    """Simulation: loads, plans (same `plan_fn` as the write), guards, renders. No write
    transaction, no journal row, no lock, no rebuild."""
    item = await load_item(session, server_id, rating_key)
    if item is None:
        raise ItemNotFound
    _check_if_match(item, if_match)
    plan = await plan_fn(session, item)
    members_before, members_after = await _guard_regroup(session, plan, item, allow_regroup)
    return patch_result(
        plan, dry_run=True, replayed=False, edit_id=None, at_ms=now_ms(),
        members_before=members_before, members_after=members_after,
    )


# ─── Write ───────────────────────────────────────────────────────────────────


async def _find_replay(
    session: AsyncSession, key_id: str, idempotency_key: str, sha: str
) -> dict[str, Any] | None:
    row = (
        await session.execute(
            select(MetadataEdit).where(
                MetadataEdit.key_id == key_id, MetadataEdit.idempotency_key == idempotency_key
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    if row.body_sha256 != sha:
        raise IdempotencyKeyReused
    response = json.loads(row.response_json)
    response["replayed"] = True
    return response


async def write(
    *,
    session_factory: Callable[[], AsyncSession],
    server_id: str,
    rating_key: str,
    plan_fn: PlanFn,
    if_match: str,
    idempotency_key: str,
    sha: str,
    key_id: str,
    request_id: str | None,
    reason: str | None,
    allow_regroup: bool,
    deadline_s: float,
    reverts: str | None = None,
) -> dict[str, Any]:
    """Validate a write (patch, lock or revert) in ONE transaction: replay check,
    `If-Match`, plan, regroup guard, no-op, `UPDATE` of every variant, journal row.
    The response is fully prepared before the COMMIT (no failure can follow it)."""
    wrote_type: str | None = None
    async with immediate_write_session(session_factory, deadline_s=deadline_s) as tx:
        session = tx.session
        replay = await _find_replay(session, key_id, idempotency_key, sha)
        if replay is not None:
            return replay
        item = await load_item(session, server_id, rating_key)
        if item is None:
            raise ItemNotFound
        _check_if_match(item, if_match)
        plan = await plan_fn(session, item)
        members_before, members_after = await _guard_regroup(session, plan, item, allow_regroup)

        at = now_ms()
        if plan.noop:
            return patch_result(
                plan, dry_run=False, replayed=False, edit_id=None, at_ms=at,
                members_before=members_before, members_after=members_after,
            )
        for change in plan.changes:
            await session.execute(
                update(Media)
                .where(
                    Media.server_id == server_id, Media.rating_key == rating_key,
                    Media.filter == change.filter, Media.sort_order == change.sort_order,
                )
                .values(**change.updates, updated_at=at)
            )
        edit_id = new_ulid(at)
        result = patch_result(
            plan, dry_run=False, replayed=False, edit_id=edit_id, at_ms=at,
            members_before=members_before, members_after=members_after,
        )
        before_json, after_json = journal_json(plan)
        session.add(MetadataEdit(
            edit_id=edit_id, kind=plan.kind, at_ms=at, server_id=server_id, rating_key=rating_key,
            item_type=item.type, key_id=key_id, request_id=request_id,
            idempotency_key=idempotency_key, body_sha256=sha, reason=reason,
            revision_before=plan.revision_before, revision_after=plan.revision_after,
            before_json=before_json, after_json=after_json,
            response_json=json.dumps(result, ensure_ascii=False),
            reverts=reverts, reverted_by=None,
        ))
        if reverts is not None:
            await session.execute(
                update(MetadataEdit).where(MetadataEdit.edit_id == reverts).values(reverted_by=edit_id)
            )
        wrote_type = item.type
    # Committed. Group snapshot rebuild is debounced and runs outside the transaction.
    if wrote_type in ("movie", "show"):
        from app.services import unified_group_service

        unified_group_service.schedule_rebuild(wrote_type, session_factory=session_factory)
    return result


# ─── History ─────────────────────────────────────────────────────────────────


async def history(
    session: AsyncSession, server_id: str, rating_key: str, limit: int, cursor: str | None
) -> tuple[list[dict[str, Any]], str | None]:
    conditions = [MetadataEdit.server_id == server_id, MetadataEdit.rating_key == rating_key]
    if cursor:
        at_ms, edit_id = _decode_cursor(cursor, 2)
        if (
            isinstance(at_ms, bool) or not isinstance(at_ms, int) or not 0 <= at_ms < _MAX_CURSOR_MS
            or not isinstance(edit_id, str)
        ):
            raise InvalidParameter("Invalid cursor")
        conditions.append(or_(
            MetadataEdit.at_ms < at_ms,
            and_(MetadataEdit.at_ms == at_ms, MetadataEdit.edit_id < edit_id),
        ))
    rows = (
        await session.execute(
            select(MetadataEdit).where(*conditions)
            .order_by(MetadataEdit.at_ms.desc(), MetadataEdit.edit_id.desc())
            .limit(limit + 1)
        )
    ).scalars().all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    if not rows and not cursor:
        known = await session.execute(
            select(func.count()).select_from(Media)
            .where(Media.server_id == server_id, Media.rating_key == rating_key)
        )
        if known.scalar_one() == 0:
            raise ItemNotFound
    next_cursor = _encode_cursor(rows[-1].at_ms, rows[-1].edit_id) if has_more and rows else None
    return [edit_view(_edit_record(r)) for r in rows], next_cursor


def _edit_record(row: MetadataEdit) -> dict[str, Any]:
    return {
        c: getattr(row, c)
        for c in (
            "edit_id", "kind", "at_ms", "server_id", "rating_key", "item_type", "key_id",
            "request_id", "idempotency_key", "reason", "revision_before", "revision_after",
            "before_json", "after_json", "reverts", "reverted_by",
        )
    }


async def get_edit(session: AsyncSession, edit_id: str) -> dict[str, Any]:
    return edit_view(_edit_record(await get_edit_row(session, edit_id)))


__all__ = [
    "DatabaseBusy", "EditNotFound", "FieldError", "IdempotencyKeyReused", "InvalidParameter",
    "Item", "ItemNotFound", "MASTER_KEY_ID", "RegroupNotAllowed", "RevertNotAllowed",
    "RevisionMismatch", "body_sha256", "get_edit", "get_edit_row", "history", "list_episodes",
    "list_items", "load_item", "load_items", "lookup", "lock_plan_fn", "members_count",
    "patch_dict", "patch_plan_fn", "preview", "revert_plan_fn", "write",
]
