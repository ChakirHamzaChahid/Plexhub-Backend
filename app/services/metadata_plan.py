"""Plans of a metadata write: revision, `plan_patch`, `plan_lock`, `plan_revert`
(ADR 0011 § D2.1–D2.6). PURE: no I/O, no clock.

A *plan* is everything a write decides — per-variant column updates (derived
values and lock included), the changed field names, the revision before/after,
the regrouping — computed from the current variants and the normalised request.
`preview` returns a plan; `PATCH`/`lock`/`revert` apply the very same plan
inside the writer transaction. That is how "a simulation fails and succeeds
exactly like the real write" holds by construction.

The revision is a hash of the editable fields, the exposed derived values and
the lock state of EVERY variant. It changes whichever writer touches them
(sync, enrichment, admin, B2) without any new column.
"""
from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from app.services.metadata_columns import FIELDS_BY_TYPE
from app.services.metadata_fields import (
    changed_field_names,
    columns_to_flat,
    field_columns,
    values_equal,
)
from app.utils.rating_blend import blend_rating
from app.utils.unification import calculate_history_group_key, calculate_unification_id

LOCK_SOURCE = "plexhubtmm"
REVISION_FORM = 1  # bump ONLY when the set of fields entering the hash changes on purpose

_DERIVED_BY_COLUMN = {
    "unification_id": "unificationId",
    "history_group_key": "historyGroupKey",
    "title_sortable": "titleSortable",
    "display_rating": "displayRating",
}
_UNIFICATION_INPUTS = {"title", "year", "ids.imdb", "ids.tmdb"}
_RATING_FIELDS = {"ratings.imdb", "ratings.tmdb"}


@dataclass(frozen=True)
class Lock:
    locked: bool
    source: str | None


@dataclass(frozen=True)
class VariantState:
    server_id: str
    rating_key: str
    filter: str
    sort_order: str
    type: str
    is_adult: bool
    flat: dict[str, Any]
    derived: dict[str, Any]
    lock: Lock


@dataclass
class VariantChange:
    filter: str
    sort_order: str
    updates: dict[str, Any]  # media columns (derived + lock included), without updated_at
    changed_fields: list[str]


@dataclass
class Plan:
    kind: str
    item_type: str
    noop: bool
    changed_fields: list[str]
    before: list[VariantState]
    after: list[VariantState]
    changes: list[VariantChange]
    revision_before: str
    revision_after: str
    unification_before: str
    unification_after: str
    regroup_changes: bool
    lock_before: Lock
    lock_after: Lock


class RevertConflict(Exception):
    """The edit cannot be reverted as is: `fields` lists what changed since
    (`lock` for the lock rule, `variants` when none of its variants remains)."""

    def __init__(self, fields: list[str]) -> None:
        super().__init__(",".join(fields))
        self.fields = fields


# ─── states ─────────────────────────────────────────────────────────────────


def variant_from_columns(cols: Mapping[str, Any]) -> VariantState:
    type_ = cols["type"]
    return VariantState(
        server_id=cols["server_id"],
        rating_key=cols["rating_key"],
        filter=cols["filter"],
        sort_order=cols["sort_order"],
        type=type_,
        is_adult=bool(cols.get("is_adult")),
        flat=columns_to_flat(cols, type_),
        derived={
            "unificationId": cols.get("unification_id") or "",
            "historyGroupKey": cols.get("history_group_key") or "",
            "titleSortable": cols.get("title_sortable") or "",
            "displayRating": float(cols.get("display_rating") or 0.0),
        },
        lock=Lock(bool(cols.get("match_locked")), cols.get("match_source") or None),
    )


def sort_variants(variants: Sequence[VariantState]) -> list[VariantState]:
    """Canonical variant first: smallest `(filter, sort_order)` in byte order."""
    return sorted(variants, key=lambda v: (v.filter.encode("utf-8"), v.sort_order.encode("utf-8")))


def variant_images(variants: Sequence[VariantState]) -> list[dict[str, Any]]:
    """JSON-able image of every variant, stored in the edit journal."""
    return [
        {
            "filter": v.filter,
            "sortOrder": v.sort_order,
            "flat": v.flat,
            "derived": v.derived,
            "lock": {"locked": v.lock.locked, "source": v.lock.source},
        }
        for v in sort_variants(variants)
    ]


