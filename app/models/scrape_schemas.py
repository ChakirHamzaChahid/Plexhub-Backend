"""Pydantic v2 models of the scraping gateway (`/api/scrape/v1`, lot B1).

Source of truth: contract `15-contrat-b1-backend.md` sections 4.4-4.6, 5.2-5.10
and 6; PRD FR-016, FR-025, FR-028b, FR-035; ADR 0009 D2.4/D2.9/D2.11/D2.12.

Conventions (CLAUDE.md section 3): camelCase aliases through `to_camel`,
`populate_by_name=True` (so the mappers can emit camelCase dicts AND callers
may build a model with snake_case keywords), `extra="forbid"` (an unexpected
key is a bug of the mapper, not something to forward to the client), and
routes must be declared with `response_model_by_alias=True`.

Absent value -> JSON `null`, never an empty string; empty list -> `[]`.

`omdbStatus` is REQUIRED on `MovieDetails`/`ShowDetails`: the form stored in
the cache (ADR D2.4) is the dict returned by `scrape_mappers.map_movie` /
`map_show`, which carries neither the OMDb ratings nor `omdbStatus`; it only
becomes a valid `MovieDetails`/`ShowDetails` once `scrape_mappers.with_omdb`
has composed the response.
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel, model_validator
from pydantic.alias_generators import to_camel


class _ScrapeModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )


# --- Enumerations -----------------------------------------------------------

RatingSource = Literal["tmdb", "imdb", "rottenTomatoes", "metacritic"]
OmdbStatus = Literal[
    "ok", "not_configured", "budget_exhausted", "not_found", "error", "no_imdb_id",
]
CacheStatus = Literal["hit", "miss"]
MatchResult = Literal["matched", "ambiguous", "nomatch"]
FoundResult = Literal["found", "not_found"]
MediaKind = Literal["movie", "show"]
FindMediaKind = Literal["movie", "show", "episode"]
CacheFamily = Literal["match", "movie", "show", "season", "omdb", "find"]
ErrorCode = Literal[
    "invalid_parameter",
    "invalid_api_key",
    "insufficient_scope",
    "rate_limited",
    "upstream_error",
    "upstream_unavailable",
    "provider_not_configured",
    "upstream_timeout",
]


# --- Building blocks (contract 6) ------------------------------------------

class Rating(_ScrapeModel):
    """Contract 6.4. `tmdb`/`imdb` are 0..10 over 10; `rottenTomatoes` and
    `metacritic` are 0..100 over 100 with `votes: null`."""
    source: RatingSource
    value: float = Field(ge=0)
    max: float = Field(gt=0)
    votes: int | None = Field(default=None, ge=0)


class CastMember(_ScrapeModel):
    name: str
    character: str | None = None
    order: int
    profile_path: str | None = None
    tmdb_person_id: int


class CrewMember(_ScrapeModel):
    name: str
    job: str
    department: str | None = None
    profile_path: str | None = None
    tmdb_person_id: int


class Candidate(_ScrapeModel):
    """Contract 6.6."""
    tmdb_id: int
    media_type: MediaKind
    title: str
    original_title: str | None = None
    year: int | None = None
    overview: str | None = None
    poster_path: str | None = None
    score: float = Field(ge=0, le=1)


class Images(_ScrapeModel):
    """Contract 6.7: raw TMDB paths (`/abc.jpg`), bounded lists."""
    poster: str | None = None
    backdrop: str | None = None
    posters: list[str] = Field(default_factory=list, max_length=20)
    backdrops: list[str] = Field(default_factory=list, max_length=20)
    logos: list[str] = Field(default_factory=list, max_length=10)


class ExternalIds(_ScrapeModel):
    imdb: str | None = None
    tvdb: str | None = None
    wikidata: str | None = None


class SeasonExternalIds(_ScrapeModel):
    """Contract 6.3: a season only carries `{tvdb}`."""
    tvdb: str | None = None


class Collection(_ScrapeModel):
    tmdb_id: int
    name: str


class Trailer(_ScrapeModel):
    site: Literal["YouTube"]
    key: str


class SeasonSummary(_ScrapeModel):
    """Contract 6.2: one entry of `ShowDetails.seasons`."""
    season_number: int
    name: str | None = None
    air_date: str | None = None
    episode_count: int | None = None
    poster_path: str | None = None
    tmdb_id: int | None = None


# --- Details (contract 6.1 - 6.3) ------------------------------------------

class MovieDetails(_ScrapeModel):
    tmdb_id: int
    title: str | None = None
    original_title: str | None = None
    overview: str | None = None
    tagline: str | None = None
    release_date: str | None = None
    year: int | None = None
    runtime_minutes: int | None = None
    status: str | None = None
    content_rating: str | None = None
    genres: list[str] = Field(default_factory=list)
    studios: list[str] = Field(default_factory=list, max_length=5)
    countries: list[str] = Field(default_factory=list)
    collection: Collection | None = None
    external_ids: ExternalIds
    ratings: list[Rating] = Field(default_factory=list)
    omdb_status: OmdbStatus
    cast: list[CastMember] = Field(default_factory=list, max_length=30)
    crew: list[CrewMember] = Field(default_factory=list, max_length=30)
    trailer: Trailer | None = None
    images: Images
    image_base_url: str


class ShowDetails(_ScrapeModel):
    tmdb_id: int
    title: str | None = None
    original_title: str | None = None
    overview: str | None = None
    tagline: str | None = None
    first_air_date: str | None = None
    year: int | None = None
    episode_runtime_minutes: int | None = None
    status: str | None = None
    content_rating: str | None = None
    genres: list[str] = Field(default_factory=list)
    studios: list[str] = Field(default_factory=list, max_length=5)
    countries: list[str] = Field(default_factory=list)
    external_ids: ExternalIds
    ratings: list[Rating] = Field(default_factory=list)
    omdb_status: OmdbStatus
    cast: list[CastMember] = Field(default_factory=list, max_length=30)
    crew: list[CrewMember] = Field(default_factory=list, max_length=30)
    trailer: Trailer | None = None
    images: Images
    image_base_url: str
    number_of_seasons: int | None = None
    number_of_episodes: int | None = None
    seasons: list[SeasonSummary] = Field(default_factory=list)


class EpisodeDetails(_ScrapeModel):
    tmdb_id: int
    episode_number: int = Field(ge=1)
    season_number: int
    name: str | None = None
    overview: str | None = None
    air_date: str | None = None
    runtime_minutes: int | None = None
    still_path: str | None = None
    ratings: list[Rating] = Field(default_factory=list)
    crew: list[CrewMember] = Field(default_factory=list, max_length=10)
    guest_stars: list[CastMember] = Field(default_factory=list, max_length=20)


class SeasonDetails(_ScrapeModel):
    show_tmdb_id: int
    tmdb_id: int
    season_number: int
    name: str | None = None
    overview: str | None = None
    air_date: str | None = None
    external_ids: SeasonExternalIds
    images: Images
    image_base_url: str
    episodes: list[EpisodeDetails] = Field(default_factory=list, max_length=500)


class FindMatch(_ScrapeModel):
    """Contract 5.7: `showTmdbId`/`seasonNumber`/`episodeNumber` are set for
    an `episode` match only (null otherwise)."""
    media_type: FindMediaKind
    tmdb_id: int
    show_tmdb_id: int | None = None
    season_number: int | None = None
    episode_number: int | None = None


# --- Common envelope and route responses (contract 4.4, 5.2 - 5.7) ----------

class _Envelope(_ScrapeModel):
    """Contract 4.4. `cache_key` is null for `/search` (nothing persisted;
    `expiresAt` then equals `fetchedAt`). `language` is `-` for omdb/find."""
    schema_version: int = Field(ge=1)
    cache_key: str | None
    language: str
    fetched_at: int
    expires_at: int
    cache: CacheStatus


class MatchResponse(_Envelope):
    result: MatchResult
    confidence: float | None = Field(ge=0, le=1)
    tmdb_id: int | None = None
    media_type: MediaKind
    candidates: list[Candidate] = Field(default_factory=list, max_length=10)
    details: MovieDetails | ShowDetails | None = None


class ScrapeSearchResponse(_Envelope):
    """`GET /search` (contract 5.3). Named `Scrape…` because `app/api/ai.py`
    already publishes a `SearchResponse` component: two classes of the same
    name make FastAPI rename BOTH OpenAPI components to
    `app__api__ai__SearchResponse` / `app__models__…`, which would change the
    existing AI contract (B1-11)."""
    result: FoundResult
    candidates: list[Candidate] = Field(default_factory=list, max_length=10)


class MovieResponse(_Envelope):
    result: FoundResult
    details: MovieDetails | None = None


class ShowResponse(_Envelope):
    result: FoundResult
    details: ShowDetails | None = None


class SeasonResponse(_Envelope):
    result: FoundResult
    details: SeasonDetails | None = None


class FindResponse(_Envelope):
    result: FoundResult
    matches: list[FindMatch] = Field(default_factory=list, max_length=5)


# --- Cache administration (contract 5.8 - 5.10, FR-028b) --------------------

class FamilyStats(_ScrapeModel):
    """One `(family, keyVersion)` line of `/cache/stats`. `entries` is the sum
    of the three result classes; `expired` is counted IN ADDITION (a subset of
    `entries`, not a fourth class)."""
    family: CacheFamily
    key_version: int
    entries: int = Field(ge=0)
    positive: int = Field(ge=0)
    ambiguous: int = Field(ge=0)
    negative: int = Field(ge=0)
    expired: int = Field(ge=0)
    oldest_fetched_at: int | None = None
    newest_fetched_at: int | None = None

    @model_validator(mode="after")
    def _check_counts(self) -> "FamilyStats":
        if self.entries != self.positive + self.ambiguous + self.negative:
            raise ValueError("entries must equal positive + ambiguous + negative")
        if self.expired > self.entries:
            raise ValueError("expired must be a subset of entries")
        return self


class TtlInfo(_ScrapeModel):
    success_days: int
    negative_days: int


class SinceStart(_ScrapeModel):
    hits: int = Field(ge=0)
    misses: int = Field(ge=0)
    coalesced: int = Field(ge=0)
    upstream_errors: int = Field(ge=0)


class CacheStatsResponse(_ScrapeModel):
    generated_at: int
    families: list[FamilyStats] = Field(default_factory=list)
    ttl: TtlInfo
    since_start: SinceStart


class InvalidateRequest(_ScrapeModel):
    """Contract 5.9. EITHER `keys` (1..500) OR `family` + `tmdbId`; the
    exclusivity is a 400 `invalid_parameter` raised by the service (not a
    Pydantic validation error), hence every field is optional here.

    `family` is a plain string on purpose: a family other than
    movie|show|season is a 400 `invalid_parameter` (PRD FR-015b), not a 422.
    The bounds of `keys` and `tmdbId` stay native 422s.

    `Annotated[X | None, Field(...)] = None` (not `X | None = Field(...)`):
    the latter makes pydantic attach the field metadata to one member of the
    union inside FastAPI's body model (UnsupportedFieldAttributeWarning)."""
    keys: Annotated[list[str] | None, Field(min_length=1, max_length=500)] = None
    family: Annotated[
        str | None, Field(description="movie | show | season (other value: 400)")
    ] = None
    tmdb_id: Annotated[int | None, Field(ge=1, le=999_999_999)] = None


