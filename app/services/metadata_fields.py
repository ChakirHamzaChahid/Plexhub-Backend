"""Conversions between `media` columns and the typed API fields, plus the
validation/normalisation of a write (contract B2 § 6.2). PURE: no I/O.

Pivot representation: a *flat* dict keyed by the dotted API names of
`metadata_columns.FIELDS_BY_TYPE` (`ids.imdb`, `ratings.tmdb`…) with typed
values (lists, floats, `{"value", "votes"}`, cast entries). The nested camelCase
shape of the API (`ids: {…}`, `ratings: {…}`) is only produced by
`flat_to_nested` at the edge.

Only the columns of the fields that really change are ever written
(`field_columns`), so a lossy read (e.g. `cast` synthesised from the CSV of names
when `cast_json` is absent) never rewrites an untouched column.
"""
from __future__ import annotations

import json
import math
import re
import unicodedata
from collections.abc import Mapping
from datetime import date
from typing import Any
from urllib.parse import urlparse

from app.services.metadata_columns import FIELD_COLUMNS, FIELDS_BY_TYPE, LIMITS
from app.utils.string_normalizer import normalize_for_sorting

# Same expressions as the NFO import (`nfo_import_service`): one definition of "a
# valid id" for every path that writes ids.
_IMDB_RE = re.compile(r"^tt\d{7,10}$")
_TMDB_RE = re.compile(r"^\d{1,9}$")
_TVDB_RE = re.compile(r"^\d{1,12}$")
_WIKIDATA_RE = re.compile(r"^Q\d+$")
_YOUTUBE_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")  # utils.youtube.YOUTUBE_ID_RE
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_FLOAT_TOLERANCE = 1e-9
_CAST_KEYS = ("name", "role", "thumb", "profile", "tvdbId")