def _round(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, dict):
        return {k: _round(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_round(v) for v in value]
    return value


def compute_revision(variants: Sequence[VariantState]) -> str:
    payload = {
        "v": REVISION_FORM,
        "variants": [
            {
                "filter": v.filter,
                "sortOrder": v.sort_order,
                "flat": _round(v.flat),
                "derived": _round(v.derived),
                "lock": [v.lock.locked, v.lock.source],
            }
            for v in sort_variants(variants)
        ],
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    # surrogatepass: odd text already stored must never make the revision (hence a read) crash
    digest = hashlib.sha256(blob.encode("utf-8", "surrogatepass")).digest()[:16]
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


# ─── building a plan ────────────────────────────────────────────────────────


def _apply_updates(v: VariantState, updates: Mapping[str, Any], flat: dict[str, Any]) -> VariantState:
    derived = dict(v.derived)
    for column, key in _DERIVED_BY_COLUMN.items():
        if column in updates:
            derived[key] = updates[column]
    lock = v.lock
    if "match_locked" in updates:
        lock = Lock(bool(updates["match_locked"]), updates.get("match_source"))
    elif "match_source" in updates:
        lock = Lock(lock.locked, updates["match_source"])
    return replace(v, flat=flat, derived=derived, lock=lock)


def _finish(
    kind: str,
    before: list[VariantState],
    after: list[VariantState],
    changes: list[VariantChange],
    noop: bool,
) -> Plan:
    names = sorted({n for c in changes for n in c.changed_fields})
    # The grouping reported to the client is the one of the first variant that REALLY regroups
    # (the canonical one when none does), so a 409 never reads "before == after".
    shown = next(
        (i for i, (b, a) in enumerate(zip(before, after))
         if b.derived["unificationId"] != a.derived["unificationId"]),
        0,
    )
    return Plan(
        kind=kind,
        item_type=before[0].type,
        noop=noop,
        changed_fields=names,
        before=before,
        after=after,
        changes=changes,
        revision_before=compute_revision(before),
        revision_after=compute_revision(after),
        unification_before=before[shown].derived["unificationId"],
        unification_after=after[shown].derived["unificationId"],
        regroup_changes=any(
            b.derived["unificationId"] != a.derived["unificationId"] for b, a in zip(before, after)
        ),
        lock_before=before[0].lock,
        lock_after=after[0].lock,
    )


def _lock_updates(v: VariantState) -> dict[str, Any]:
    """Locking a free variant: `plexhubtmm`. An already-locked variant keeps its
    source (the operator's `manual` verdict is more informative)."""
    if v.lock.locked:
        return {}
    return {"match_locked": True, "match_source": LOCK_SOURCE}


def _unification_inputs(flat: Mapping[str, Any]) -> tuple[Any, ...]:
    """(title, year, imdb, tmdb) in the argument order of `calculate_unification_id`."""
    return (flat.get("title") or "", flat.get("year"), flat.get("ids.imdb"), flat.get("ids.tmdb"))


def plan_patch(variants: Sequence[VariantState], patch_norm: Mapping[str, Any]) -> Plan:
    before = sort_variants(variants)
    after: list[VariantState] = []
    changes: list[VariantChange] = []
    any_field_change = False

    for v in before:
        flat_after = {**v.flat, **patch_norm}
        changed = [n for n in changed_field_names(v.flat, flat_after) if n in patch_norm]
        updates: dict[str, Any] = {}
        for name in changed:
            updates.update(field_columns(name, flat_after))

        if changed and v.type in ("movie", "show") and _UNIFICATION_INPUTS & set(changed):
            uid = calculate_unification_id(
                flat_after["title"] or "", flat_after.get("year"),
                flat_after.get("ids.imdb"), flat_after.get("ids.tmdb"),
            )
            hgk = calculate_history_group_key(uid, v.rating_key, v.server_id)
            if uid != v.derived["unificationId"]:
                updates["unification_id"] = uid
            if hgk != v.derived["historyGroupKey"]:
                updates["history_group_key"] = hgk
        if _RATING_FIELDS & set(changed):
            blended = blend_rating(
                (flat_after.get("ratings.imdb") or {}).get("value"),
                (flat_after.get("ratings.tmdb") or {}).get("value"),
            )
            if blended is not None and abs(blended - v.derived["displayRating"]) > 1e-9:
                updates["display_rating"] = blended

        any_field_change = any_field_change or bool(updates)
        changes.append(VariantChange(v.filter, v.sort_order, updates, changed))
        after.append(_apply_updates(v, updates, flat_after))

    if not any_field_change:
        return _finish(
            "patch", before, list(before),
            [VariantChange(c.filter, c.sort_order, {}, []) for c in changes], noop=True,
        )

    for i, v in enumerate(before):
        lock_updates = _lock_updates(v)
        if lock_updates:
            changes[i].updates.update(lock_updates)
            after[i] = _apply_updates(after[i], lock_updates, after[i].flat)
    return _finish("patch", before, after, changes, noop=False)


def plan_lock(variants: Sequence[VariantState], *, locked: bool) -> Plan:
    before = sort_variants(variants)
    after: list[VariantState] = []
    changes: list[VariantChange] = []
    for v in before:
        if locked:
            updates = _lock_updates(v)
        else:
            updates = {"match_locked": False, "match_source": None} if v.lock.locked else {}
        changes.append(VariantChange(v.filter, v.sort_order, updates, []))
        after.append(_apply_updates(v, updates, v.flat))
    noop = not any(c.updates for c in changes)
    return _finish("lock", before, before if noop else after, changes, noop=noop)


def plan_revert(
    current: Sequence[VariantState],
    edit: Mapping[str, Any],
    *,
    lock_newer_edit: bool,
) -> Plan:
    """Undo `edit` (its stored `before`/`after` variant images) on the CURRENT state.

    Per variant, every field the edit changed must still hold the edit's "after"
    value; the lock is restored only if it currently equals the edit's `lockAfter`
    and no newer non-reverted edit exists (`lock_newer_edit`). Derived values
    (`unification_id`, `history_group_key`, `title_sortable`, `display_rating`)
    are RESTORED from the record, not recomputed. Variants that appeared since the
    edit are left alone; one that vanished is skipped.
    """
    before = sort_variants(current)
    img_before = {(i["filter"], i["sortOrder"]): i for i in edit["before"]}
    img_after = {(i["filter"], i["sortOrder"]): i for i in edit["after"]}

    matched = [v for v in before if (v.filter, v.sort_order) in img_before]
    if not matched:
        raise RevertConflict(["variants"])

    conflicts: set[str] = set()
    lock_changed = False
    for v in matched:
        key = (v.filter, v.sort_order)
        names = changed_field_names(img_before[key]["flat"], img_after[key]["flat"])
        for name in names:
            if not values_equal(v.flat.get(name), img_after[key]["flat"].get(name)):
                conflicts.add(name)
        if img_before[key]["lock"] != img_after[key]["lock"]:
            lock_changed = True
    if lock_changed:
        for v in matched:
            wanted = img_after[(v.filter, v.sort_order)]["lock"]
            if {"locked": v.lock.locked, "source": v.lock.source} != wanted:
                conflicts.add("lock")
        if lock_newer_edit:
            conflicts.add("lock")
    if conflicts:
        raise RevertConflict(sorted(conflicts))

    after: list[VariantState] = []
    changes: list[VariantChange] = []
    for v in before:
        key = (v.filter, v.sort_order)
        if key not in img_before:
            changes.append(VariantChange(v.filter, v.sort_order, {}, []))
            after.append(v)
            continue
        b, a = img_before[key], img_after[key]
        names = changed_field_names(b["flat"], a["flat"])
        flat_after = {**v.flat, **{n: b["flat"][n] for n in names}}
        updates: dict[str, Any] = {}
        for name in names:
            updates.update(field_columns(name, b["flat"]))
        for column, derived_key in _DERIVED_BY_COLUMN.items():
            if b["derived"][derived_key] != a["derived"][derived_key]:
                updates[column] = b["derived"][derived_key]
        if v.type in ("movie", "show") and ("unification_id" in updates or "history_group_key" in updates):
            # The recorded grouping is only valid while the inputs it was derived from are
            # what they were before the edit. A later edit may have moved the title / year /
            # ids (without touching the grouping, e.g. a title change on a row that has an id):
            # then restoring the recorded key would leave a stale group on a LOCKED row, which
            # nothing ever corrects. Recompute from the resulting fields instead.
            if _unification_inputs(flat_after) != _unification_inputs(b["flat"]):
                uid = calculate_unification_id(*_unification_inputs(flat_after))
                hgk = calculate_history_group_key(uid, v.rating_key, v.server_id)
                for column, value, current in (
                    ("unification_id", uid, v.derived["unificationId"]),
                    ("history_group_key", hgk, v.derived["historyGroupKey"]),
                ):
                    if value != current:
                        updates[column] = value
                    else:
                        updates.pop(column, None)
        if b["lock"] != a["lock"]:
            updates["match_locked"] = b["lock"]["locked"]
            updates["match_source"] = b["lock"]["source"]
        changes.append(VariantChange(v.filter, v.sort_order, updates, names))
        after.append(_apply_updates(v, updates, flat_after))

    noop = not any(c.updates for c in changes)
    return _finish("revert", before, before if noop else after, changes, noop=noop)


__all__ = [
    "FIELDS_BY_TYPE", "LOCK_SOURCE", "Lock", "Plan", "RevertConflict", "VariantChange",
    "VariantState", "compute_revision", "plan_lock", "plan_patch", "plan_revert",
    "sort_variants", "variant_from_columns", "variant_images",
]