class InvalidateBody(RootModel[InvalidateRequest]):
    """Request body of `POST /cache/invalidate` (same JSON as
    `InvalidateRequest`, which it wraps).

    Why a wrapper: for a single non-embedded body model, FastAPI 0.115
    (pinned < 0.116, CLAUDE.md section 10) builds one `TypeAdapter` per field
    of that model with `Annotated[annotation, field_info]`; on an optional
    (`X | None`) field carrying an alias, pydantic >= 2.12 then emits an
    `UnsupportedFieldAttributeWarning` (9 of them for the 3 fields here, B1-09
    review). The `Annotated[...] = None` form above does not avoid it (the
    warning comes from FastAPI's re-wrapping, not from the declaration); a
    root model has a single, non-optional, alias-less `root` field, so
    nothing is re-wrapped."""


class InvalidateResponse(_ScrapeModel):
    deleted: int = Field(ge=0)


class PurgeRequest(_ScrapeModel):
    limit: int = Field(default=10_000, ge=1, le=100_000)


class PurgeResponse(_ScrapeModel):
    deleted: int = Field(ge=0)
    remaining: int = Field(ge=0)


# --- Errors (contract 4.6, FR-035) ------------------------------------------

class ErrorDetail(_ScrapeModel):
    code: ErrorCode
    message: str
    retry_after_seconds: int | None = Field(default=None, ge=0)


class ErrorResponse(_ScrapeModel):
    """Body of every gateway error: `{"detail": {"code", "message",
    "retryAfterSeconds"?}}` (FastAPI `HTTPException(detail=dict)`)."""
    detail: ErrorDetail