class FieldError(ValueError):
    """A value refused by the validation => HTTP 400 `invalid_field` (`field` is the
    dotted API name)."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(f"{field}: {message}")
        self.field = field
        self.message = message


# ─── columns -> flat fields ─────────────────────────────────────────────────


def _s(value: Any) -> str | None:
    if value is None:
        return None
    text_value = str(value).strip()
    return text_value or None


def _csv(value: Any) -> list[str]:
    if value is None:
        return []
    seen: dict[str, None] = {}
    for part in str(value).split(","):
        item = part.strip()
        if item:
            seen.setdefault(item, None)
    return list(seen)


def _rating(value: Any, votes: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    return {"value": float(value), "votes": int(votes) if votes is not None else None}


def _cast_entry(raw: Mapping[str, Any]) -> dict[str, Any] | None:
    name = _s(raw.get("name"))
    if not name:
        return None
    tvdb = raw.get("tvdbId", raw.get("tvdbid"))
    return {
        "name": name,
        "role": _s(raw.get("role")),
        "thumb": _s(raw.get("thumb")),
        "profile": _s(raw.get("profile")),
        "tvdbId": _s(tvdb),
    }


def _read_cast(cast_csv: Any, cast_json: Any) -> list[dict[str, Any]]:
    if cast_json:
        try:
            parsed = json.loads(cast_json)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, list):
            entries = [e for e in (_cast_entry(x) for x in parsed if isinstance(x, Mapping)) if e]
            if entries:
                return entries
    return [
        {"name": name, "role": None, "thumb": None, "profile": None, "tvdbId": None}
        for name in _csv(cast_csv)
    ]


def columns_to_flat(cols: Mapping[str, Any], type_: str) -> dict[str, Any]:
    """Every field of `type_` (a missing value reads as None / [])."""
    full: dict[str, Any] = {
        "title": _s(cols.get("title")),
        "originalTitle": _s(cols.get("original_title")),
        "summary": _s(cols.get("summary")),
        "tagline": _s(cols.get("tagline")),
        "genres": _csv(cols.get("genres")),
        "studios": _csv(cols.get("studio")),
        "countries": _csv(cols.get("country")),
        "contentRating": _s(cols.get("content_rating")),
        "year": cols.get("year"),
        "premiered": _s(cols.get("premiered")),
        "status": _s(cols.get("status")),
        "durationMs": cols.get("duration"),
        "ids.imdb": _s(cols.get("imdb_id")),
        "ids.tmdb": _s(cols.get("tmdb_id")),
        "ids.tvdb": _s(cols.get("tvdb_id")),
        "ids.wikidata": _s(cols.get("wikidata_id")),
        "ratings.imdb": _rating(cols.get("imdb_rating"), cols.get("imdb_votes")),
        "ratings.tmdb": _rating(cols.get("tmdb_rating"), cols.get("tmdb_votes")),
        "cast": _read_cast(cols.get("cast"), cols.get("cast_json")),
        "youtubeTrailer": _s(cols.get("youtube_trailer")),
    }
    return {name: full[name] for name in FIELDS_BY_TYPE[type_]}


def flat_to_nested(flat: Mapping[str, Any]) -> dict[str, Any]:
    """API shape: `ids` and `ratings` grouped; every other key as is. `ids` is
    complete whenever ANY id field is present (all four keys, null when empty)."""
    nested: dict[str, Any] = {}
    ids: dict[str, Any] = {}
    ratings: dict[str, Any] = {}
    for name, value in flat.items():
        if name.startswith("ids."):
            ids[name[4:]] = value
        elif name.startswith("ratings."):
            ratings[name[8:]] = value
        else:
            nested[name] = value
    if ids:
        nested["ids"] = {k: ids.get(k) for k in ("imdb", "tmdb", "tvdb", "wikidata")}
    if ratings:
        nested["ratings"] = {k: ratings.get(k) for k in ("imdb", "tmdb")}
    return nested


# ─── validation / normalisation of a write ──────────────────────────────────


def _has_control_chars(value: str, *, allow_newlines: bool) -> bool:
    for ch in value:
        if unicodedata.category(ch) == "Cc" and not (allow_newlines and ch in "\n\r\t"):
            return True
    return False


def _text(name: str, value: Any, *, nullable: bool, lo: int, hi: int, newlines: bool = False) -> str | None:
    if value is None:
        if nullable:
            return None
        raise FieldError(name, "must not be null")
    if not isinstance(value, str):
        raise FieldError(name, "must be a string")
    cleaned = value.strip()
    if _has_control_chars(cleaned, allow_newlines=newlines):
        raise FieldError(name, "contains a control character")
    if not lo <= len(cleaned) <= hi:
        raise FieldError(name, f"length must be between {lo} and {hi}")
    return cleaned


def _list(name: str, value: Any) -> list[str]:
    limits = LIMITS[name]
    if value is None:
        return []
    if not isinstance(value, list):
        raise FieldError(name, "must be a list")
    if len(value) > limits["maxItems"]:  # type: ignore[operator]
        raise FieldError(name, f"at most {limits['maxItems']} items")
    out: dict[str, None] = {}
    for raw in value:
        item = _text(name, raw, nullable=False, lo=limits["itemMin"], hi=limits["itemMax"])  # type: ignore[arg-type]
        if "," in item:  # type: ignore[operator]
            raise FieldError(name, "an item must not contain a comma")
        out.setdefault(item, None)  # type: ignore[arg-type]
    return list(out)


def _int(name: str, value: Any, lo: int, hi: int) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise FieldError(name, "must be an integer")
    if not lo <= value <= hi:
        raise FieldError(name, f"must be between {lo} and {hi}")
    return value


def _pattern(name: str, value: Any, regex: re.Pattern[str]) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not regex.match(value.strip()):
        raise FieldError(name, "invalid format")
    return value.strip()


def _premiered(name: str, value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _ISO_DATE_RE.match(value.strip()):
        raise FieldError(name, "must be YYYY-MM-DD")
    try:
        date.fromisoformat(value.strip())
    except ValueError:
        raise FieldError(name, "not a valid date") from None
    return value.strip()


def _rating_value(name: str, value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) - {"value", "votes"} or "value" not in value:
        raise FieldError(name, "must be {value, votes} or null")
    raw, votes = value["value"], value.get("votes")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
        raise FieldError(name, "value must be a finite number")
    if not 0 <= raw <= 10:
        raise FieldError(name, "value must be between 0 and 10")
    if votes is not None:
        if isinstance(votes, bool) or not isinstance(votes, int) or votes < 0:
            raise FieldError(name, "votes must be an integer >= 0")
    return {"value": float(raw), "votes": votes}


def _https_url(name: str, value: Any, hi: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > hi or any(c.isspace() for c in value):
        raise FieldError(name, "invalid URL")
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise FieldError(name, "URL must be https")
    return value


def _cast(name: str, value: Any) -> list[dict[str, Any]]:
    limits = LIMITS["cast"]
    if value is None:
        return []
    if not isinstance(value, list):
        raise FieldError(name, "must be a list")
    if len(value) > limits["maxItems"]:  # type: ignore[operator]
        raise FieldError(name, f"at most {limits['maxItems']} entries")
    out: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, Mapping) or set(raw) - set(_CAST_KEYS):
            raise FieldError(name, "an entry must be {name, role, thumb, profile, tvdbId}")
        entry_name = _text(name, raw.get("name"), nullable=False, lo=limits["nameMin"], hi=limits["nameMax"])  # type: ignore[arg-type]
        role = raw.get("role")
        role = None if role is None else _text(name, role, nullable=False, lo=1, hi=limits["roleMax"])  # type: ignore[arg-type]
        tvdb = raw.get("tvdbId")
        if tvdb is not None and (not isinstance(tvdb, str) or not _TVDB_RE.match(tvdb)):
            raise FieldError(name, "tvdbId must be digits")
        out.append({
            "name": entry_name,
            "role": role,
            "thumb": _https_url(name, raw.get("thumb"), limits["urlMax"]),  # type: ignore[arg-type]
            "profile": _https_url(name, raw.get("profile"), limits["urlMax"]),  # type: ignore[arg-type]
            "tvdbId": tvdb,
        })
    return out


def _validate(name: str, value: Any) -> Any:
    lim = LIMITS.get(name, {})
    if name == "title":
        return _text(name, value, nullable=False, lo=1, hi=300)
    if name == "originalTitle":
        return _text(name, value, nullable=True, lo=1, hi=300)
    if name == "summary":
        return _text(name, value, nullable=True, lo=1, hi=10_000, newlines=True)
    if name == "tagline":
        return _text(name, value, nullable=True, lo=1, hi=500, newlines=True)
    if name in ("genres", "studios", "countries"):
        return _list(name, value)
    if name == "contentRating":
        return _text(name, value, nullable=True, lo=1, hi=32)
    if name == "year":
        return _int(name, value, lim["min"], lim["max"])  # type: ignore[arg-type]
    if name == "premiered":
        return _premiered(name, value)
    if name == "status":
        return _text(name, value, nullable=True, lo=1, hi=64)
    if name == "durationMs":
        return _int(name, value, lim["min"], lim["max"])  # type: ignore[arg-type]
    if name == "ids.imdb":
        return _pattern(name, value, _IMDB_RE)
    if name == "ids.tmdb":
        return _pattern(name, value, _TMDB_RE)
    if name == "ids.tvdb":
        return _pattern(name, value, _TVDB_RE)
    if name == "ids.wikidata":
        return _pattern(name, value, _WIKIDATA_RE)
    if name in ("ratings.imdb", "ratings.tmdb"):
        return _rating_value(name, value)
    if name == "cast":
        return _cast(name, value)
    if name == "youtubeTrailer":
        return _pattern(name, value, _YOUTUBE_RE)
    raise FieldError(name, "unknown field")  # pragma: no cover - guarded by caller


def normalize_patch(patch: Mapping[str, Any], type_: str, *, is_adult: bool) -> dict[str, Any]:
    """Validate and normalise the `fields` object of a write; return the flat dict of
    the fields that were PRESENT (an absent key means "unchanged", `None` clears)."""
    allowed = set(FIELDS_BY_TYPE[type_])
    flat_in: dict[str, Any] = {}
    for key, value in patch.items():
        if key in ("ids", "ratings"):
            if not isinstance(value, Mapping):
                raise FieldError(key, "must be an object")
            for sub, sub_value in value.items():
                flat_in[f"{key}.{sub}"] = sub_value
        else:
            flat_in[key] = value

    out: dict[str, Any] = {}
    for name, value in flat_in.items():
        if name not in FIELD_COLUMNS:
            raise FieldError(name, "unknown field")
        if name not in allowed:
            raise FieldError(name, f"not writable for type {type_}")
        if name == "contentRating" and is_adult:
            raise FieldError(name, "the adult classification is never replaced")
        out[name] = _validate(name, value)
    return out


def apply_patch(flat: Mapping[str, Any], patch_norm: Mapping[str, Any]) -> dict[str, Any]:
    return {**flat, **patch_norm}


# ─── equality / diff ────────────────────────────────────────────────────────


def _eq(a: Any, b: Any) -> bool:
    if isinstance(a, Mapping) and isinstance(b, Mapping):
        return set(a) == set(b) and all(_eq(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_eq(x, y) for x, y in zip(a, b))
    if (
        isinstance(a, (int, float)) and isinstance(b, (int, float))
        and not isinstance(a, bool) and not isinstance(b, bool)
    ):
        return abs(a - b) <= _FLOAT_TOLERANCE
    return a == b


def values_equal(a: Any, b: Any) -> bool:
    """Equality after normalisation (ratings compared to 1e-9)."""
    return _eq(a, b)


def changed_field_names(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[str]:
    names = set(before) | set(after)
    return sorted(n for n in names if not _eq(before.get(n), after.get(n)))


# ─── flat fields -> columns ─────────────────────────────────────────────────


def field_columns(name: str, flat: Mapping[str, Any]) -> dict[str, Any]:
    """The `media` columns to set for ONE field, from its (normalised) value."""
    value = flat[name]
    if name == "title":
        return {"title": value, "title_sortable": normalize_for_sorting(value).lower()}
    if name in ("genres", "studios", "countries"):
        column = FIELD_COLUMNS[name][0]
        return {column: ", ".join(value) if value else None}
    if name in ("ratings.imdb", "ratings.tmdb"):
        value_col, votes_col = FIELD_COLUMNS[name]
        if value is None:
            return {value_col: None, votes_col: None}
        return {value_col: value["value"], votes_col: value["votes"]}
    if name == "cast":
        if not value:
            return {"cast": None, "cast_json": None}
        names = list(dict.fromkeys(entry["name"] for entry in value))
        stored = [
            {
                "name": e["name"], "role": e["role"], "thumb": e["thumb"],
                "profile": e["profile"], "tvdbid": e["tvdbId"],
            }
            for e in value
        ]
        return {"cast": ", ".join(names), "cast_json": json.dumps(stored, ensure_ascii=False)}
    (column,) = FIELD_COLUMNS[name]
    return {column: value}
