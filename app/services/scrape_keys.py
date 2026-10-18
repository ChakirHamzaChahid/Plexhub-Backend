"""Canonical cache keys and bounded settings of the scrape gateway (lot B1).

ADR 0009 D2.2/D2.3, PRD FR-007..FR-009/FR-012, contract section 4.2.

Pure module: no DB, no network, no route. A canonical key is the triplet
``(key_version, language, key)``; its text form, used in logs and for
invalidation, is ``"{key_version}|{language}|{key}"``.

    family  key                                text form
    match   make_key(type, title, year)        1|fr-FR|movie|matrix|1999
    movie   movie|{tmdb_id}                    1|fr-FR|movie|603
    show    show|{tmdb_id}                     1|en-US|show|1396
    season  season|{show_id}|{n}               1|fr-FR|season|1396|0
    omdb    omdb|{imdb_id}   (language "-")    1|-|omdb|tt0133093
    find    find|{source}|{id} (language "-")  1|-|find|imdb|tt0133093

`match` keys are built by `scrape_cache_service.make_key` itself (imported, never
copied): any evolution of `normalize_for_sorting` goes through that single point
and MUST be paired with a `KEY_VERSION` bump.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.config import DEFAULT_SCRAPE_ALLOWED_LANGUAGES, parse_language_list, settings
from app.services import scrape_cache_service
from app.utils.string_normalizer import normalize_for_sorting

# Declared here and nowhere else (ADR 0009 D2.2).
KEY_VERSION = 1
SCHEMA_VERSION = 1

FAMILIES = ("match", "movie", "show", "season", "omdb", "find")
MEDIA_TYPES = ("movie", "show")
FIND_SOURCES = ("imdb", "tvdb")

NO_LANGUAGE = "-"

# `[0-9]` and `fullmatch` on purpose: `\d` accepts non-ASCII digits and `$`
# accepts a trailing newline, both of which would let a malformed key through.
_VERSION_RE = re.compile(r"[1-9][0-9]{0,8}")
_LANGUAGE_RE = re.compile(r"[a-z]{2}-[A-Z]{2}")
_TMDB_ID_RE = re.compile(r"[1-9][0-9]{0,8}")  # 1..999 999 999 (FR-015b)
_SEASON_RE = re.compile(r"0|[1-9][0-9]{0,2}")  # 0..999
_IMDB_ID_RE = re.compile(r"tt[0-9]{7,10}")
_TVDB_ID_RE = re.compile(r"[1-9][0-9]{0,11}")
_TVDB_RAW_RE = re.compile(r"[0-9]{1,12}")  # client form, leading zeros allowed (X19)
_YEAR_RE = re.compile(r"[1-9][0-9]{0,3}")


class InvalidCacheKey(ValueError):
    """A key (or one of its parts) does not follow the canonical form."""


class EmptyNormalizedTitle(InvalidCacheKey):
    """`normalize_for_sorting(title)` is empty: the match key would be shared by
    every punctuation-only title ("movie||1999"), so it is refused (FR-007)."""


@dataclass(frozen=True)
class CacheKey:
    family: str
    key_version: int
    language: str
    key: str

    def __post_init__(self) -> None:
        _validate(self.family, self.key_version, self.language, self.key)

    def to_text(self) -> str:
        return f"{self.key_version}|{self.language}|{self.key}"


def validate_language(value: str) -> str:
    """Return `value` if it is a ``xx-XX`` language tag, else raise InvalidCacheKey."""
    if not isinstance(value, str) or _LANGUAGE_RE.fullmatch(value) is None:
        raise InvalidCacheKey("language must match ^[a-z]{2}-[A-Z]{2}$")
    return value


def _validate(family: str, key_version: int, language: str, key: str) -> None:
    if family not in FAMILIES:
        raise InvalidCacheKey(f"unknown family: {family!r}")
    if (
        not isinstance(key_version, int)
        or isinstance(key_version, bool)
        or _VERSION_RE.fullmatch(str(key_version)) is None
    ):
        raise InvalidCacheKey("key_version must be a positive integer")
    if not isinstance(key, str):
        raise InvalidCacheKey("key must be a string")

    if family in ("omdb", "find"):
        if language != NO_LANGUAGE:
            raise InvalidCacheKey(f"language must be {NO_LANGUAGE!r} for {family}")
    else:
        validate_language(language)

    parts = key.split("|")  # never filter empty segments (X12, contract 4.2)
    if family == "match":
        if len(parts) != 3 or parts[0] not in MEDIA_TYPES:
            raise InvalidCacheKey("match key must be 'movie|show' + title + year")
        title, year = parts[1], parts[2]
        if not title or title != title.strip():
            raise InvalidCacheKey("match key has an empty or untrimmed title")
        if year != "" and _YEAR_RE.fullmatch(year) is None:
            raise InvalidCacheKey("match key year must be empty or 1..9999")
    elif family in ("movie", "show"):
        if len(parts) != 2 or parts[0] != family or _TMDB_ID_RE.fullmatch(parts[1]) is None:
            raise InvalidCacheKey(f"{family} key must be '{family}|<tmdbId>'")
    elif family == "season":
        if (
            len(parts) != 3
            or parts[0] != "season"
            or _TMDB_ID_RE.fullmatch(parts[1]) is None
            or _SEASON_RE.fullmatch(parts[2]) is None
        ):
            raise InvalidCacheKey("season key must be 'season|<tmdbId>|<0..999>'")
    elif family == "omdb":
        if len(parts) != 2 or parts[0] != "omdb" or _IMDB_ID_RE.fullmatch(parts[1]) is None:
            raise InvalidCacheKey("omdb key must be 'omdb|tt<7..10 digits>'")
    else:  # find
        if len(parts) != 3 or parts[0] != "find" or parts[1] not in FIND_SOURCES:
            raise InvalidCacheKey("find key must be 'find|imdb|<id>' or 'find|tvdb|<id>'")
        id_re = _IMDB_ID_RE if parts[1] == "imdb" else _TVDB_ID_RE
        if id_re.fullmatch(parts[2]) is None:
            raise InvalidCacheKey(f"find key has a malformed {parts[1]} id")


def parse(text: str) -> CacheKey:
    """Parse a text form (contract 4.2) into a `CacheKey`, or raise InvalidCacheKey.

    The version is accepted for any positive integer: reading filters on
    `KEY_VERSION`, parsing must still be able to name a foreign-version key."""
    if not isinstance(text, str):
        raise InvalidCacheKey("key must be a string")
    parts = text.split("|")  # no filtering of empty segments
    if len(parts) < 4:
        raise InvalidCacheKey("a key has at least 4 segments")
    version_s, language, rest = parts[0], parts[1], parts[2:]
    if _VERSION_RE.fullmatch(version_s) is None:
        raise InvalidCacheKey("key_version must be a positive integer")
    head = rest[0]
    if head in ("season", "omdb", "find"):
        family = head
    elif head in MEDIA_TYPES:
        # 5 segments in all (version|lang|type|title|year) = match, 4 = details.
        if len(rest) == 3:
            family = "match"
        elif len(rest) == 2:
            family = head
        else:
            raise InvalidCacheKey("malformed movie/show key")
    else:
        raise InvalidCacheKey(f"unknown key family: {head!r}")
    return CacheKey(family, int(version_s), language, "|".join(rest))


def match_key(media_type: str, title: str, year: int | None, language: str) -> CacheKey:
    """Title match key: exactly `scrape_cache_service.make_key`, no re-cleaning."""
    if media_type not in MEDIA_TYPES:
        raise InvalidCacheKey("media_type must be 'movie' or 'show'")
    if year is not None and (
        isinstance(year, bool) or not isinstance(year, int) or not 1 <= year <= 9999
    ):
        raise InvalidCacheKey("year must be None or 1..9999")
    validate_language(language)
    if not normalize_for_sorting(title):
        raise EmptyNormalizedTitle("title normalizes to an empty string")
    key = scrape_cache_service.make_key(media_type, title, year)
    return CacheKey("match", KEY_VERSION, language, key)


def movie_key(tmdb_id: int, language: str) -> CacheKey:
    return CacheKey("movie", KEY_VERSION, language, f"movie|{_int(tmdb_id, 'tmdb_id')}")


def show_key(tmdb_id: int, language: str) -> CacheKey:
    return CacheKey("show", KEY_VERSION, language, f"show|{_int(tmdb_id, 'tmdb_id')}")


def season_key(show_id: int, season_number: int, language: str) -> CacheKey:
    key = f"season|{_int(show_id, 'show_id')}|{_int(season_number, 'season_number')}"
    return CacheKey("season", KEY_VERSION, language, key)


def omdb_key(imdb_id: str) -> CacheKey:
    return CacheKey("omdb", KEY_VERSION, NO_LANGUAGE, f"omdb|{imdb_id}")


def find_key(source: str, external_id: str) -> CacheKey:
    """`find` key; a tvdb id is normalised to an integer ("007" -> "7", X19)."""
    if source == "tvdb":
        # Full ASCII match BEFORE `int()` (B1-07 review): `int()` would accept
        # " 7", "+7", "1_000", non-ASCII digits and a bool.
        if not isinstance(external_id, str) or _TVDB_RAW_RE.fullmatch(external_id) is None:
            raise InvalidCacheKey("tvdb id must be 1 to 12 ASCII digits")
        external_id = str(int(external_id))
    return CacheKey("find", KEY_VERSION, NO_LANGUAGE, f"find|{source}|{external_id}")


def _int(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidCacheKey(f"{name} must be an integer")
    return value


# --- Bounded gateway settings (ADR 0009 D2.3, FR-012/FR-033/FR-034) ----------
# The config only reads; clamping lives here (same split as the EPG sync).

_DAY_MS = 86_400_000

TTL_SUCCESS_DAYS_RANGE = (1, 365)
TTL_NEGATIVE_DAYS_RANGE = (1, 30)
REQUEST_TIMEOUT_SECONDS_RANGE = (5, 28)


def _clamp(value: int, bounds: tuple[int, int]) -> int:
    low, high = bounds
    return max(low, min(high, int(value)))


def ttl_success_ms() -> int:
    """TTL of a success entry (matched/found), in ms, bounded to 1..365 days."""
    return _clamp(settings.SCRAPE_TTL_SUCCESS_DAYS, TTL_SUCCESS_DAYS_RANGE) * _DAY_MS


def ttl_negative_ms() -> int:
    """TTL of a negative entry (ambiguous/nomatch/not_found), in ms, 1..30 days."""
    return _clamp(settings.SCRAPE_TTL_NEGATIVE_DAYS, TTL_NEGATIVE_DAYS_RANGE) * _DAY_MS


def request_timeout_seconds() -> int:
    """Overall deadline of one gateway request, bounded to 5..28 seconds."""
    return _clamp(settings.SCRAPE_REQUEST_TIMEOUT_SECONDS, REQUEST_TIMEOUT_SECONDS_RANGE)


def rate_limit_per_minute() -> int:
    """Requests per minute per API key; 0 means disabled (any value <= 0)."""
    return max(0, int(settings.SCRAPE_RATE_LIMIT_PER_MINUTE))


def allowed_languages() -> tuple[str, ...]:
    """Languages a gateway client may ask for (`SCRAPE_ALLOWED_LANGUAGES`).

    Invalid elements are dropped (the WARNING is logged once, at boot, by
    `Settings`); an empty result falls back to the default `fr-FR,en-US`.
    `TMDB_LANGUAGE` is NOT part of this tuple: the caller accepts it on top
    (it is the default when the parameter is absent)."""
    valid, _ = parse_language_list(settings.SCRAPE_ALLOWED_LANGUAGES)
    return valid or DEFAULT_SCRAPE_ALLOWED_LANGUAGES
