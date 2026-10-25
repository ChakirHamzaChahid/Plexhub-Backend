"""Pure builders of the API views (camelCase dicts) from variants and plans.

No I/O: the service layer loads rows / counts members and passes them in. Every
dict is shaped for the Pydantic models of `app/models/metadata_schemas.py`
(validated when the route returns them).
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.models.metadata_schemas import SCHEMA_VERSION
from app.services.aggregation_service import IMPLAUSIBLE_DURATION_MS
from app.services.metadata_fields import changed_field_names, flat_to_nested
from app.services.metadata_plan import Lock, Plan, VariantState, sort_variants, variant_images


@dataclass(frozen=True)
class ItemContext:
    """Read-only context of the CANONICAL row (contract § 6.1)."""

    is_hidden: bool
    season: int | None
    episode: int | None
    show_rating_key: str | None
    show_title: str | None
    updated_at: int
    duration_ms: int | None


def context_from_rows(rows: Sequence[Mapping[str, Any]]) -> ItemContext:
    canonical = min(rows, key=lambda r: (r["filter"].encode(), r["sort_order"].encode()))
    is_episode = canonical["type"] == "episode"
    return ItemContext(
        is_hidden=not bool(canonical.get("is_in_allowed_categories", True)),
        season=canonical.get("parent_index") if is_episode else None,
        episode=canonical.get("index") if is_episode else None,
        show_rating_key=canonical.get("grandparent_rating_key") if is_episode else None,
        show_title=canonical.get("grandparent_title") if is_episode else None,
        updated_at=max(int(r.get("updated_at") or 0) for r in rows),
        duration_ms=canonical.get("duration"),
    )


# ─── small pieces ────────────────────────────────────────────────────────────


def lock_view(lock: Lock) -> dict[str, Any]:
    return {"locked": lock.locked, "source": lock.source}


def fields_view(flat: Mapping[str, Any]) -> dict[str, Any]:
    return flat_to_nested(flat)


def ids_view(flat: Mapping[str, Any]) -> dict[str, Any]:
    return {k: flat.get(f"ids.{k}") for k in ("imdb", "tmdb", "tvdb", "wikidata")}


def variants_view(variants: Sequence[VariantState]) -> dict[str, Any]:
    ordered = sort_variants(variants)
    first = ordered[0]
    consistent = all(
        not changed_field_names(first.flat, v.flat) and v.lock == first.lock for v in ordered[1:]
    )
    return {
        "count": len(ordered),
        "consistent": consistent,
        "items": [
            {
                "filter": v.filter,
                "sortOrder": v.sort_order,
                "fields": fields_view(v.flat),
                "lock": lock_view(v.lock),
            }
            for v in ordered
        ],
    }


# ─── item views ──────────────────────────────────────────────────────────────


def item_summary(variants: Sequence[VariantState], ctx: ItemContext, revision: str) -> dict[str, Any]:
    c = sort_variants(variants)[0]
    return {
        "serverId": c.server_id,
        "ratingKey": c.rating_key,
        "type": c.type,
        "title": c.flat.get("title"),
        "year": c.flat.get("year"),
        "ids": ids_view(c.flat),
        "unificationId": c.derived["unificationId"],
        "revision": revision,
        "lock": lock_view(c.lock),
        "isAdult": c.is_adult,
        "isHidden": ctx.is_hidden,
        "season": ctx.season,
        "episode": ctx.episode,
        "showRatingKey": ctx.show_rating_key,
    }


def item_view(variants: Sequence[VariantState], ctx: ItemContext, revision: str) -> dict[str, Any]:
    c = sort_variants(variants)[0]
    return {
        "schemaVersion": SCHEMA_VERSION,
        "serverId": c.server_id,
        "ratingKey": c.rating_key,
        "type": c.type,
        "revision": revision,
        "lock": lock_view(c.lock),
        "fields": fields_view(c.flat),
        "derived": dict(c.derived),
        "variants": variants_view(variants),
        "context": {
            "isAdult": c.is_adult,
            "isHidden": ctx.is_hidden,
            "season": ctx.season,
            "episode": ctx.episode,
            "showRatingKey": ctx.show_rating_key,
            "showTitle": ctx.show_title,
            "updatedAt": ctx.updated_at,
            "durationImplausible": (
                ctx.duration_ms is not None and ctx.duration_ms < IMPLAUSIBLE_DURATION_MS
            ),
        },
    }


# ─── write results ───────────────────────────────────────────────────────────


def regroup_view(plan: Plan, members_before: int, members_after: int) -> dict[str, Any]:
    return {
        "changes": plan.regroup_changes,
        "unificationIdBefore": plan.unification_before,
        "unificationIdAfter": plan.unification_after,
        "membersBefore": members_before,
        "membersAfter": members_after,
    }


def patch_result(
    plan: Plan,
    *,
    dry_run: bool,
    replayed: bool,
    edit_id: str | None,
    at_ms: int,
    members_before: int,
    members_after: int,
) -> dict[str, Any]:
    b, a = plan.before[0], plan.after[0]
    return {
        "schemaVersion": SCHEMA_VERSION,
        "kind": plan.kind,
        "dryRun": dry_run,
        "noop": plan.noop,
        "replayed": replayed,
        "editId": edit_id,
        "item": {"serverId": b.server_id, "ratingKey": b.rating_key, "type": b.type},
        "revisionBefore": plan.revision_before,
        "revisionAfter": plan.revision_after,
        "changedFields": plan.changed_fields,
        "before": fields_view(b.flat),
        "after": fields_view(a.flat),
        "derivedBefore": dict(b.derived),
        "derivedAfter": dict(a.derived),
        "lockBefore": lock_view(plan.lock_before),
        "lockAfter": lock_view(plan.lock_after),
        "regroup": regroup_view(plan, members_before, members_after),
        "variants": variants_view(plan.before),
        "variantsAfter": variants_view(plan.after),
        "atMs": at_ms,
    }


# ─── journal ─────────────────────────────────────────────────────────────────


def journal_json(plan: Plan) -> tuple[str, str]:
    """(`before_json`, `after_json`) stored in `metadata_edits`: the image of EVERY
    variant (fields, derived, lock) so a revert restores divergent variants exactly."""
    before = {"variants": variant_images(plan.before)}
    after = {"variants": variant_images(plan.after), "changedFields": plan.changed_fields}
    return (
        json.dumps(before, sort_keys=True, ensure_ascii=False),
        json.dumps(after, sort_keys=True, ensure_ascii=False),
    )


def edit_view(record: Mapping[str, Any]) -> dict[str, Any]:
    """API `Edit` from a `metadata_edits` row (a mapping of its columns)."""
    before = json.loads(record["before_json"])["variants"]
    after_doc = json.loads(record["after_json"])
    after = after_doc["variants"]
    changed = list(after_doc.get("changedFields", []))
    b0, a0 = before[0], after[0]
    return {
        "editId": record["edit_id"],
        "kind": record["kind"],
        "atMs": record["at_ms"],
        "keyId": record["key_id"],
        "requestId": record["request_id"],
        "idempotencyKey": record["idempotency_key"],
        "reason": record["reason"],
        "item": {
            "serverId": record["server_id"],
            "ratingKey": record["rating_key"],
            "type": record["item_type"],
        },
        "revisionBefore": record["revision_before"],
        "revisionAfter": record["revision_after"],
        "changedFields": changed,
        "before": flat_to_nested({n: b0["flat"][n] for n in changed if n in b0["flat"]}, pad=False),
        "after": flat_to_nested({n: a0["flat"][n] for n in changed if n in a0["flat"]}, pad=False),
        "beforeVariants": [
            {
                "filter": v["filter"],
                "sortOrder": v["sortOrder"],
                "fields": flat_to_nested(v["flat"]),
                "lock": v["lock"],
                "derived": v["derived"],
            }
            for v in before
        ],
        "lockBefore": b0["lock"],
        "lockAfter": a0["lock"],
        "derivedBefore": b0["derived"],
        "derivedAfter": a0["derived"],
        "unificationIdBefore": b0["derived"]["unificationId"],
        "unificationIdAfter": a0["derived"]["unificationId"],
        "reverts": record["reverts"],
        "revertedBy": record["reverted_by"],
    }
