"""Single source of truth for what the metadata API (lot B2) may write.

Pure module (no I/O). Three consumers share it so they cannot drift apart:

- the metadata API (validation, `GET /fields`, the revision of an item);
- every AUTOMATIC writer of `media` that must skip a row whose `match_locked` is
  set (`sync_worker` upsert, NFO import, certification/trailer/title back-fills…,
  ADR 0012 § D2.1);
- the tests that pin the inventory of `media` writers.

Names in `FIELD_COLUMNS` are the camelCase API names (dotted for sub-fields:
`ids.imdb`, `ratings.tmdb`) of contract B2 § 6.2; values are the `media` columns
a write of that field sets. Images, flow, read state, hashes, keys, the
adult/visibility flags and the provider-origin ratings are NEVER writable.
"""
from __future__ import annotations

TYPES: tuple[str, ...] = ("movie", "show", "episode")

# API field name -> media columns written by that field.
FIELD_COLUMNS: dict[str, tuple[str, ...]] = {
    "cast": ("cast", "cast_json"),
    "contentRating": ("content_rating",),
    "countries": ("country",),
    "durationMs": ("duration",),
    "genres": ("genres",),
    "ids.imdb": ("imdb_id",),
    "ids.tmdb": ("tmdb_id",),
    "ids.tvdb": ("tvdb_id",),
    "ids.wikidata": ("wikidata_id",),
    "originalTitle": ("original_title",),
    "premiered": ("premiered",),
    "ratings.imdb": ("imdb_rating", "imdb_votes"),
    "ratings.tmdb": ("tmdb_rating", "tmdb_votes"),
    "status": ("status",),
    "studios": ("studio",),
    "summary": ("summary",),
    "tagline": ("tagline",),
    "title": ("title", "title_sortable"),
    "year": ("year",),
    "youtubeTrailer": ("youtube_trailer",),
}

_MOVIE_FIELDS = tuple(sorted(n for n in FIELD_COLUMNS if n != "status"))

# Contract B2 § 6.3 (M3-D3): the writable field set per item type.
FIELDS_BY_TYPE: dict[str, tuple[str, ...]] = {
    "movie": _MOVIE_FIELDS,
    "show": tuple(sorted((*_MOVIE_FIELDS, "status"))),
    "episode": ("durationMs", "premiered", "ratings.imdb", "ratings.tmdb", "summary", "title"),
}

# Columns recomputed by the server on a write (never accepted from the client),
# but protected by the row lock all the same.
DERIVED_COLUMNS: tuple[str, ...] = (
    "unification_id",
    "history_group_key",
    "title_sortable",
    "display_rating",
)

# The lock-protected set: every column a B2 write can touch, derived ones included.
METADATA_WRITABLE_COLUMNS: frozenset[str] = frozenset(
    {col for cols in FIELD_COLUMNS.values() for col in cols} | set(DERIVED_COLUMNS)
)

# Bounds of contract B2 § 6.2 (+ URL bound, ADR 0012 / écart X10). Rendered by
# `GET /fields` and read by the validators, so the two cannot disagree.
LIMITS: dict[str, dict[str, object]] = {
    "title": {"min": 1, "max": 300},
    "originalTitle": {"min": 1, "max": 300},
    "summary": {"min": 1, "max": 10_000},
    "tagline": {"min": 1, "max": 500},
    "genres": {"maxItems": 20, "itemMin": 1, "itemMax": 64, "noComma": True},
    "studios": {"maxItems": 10, "itemMin": 1, "itemMax": 200, "noComma": True},
    "countries": {"maxItems": 20, "itemMin": 1, "itemMax": 64, "noComma": True},
    "contentRating": {"min": 1, "max": 32},
    "year": {"min": 1850, "max": 2200},
    "premiered": {"format": "YYYY-MM-DD"},
    "status": {"min": 1, "max": 64},
    "durationMs": {"min": 1, "max": 86_400_000},
    "ids.imdb": {"pattern": r"^tt\d{7,10}$"},
    "ids.tmdb": {"pattern": r"^\d{1,9}$"},
    "ids.tvdb": {"pattern": r"^\d{1,12}$"},
    "ids.wikidata": {"pattern": r"^Q\d+$"},
    "ratings": {"valueMin": 0, "valueMax": 10, "votesMin": 0, "votesMax": 2_147_483_647},
    "cast": {
        "maxItems": 50, "nameMin": 1, "nameMax": 200, "roleMax": 200,
        "urlMax": 2048, "urlScheme": "https", "tvdbIdPattern": r"^\d{1,12}$",
    },
    "youtubeTrailer": {"pattern": r"^[A-Za-z0-9_-]{11}$"},
    "reason": {"min": 0, "max": 200},
}
