"""Pydantic v2 models of the metadata API (`/api/metadata/v1`, lot B2).

Source of truth: contract `16-contrat-b2-backend.md` § 4–6 (names kept, deviations
listed in `docs/10-prd-metadata-api.md` § écarts); ADR 0011.

Conventions (CLAUDE.md § 3): camelCase aliases through `to_camel`,
`populate_by_name=True`; responses are declared with `response_model_by_alias=True`
and `response_model_exclude_unset=True` — an `EditableFields` only carries the
fields of the item's type (an episode has no `ids`), and the *partial* images of an
`Edit` (`before`/`after`: modified fields only) rely on the same rule.

Requests are STRICT (`"7.5"` is not a rating, `1` is not `true`) and `extra="forbid"`
(a field that is not writable at all — `thumbUrl`, `unificationId`… — is the
framework's 422; a writable-but-not-for-this-type field is a 400 `invalid_field`
raised by the validators). Bounds on VALUES are deliberately not expressed here:
they are 400 `invalid_field` naming the field, produced by `metadata_fields`.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

SCHEMA_VERSION = 1  # bump only on a removal / type change (and then open /api/metadata/v2)

ItemType = Literal["movie", "show", "episode"]
EditKind = Literal["patch", "revert", "lock"]
IntegrityCheck = Literal["ok", "failed", "not_run", "unknown"]


class _Out(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class _In(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid", strict=True,
    )


# ─── Requests ────────────────────────────────────────────────────────────────


class IdsPatch(_In):
    imdb: str | None = None
    tmdb: str | None = None
    tvdb: str | None = None
    wikidata: str | None = None


class RatingIn(_In):
    value: float
    votes: int | None = None


class RatingsPatch(_In):
    imdb: RatingIn | None = None
    tmdb: RatingIn | None = None


class CastIn(_In):
    name: str
    role: str | None = None
    thumb: str | None = None
    profile: str | None = None
    tvdb_id: str | None = None


class PatchFields(_In):
    """Partial `EditableFields`: an ABSENT key is unchanged, `null` clears."""

    title: str | None = None
    original_title: str | None = None
    summary: str | None = None
    tagline: str | None = None
    genres: list[str] | None = None
    studios: list[str] | None = None
    countries: list[str] | None = None
    content_rating: str | None = None
    year: int | None = None
    premiered: str | None = None
    status: str | None = None
    duration_ms: int | None = None
    ids: IdsPatch | None = None
    ratings: RatingsPatch | None = None
    cast: list[CastIn] | None = None
    youtube_trailer: str | None = None


# `reason` (0..200 characters) is NOT bounded here: a pydantic length error is a 422 that echoes
# the offending input back, which crashes the response when it holds a lone surrogate. The bound
# and the character rules live in `metadata_fields.validate_reason` (400 `invalid_field`).
class PatchBody(_In):
    fields: PatchFields = Field(default_factory=PatchFields)
    allow_regroup: bool = False
    reason: str | None = None


class RevertBody(_In):
    allow_regroup: bool = False
    reason: str | None = None


class LockBody(_In):
    locked: bool
    reason: str | None = None


# ─── Building blocks ─────────────────────────────────────────────────────────


class LockState(_Out):
    locked: bool
    source: str | None


class RatingOut(_Out):
    value: float
    votes: int | None = None


class CastEntry(_Out):
    name: str
    role: str | None = None
    thumb: str | None = None
    profile: str | None = None
    tvdb_id: str | None = None


class IdsOut(_Out):
    imdb: str | None = None
    tmdb: str | None = None
    tvdb: str | None = None
    wikidata: str | None = None


class RatingsOut(_Out):
    imdb: RatingOut | None = None
    tmdb: RatingOut | None = None


class EditableFields(_Out):
    title: str | None = None
    original_title: str | None = None
    summary: str | None = None
    tagline: str | None = None
    genres: list[str] | None = None
    studios: list[str] | None = None
    countries: list[str] | None = None
    content_rating: str | None = None
    year: int | None = None
    premiered: str | None = None
    status: str | None = None
    duration_ms: int | None = None
    ids: IdsOut | None = None
    ratings: RatingsOut | None = None
    cast: list[CastEntry] | None = None
    youtube_trailer: str | None = None


class Derived(_Out):
    unification_id: str
    history_group_key: str
    title_sortable: str
    display_rating: float


class VariantItem(_Out):
    filter: str
    sort_order: str
    fields: EditableFields
    lock: LockState


class Variants(_Out):
    count: int
    consistent: bool
    items: list[VariantItem]


class Context(_Out):
    is_adult: bool
    is_hidden: bool
    season: int | None
    episode: int | None
    show_rating_key: str | None
    show_title: str | None
    updated_at: int
    duration_implausible: bool  # additive (écart X11): duration < 5 min, to be read as absent


class ItemRef(_Out):
    server_id: str
    rating_key: str
    type: ItemType


class ItemSummary(_Out):
    server_id: str
    rating_key: str
    type: ItemType
    title: str | None
    year: int | None
    ids: IdsOut
    unification_id: str
    revision: str
    lock: LockState
    is_adult: bool
    is_hidden: bool  # additive (lot B3): same value as `context.isHidden`
    season: int | None
    episode: int | None
    show_rating_key: str | None


# ─── Responses ───────────────────────────────────────────────────────────────


class ItemView(_Out):
    schema_version: int
    server_id: str
    rating_key: str
    type: ItemType
    revision: str
    lock: LockState
    fields: EditableFields
    derived: Derived
    variants: Variants
    context: Context


class LookupResponse(_Out):
    schema_version: int
    items: list[ItemSummary]
    truncated: bool


class ItemListResponse(_Out):
    schema_version: int
    items: list[ItemSummary]
    next_cursor: str | None


class EpisodesResponse(_Out):
    schema_version: int
    items: list[ItemSummary]
    truncated: bool


class RegroupInfo(_Out):
    changes: bool
    unification_id_before: str
    unification_id_after: str
    members_before: int
    members_after: int


class PatchResult(_Out):
    schema_version: int
    kind: EditKind
    dry_run: bool
    noop: bool
    replayed: bool
    edit_id: str | None
    item: ItemRef
    revision_before: str
    revision_after: str
    changed_fields: list[str]
    before: EditableFields
    after: EditableFields
    derived_before: Derived
    derived_after: Derived
    lock_before: LockState
    lock_after: LockState
    regroup: RegroupInfo
    variants: Variants
    variants_after: Variants
    at_ms: int


class BeforeVariant(_Out):
    filter: str
    sort_order: str
    fields: EditableFields
    lock: LockState
    derived: Derived  # additive (écart X12)


class Edit(_Out):
    edit_id: str
    kind: EditKind
    at_ms: int
    key_id: str
    request_id: str | None
    idempotency_key: str | None
    reason: str | None
    item: ItemRef
    revision_before: str
    revision_after: str
    changed_fields: list[str]
    before: EditableFields
    after: EditableFields
    before_variants: list[BeforeVariant]
    lock_before: LockState
    lock_after: LockState
    derived_before: Derived
    derived_after: Derived
    unification_id_before: str
    unification_id_after: str
    reverts: str | None
    reverted_by: str | None


class HistoryResponse(_Out):
    schema_version: int
    edits: list[Edit]
    next_cursor: str | None


class EditResponse(_Out):
    schema_version: int
    edit: Edit


class BackupStatusResponse(_Out):
    schema_version: int
    enabled: bool
    last_success_at: int | None
    age_seconds: int | None
    integrity_check: IntegrityCheck
    last_attempt_at: int | None
    last_attempt_outcome: Literal["ok", "failed"] | None
    retention_days: int
    schedule_hour_local: int | None
    schedule_timezone: str | None
    server_now_ms: int


class FieldsResponse(_Out):
    schema_version: int
    types: dict[str, list[str]]
    limits: dict[str, dict[str, Any]]


# ─── Errors ──────────────────────────────────────────────────────────────────
# Named `Metadata…` on purpose: FastAPI disambiguates two models of the same class name by
# prefixing the module path, which would also RENAME the scrape gateway components
# (`ErrorResponse`) and break its committed machine contract.

ErrorCode = Literal[
    "invalid_parameter", "invalid_field", "invalid_api_key", "insufficient_scope",
    "item_not_found", "edit_not_found", "regroup_not_allowed", "revert_conflict",
    "idempotency_key_reused", "revision_mismatch", "precondition_required",
    "rate_limited", "database_busy",
]


class MetadataErrorDetail(_Out):
    """`{"detail": {"code", "message", …extras}}`. Extras (all optional):
    `field` (invalid_field), `retryAfterSeconds`, `currentRevision` (412),
    `header` (428), `fields` (revert_conflict), and the four regroup members."""

    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="allow",
    )
    code: ErrorCode
    message: str
    field: str | None = None
    retry_after_seconds: int | None = None
    current_revision: str | None = None
    header: str | None = None
    fields: list[str] | None = None
    unification_id_before: str | None = None
    unification_id_after: str | None = None
    members_before: int | None = None
    members_after: int | None = None


class MetadataErrorResponse(_Out):
    detail: MetadataErrorDetail
