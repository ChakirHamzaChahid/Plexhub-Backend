"""Pure mappers of the scraping gateway: raw TMDB / OMDb JSON -> camelCase
dicts that validate against `app.models.scrape_schemas` (lot B1, B1-09).

No I/O, no DB, no clock: every function maps its arguments to a fresh value.
Contract: `15-contrat-b1-backend.md` sections 4.5 and 6; PRD FR-025..FR-028;
ADR 0009 D2.4, D2.9, D2.10.

Rules applied everywhere
------------------------
* absent -> ``None`` (never ``""``): strings are stripped, empty becomes None;
* durations of ``0`` -> ``None``;
* images are RAW TMDB paths (``/abc.jpg``) next to the constant
  ``IMAGE_BASE_URL``; lists are sorted by language rank (requested short
  language, ``en``, untagged, then any other) keeping TMDB order inside a
  rank (stable sort);
* lists are bounded (see the ``MAX_*`` constants) and ordered
  deterministically; people are de-duplicated on ``(tmdbPersonId, job)``
  (cast: ``(tmdbPersonId, character)``, a cast entry has no job);
* external ids go through the NFO importer validators (imported, not copied);
* ``contentRating`` and the trailer use the existing worker functions of
  ``tmdb_service`` (region derived from ``language``).

Cache form (ADR D2.4)
---------------------
``map_movie`` / ``map_show`` return the TMDB-only details: ``ratings`` holds
the TMDB rating alone and there is NO ``omdbStatus`` key. ``with_omdb``
composes the response form (OMDb ratings + ``omdbStatus``).

A payload that lacks a field the contract requires (the TMDB ``id``, a
season number) raises ``ScrapeMappingError``; the service translates it to
``502 upstream_error``.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import Any

from app.services.nfo_import_service import (
    _validate_imdb,
    _validate_tvdb,
    _validate_wikidata,
)
from app.services.omdb_service import OmdbRatings
from app.services.tmdb_service import (
    GatewayCandidate,
    _parse_movie_certification,
    _parse_tv_certification,
    _select_youtube_trailer,
)

IMAGE_BASE_URL = "https://image.tmdb.org/t/p/"

# Contract 4.5 / 6 bounds.
MAX_CAST = 30
MAX_CREW = 30
MAX_POSTERS = 20
MAX_BACKDROPS = 20
MAX_LOGOS = 10
MAX_EPISODES = 500
MAX_GUEST_STARS = 20
MAX_EPISODE_CREW = 10
MAX_STUDIOS = 5
MAX_MATCHES = 5
MAX_CANDIDATES = 10
# `seasonNumber` is bounded to 0..999 on the route, so a show has <= 1000
# seasons: not a truncation in practice, a ceiling on the output size.
MAX_SEASONS = 1000

# Contract 6.1: the crew kept for a movie (and the "main team" of a show).
MAIN_CREW_JOBS = frozenset({"Director", "Screenplay", "Writer", "Story", "Novel", "Author"})
# Contract 6.3: the crew kept for an episode.
EPISODE_CREW_JOBS = frozenset({"Director", "Writer", "Screenplay"})

OMDB_STATUSES = frozenset(
    {"ok", "not_configured", "budget_exhausted", "not_found", "error", "no_imdb_id"}
)
# Contract 6.4: order of the merged list.
_RATING_ORDER = {"tmdb": 0, "imdb": 1, "metacritic": 2, "rottenTomatoes": 3}

_ISO_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


class ScrapeMappingError(ValueError):
    """The provider payload lacks a field the contract requires."""


# --- Scalar helpers ---------------------------------------------------------

def _s(value: Any) -> str | None:
    """Stripped non-empty string, else None."""
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return None


def _int(value: Any) -> int | None:
    """int (a bool is not one), else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _positive_int(value: Any) -> int | None:
    """Duration-like value: a number > 0 as int, else None (0 -> None)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if value > 0 else None


def _date(value: Any) -> str | None:
    """`YYYY-MM-DD` or None (TMDB sends "" when unknown)."""
    text = _s(value)
    return text if text is not None and _ISO_DATE_RE.fullmatch(text) else None


def _year(date: str | None) -> int | None:
    if date is None:
        return None
    year = int(date[:4])
    return year if year >= 1 else None


def _region(language: str) -> str | None:
    """`fr-FR` -> `FR`; no `-` -> None (the worker default applies)."""
    if "-" in (language or ""):
        return language.rsplit("-", 1)[-1].upper() or None
    return None


def _short_lang(language: str) -> str:
    return (language or "").split("-", 1)[0].strip().lower()


def _external_id(validator: Callable[[str | None], str | None], value: Any) -> str | None:
    """Validate a provider id with the NFO importer rule. ASCII only: the
    importer regexes use `\\d`, which would also accept other scripts' digits."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        value = str(value) if value > 0 else None
    if not isinstance(value, str) or not value.strip().isascii():
        return None
    return validator(value)


def _unique_names(items: Iterable[Any], key: str, limit: int | None = None) -> list[str]:
    """Non-empty `key` of each dict, de-duplicated, order kept, bounded."""
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = _s(item.get(key))
        if name is None or name in seen:
            continue
        seen.add(name)
        out.append(name)
        if limit is not None and len(out) >= limit:
            break
    return out


def _dicts(value: Any) -> list[dict]:
    return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []


# --- Ratings ----------------------------------------------------------------

def map_tmdb_rating(raw: dict) -> dict | None:
    """TMDB `vote_average`/`vote_count` -> a `tmdb` Rating, omitted when there
    is no vote (contract 6.4)."""
    average = raw.get("vote_average")
    count = raw.get("vote_count")
    if isinstance(average, bool) or not isinstance(average, (int, float)):
        return None
    if isinstance(count, bool) or not isinstance(count, (int, float)) or count <= 0:
        return None
    if not 0 <= average <= 10:
        return None
    return {"source": "tmdb", "value": float(average), "max": 10.0, "votes": int(count)}


def map_omdb_ratings(omdb: OmdbRatings | None) -> list[dict]:
    """`OmdbRatings` -> Rating dicts, order imdb, metacritic, rottenTomatoes.

    IMDb 0..10 over 10 with its vote count; Metacritic and Rotten Tomatoes
    0..100 over 100 with `votes: null`. A missing or out-of-range value is
    omitted."""
    if omdb is None:
        return []
    out: list[dict] = []
    imdb = omdb.imdb_rating
    if isinstance(imdb, (int, float)) and not isinstance(imdb, bool) and 0 <= imdb <= 10:
        votes = omdb.imdb_votes if isinstance(omdb.imdb_votes, int) and omdb.imdb_votes >= 0 else None
        out.append({"source": "imdb", "value": float(imdb), "max": 10.0, "votes": votes})
    for source, score in (("metacritic", omdb.metacritic), ("rottenTomatoes", omdb.rotten_tomatoes)):
        if isinstance(score, int) and not isinstance(score, bool) and 0 <= score <= 100:
            out.append({"source": source, "value": float(score), "max": 100.0, "votes": None})
    return out


def _as_rating_dicts(omdb: OmdbRatings | Iterable[dict] | None) -> list[dict]:
    if omdb is None:
        return []
    if isinstance(omdb, OmdbRatings):
        return map_omdb_ratings(omdb)
    return [dict(r) for r in omdb if isinstance(r, dict)]


def compose_ratings(
    tmdb_rating: dict | None, omdb_ratings: OmdbRatings | Iterable[dict] | None,
) -> list[dict]:
    """Merge the TMDB rating and the OMDb ratings in the contract order
    (tmdb, imdb, metacritic, rottenTomatoes). `omdb_ratings` is either the raw
    `OmdbRatings` or the already normalised Rating dicts (the cached form).
    One entry per source (first wins); a TMDB rating with `votes == 0` is
    dropped."""
    merged: list[dict] = []
    if tmdb_rating is not None and (_int(tmdb_rating.get("votes")) or 0) > 0:
        merged.append(dict(tmdb_rating))
    merged.extend(_as_rating_dicts(omdb_ratings))
    seen: set[str] = set()
    unique: list[dict] = []
    for rating in merged:
        source = rating.get("source")
        if source in seen or source not in _RATING_ORDER:
            continue
        seen.add(source)
        unique.append(rating)
    unique.sort(key=lambda r: _RATING_ORDER[r["source"]])
    return unique


def with_omdb(
    details: dict, omdb_ratings: OmdbRatings | Iterable[dict] | None, omdb_status: str,
) -> dict:
    """Compose a movie/show response body from the cached TMDB-only details.

    Returns a NEW dict (the cached input is never mutated) whose `ratings` is
    the TMDB rating merged with the OMDb ones and whose `omdbStatus` is set.
    OMDb ratings are only merged when the status is `ok`: any other status
    means "no OMDb data" (ADR D2.10)."""
    if omdb_status not in OMDB_STATUSES:
        raise ValueError(f"unknown omdb status: {omdb_status!r}")
    tmdb_rating = next(
        (r for r in details.get("ratings") or [] if r.get("source") == "tmdb"), None,
    )
    composed = dict(details)
    composed["ratings"] = compose_ratings(
        tmdb_rating, omdb_ratings if omdb_status == "ok" else None,
    )
    composed["omdbStatus"] = omdb_status
    return composed


# --- People -----------------------------------------------------------------

def _cast_member(raw: dict, index: int) -> dict | None:
    person_id = _int(raw.get("id"))
    name = _s(raw.get("name"))
    if person_id is None or name is None:
        return None
    order = _int(raw.get("order"))
    return {
        "name": name,
        "character": _s(raw.get("character")),
        "order": order if order is not None else index,
        "profilePath": _s(raw.get("profile_path")),
        "tmdbPersonId": person_id,
    }


def _map_cast(raw_cast: Any, limit: int) -> list[dict]:
    """TMDB order kept; one entry per (person, character)."""
    out: list[dict] = []
    seen: set[tuple[int, str | None]] = set()
    for index, raw in enumerate(_dicts(raw_cast)):
        member = _cast_member(raw, index)
        if member is None:
            continue
        key = (member["tmdbPersonId"], member["character"])
        if key in seen:
            continue
        seen.add(key)
        out.append(member)
        if len(out) >= limit:
            break
    return out


def _crew_member(raw: dict, job: str | None = None) -> dict | None:
    person_id = _int(raw.get("id"))
    name = _s(raw.get("name"))
    job = job if job is not None else _s(raw.get("job"))
    if person_id is None or name is None or job is None:
        return None
    return {
        "name": name,
        "job": job,
        "department": _s(raw.get("department")),
        "profilePath": _s(raw.get("profile_path")),
        "tmdbPersonId": person_id,
    }


def _map_crew(
    entries: Iterable[dict], jobs: frozenset[str], limit: int, *, seen: set | None = None,
) -> list[dict]:
    """Crew whose job is in `jobs`, TMDB order, one entry per (person, job)."""
    out: list[dict] = []
    seen = seen if seen is not None else set()
    if limit <= 0:
        return out
    for raw in entries:
        if _s(raw.get("job")) not in jobs:
            continue
        member = _crew_member(raw)
        if member is None:
            continue
        key = (member["tmdbPersonId"], member["job"])
        if key in seen:
            continue
        seen.add(key)
        out.append(member)
        if len(out) >= limit:
            break
    return out


# --- Images -----------------------------------------------------------------

def _sorted_paths(entries: Any, language: str, limit: int) -> list[str]:
    """Raw `file_path`s ranked by language (requested short language, `en`,
    untagged, other), TMDB order inside a rank (stable), de-duplicated."""
    short = _short_lang(language)

    def rank(entry: dict) -> int:
        lang = _s(entry.get("iso_639_1"))
        lang = lang.lower() if lang else None
        if lang is None:
            return 2
        if lang == short:
            return 0
        if lang == "en":
            return 1
        return 3

    items = [e for e in _dicts(entries) if _s(e.get("file_path"))]
    items.sort(key=rank)
    out: list[str] = []
    seen: set[str] = set()
    for entry in items:
        path = _s(entry["file_path"])
        if path is None or path in seen:
            continue
        seen.add(path)
        out.append(path)
        if len(out) >= limit:
            break
    return out


def _map_images(raw: dict, language: str, *, backdrop: bool = True) -> dict:
    images = raw.get("images") if isinstance(raw.get("images"), dict) else {}
    return {
        "poster": _s(raw.get("poster_path")),
        "backdrop": _s(raw.get("backdrop_path")) if backdrop else None,
        "posters": _sorted_paths(images.get("posters"), language, MAX_POSTERS),
        "backdrops": _sorted_paths(images.get("backdrops"), language, MAX_BACKDROPS),
        "logos": _sorted_paths(images.get("logos"), language, MAX_LOGOS),
    }


# --- Common pieces of movie / show ------------------------------------------

def _require_id(raw: dict, what: str) -> int:
    tmdb_id = _int(raw.get("id"))
    if tmdb_id is None or tmdb_id < 1:
        raise ScrapeMappingError(f"{what}: missing or invalid id")
    return tmdb_id


def _map_external_ids(raw: dict) -> dict:
    ext = raw.get("external_ids") if isinstance(raw.get("external_ids"), dict) else {}
    return {
        "imdb": _external_id(_validate_imdb, ext.get("imdb_id") or raw.get("imdb_id")),
        "tvdb": _external_id(_validate_tvdb, ext.get("tvdb_id")),
        "wikidata": _external_id(_validate_wikidata, ext.get("wikidata_id")),
    }


def _map_trailer(raw: dict) -> dict | None:
    videos = raw.get("videos")
    key = _s(_select_youtube_trailer(videos if isinstance(videos, dict) else {}))
    return {"site": "YouTube", "key": key} if key else None


def _tmdb_ratings(raw: dict) -> list[dict]:
    rating = map_tmdb_rating(raw)
    return [rating] if rating is not None else []


def _credits(raw: dict) -> dict:
    return raw.get("credits") if isinstance(raw.get("credits"), dict) else {}


# --- Movie ------------------------------------------------------------------

def map_movie(raw: dict, *, language: str) -> dict:
    """`/movie/{id}` JSON -> `MovieDetails` WITHOUT the OMDb part (cache form,
    see the module docstring)."""
    tmdb_id = _require_id(raw, "movie")
    release_date = _date(raw.get("release_date"))
    collection = raw.get("belongs_to_collection")
    collection_out = None
    if isinstance(collection, dict) and _int(collection.get("id")) and _s(collection.get("name")):
        collection_out = {"tmdbId": collection["id"], "name": _s(collection["name"])}
    countries = _unique_names(_dicts(raw.get("production_countries")), "iso_3166_1")

    return {
        "tmdbId": tmdb_id,
        "title": _s(raw.get("title")),
        "originalTitle": _s(raw.get("original_title")),
        "overview": _s(raw.get("overview")),
        "tagline": _s(raw.get("tagline")),
        "releaseDate": release_date,
        "year": _year(release_date),
        "runtimeMinutes": _positive_int(raw.get("runtime")),
        "status": _s(raw.get("status")),
        "contentRating": _s(_parse_movie_certification(raw, _region(language))),
        "genres": _unique_names(_dicts(raw.get("genres")), "name"),
        "studios": _unique_names(
            _dicts(raw.get("production_companies")), "name", MAX_STUDIOS,
        ),
        "countries": countries,
        "collection": collection_out,
        "externalIds": _map_external_ids(raw),
        "ratings": _tmdb_ratings(raw),
        "cast": _map_cast(_credits(raw).get("cast"), MAX_CAST),
        "crew": _map_crew(_dicts(_credits(raw).get("crew")), MAIN_CREW_JOBS, MAX_CREW),
        "trailer": _map_trailer(raw),
        "images": _map_images(raw, language),
        "imageBaseUrl": IMAGE_BASE_URL,
    }


# --- Show -------------------------------------------------------------------

def _episode_runtime_minutes(raw: dict) -> int | None:
    values = raw.get("episode_run_time")
    if not isinstance(values, list):
        return None
    minutes = [
        int(v) for v in values
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 1
    ]
    return min(minutes) if minutes else None


def _map_season_summaries(raw_seasons: Any) -> list[dict]:
    by_number: dict[int, dict] = {}
    for entry in _dicts(raw_seasons):
        number = _int(entry.get("season_number"))
        if number is None or number < 0 or number in by_number:
            continue
        by_number[number] = {
            "seasonNumber": number,
            "name": _s(entry.get("name")),
            "airDate": _date(entry.get("air_date")),
            "episodeCount": _int(entry.get("episode_count")),
            "posterPath": _s(entry.get("poster_path")),
            "tmdbId": _int(entry.get("id")),
        }
    return [by_number[n] for n in sorted(by_number)][:MAX_SEASONS]


def map_show(raw: dict, *, language: str) -> dict:
    """`/tv/{id}` JSON -> `ShowDetails` WITHOUT the OMDb part (cache form)."""
    tmdb_id = _require_id(raw, "show")
    first_air_date = _date(raw.get("first_air_date"))

    # Studios: the networks, else the production companies. Countries: the
    # production countries (ISO codes), else `origin_country`.
    studios = _unique_names(_dicts(raw.get("networks")), "name", MAX_STUDIOS) or _unique_names(
        _dicts(raw.get("production_companies")), "name", MAX_STUDIOS,
    )
    countries = _unique_names(_dicts(raw.get("production_countries")), "iso_3166_1")
    if not countries:
        origin = raw.get("origin_country")
        countries = _unique_names(
            [{"c": c} for c in origin] if isinstance(origin, list) else [], "c",
        )

    # Crew: the creators first (job "Creator"), then the main team.
    seen: set = set()
    crew: list[dict] = []
    for creator in _dicts(raw.get("created_by")):
        member = _crew_member(creator, job="Creator")
        if member is None or (member["tmdbPersonId"], "Creator") in seen:
            continue
        seen.add((member["tmdbPersonId"], "Creator"))
        crew.append(member)
    crew = crew[:MAX_CREW]
    crew += _map_crew(
        _dicts(_credits(raw).get("crew")), MAIN_CREW_JOBS, MAX_CREW - len(crew), seen=seen,
    )

    return {
        "tmdbId": tmdb_id,
        "title": _s(raw.get("name")),
        "originalTitle": _s(raw.get("original_name")),
        "overview": _s(raw.get("overview")),
        "tagline": _s(raw.get("tagline")),
        "firstAirDate": first_air_date,
        "year": _year(first_air_date),
        "episodeRuntimeMinutes": _episode_runtime_minutes(raw),
        "status": _s(raw.get("status")),
        "contentRating": _s(_parse_tv_certification(raw, _region(language))),
        "genres": _unique_names(_dicts(raw.get("genres")), "name"),
        "studios": studios,
        "countries": countries,
        "externalIds": _map_external_ids(raw),
        "ratings": _tmdb_ratings(raw),
        "cast": _map_cast(_credits(raw).get("cast"), MAX_CAST),
        "crew": crew,
        "trailer": _map_trailer(raw),
        "images": _map_images(raw, language),
        "imageBaseUrl": IMAGE_BASE_URL,
        "numberOfSeasons": _int(raw.get("number_of_seasons")),
        "numberOfEpisodes": _int(raw.get("number_of_episodes")),
        "seasons": _map_season_summaries(raw.get("seasons")),
    }


# --- Season and episodes ----------------------------------------------------

def _map_episode(raw: dict) -> dict | None:
    tmdb_id = _int(raw.get("id"))
    number = _int(raw.get("episode_number"))
    season = _int(raw.get("season_number"))
    if tmdb_id is None or number is None or number < 1 or season is None:
        return None
    return {
        "tmdbId": tmdb_id,
        "episodeNumber": number,
        "seasonNumber": season,
        "name": _s(raw.get("name")),
        "overview": _s(raw.get("overview")),
        "airDate": _date(raw.get("air_date")),
        "runtimeMinutes": _positive_int(raw.get("runtime")),
        "stillPath": _s(raw.get("still_path")),
        "ratings": _tmdb_ratings(raw),
        "crew": _map_crew(_dicts(raw.get("crew")), EPISODE_CREW_JOBS, MAX_EPISODE_CREW),
        "guestStars": _map_cast(raw.get("guest_stars"), MAX_GUEST_STARS),
    }


def map_season(raw: dict, *, show_tmdb_id: int, language: str) -> dict:
    """`/tv/{id}/season/{n}` JSON -> `SeasonDetails` (episodes included).

    Episodes are sorted by `episodeNumber`, one per number (first wins),
    bounded to 500."""
    season_number = _int(raw.get("season_number"))
    if season_number is None or season_number < 0:
        raise ScrapeMappingError("season: missing or invalid season_number")
    tmdb_id = _require_id(raw, "season")

    by_number: dict[int, dict] = {}
    for entry in _dicts(raw.get("episodes")):
        episode = _map_episode(entry)
        if episode is not None and episode["episodeNumber"] not in by_number:
            by_number[episode["episodeNumber"]] = episode
    episodes = [by_number[n] for n in sorted(by_number)][:MAX_EPISODES]

    ext = raw.get("external_ids") if isinstance(raw.get("external_ids"), dict) else {}
    return {
        "showTmdbId": show_tmdb_id,
        "tmdbId": tmdb_id,
        "seasonNumber": season_number,
        "name": _s(raw.get("name")),
        "overview": _s(raw.get("overview")),
        "airDate": _date(raw.get("air_date")),
        "externalIds": {"tvdb": _external_id(_validate_tvdb, ext.get("tvdb_id"))},
        "images": _map_images(raw, language, backdrop=False),
        "imageBaseUrl": IMAGE_BASE_URL,
        "episodes": episodes,
    }


# --- Find and candidates ----------------------------------------------------

def map_find(raw: dict) -> list[dict]:
    """`find_external` JSON -> `FindMatch` dicts (<= 5): movies, then shows,
    then episodes, TMDB order inside each kind. Seasons and persons are not
    read; an episode without its show id, season or episode number is
    skipped."""
    matches: list[dict] = []
    seen: set[tuple[str, int]] = set()

    def add(entry: dict) -> None:
        key = (entry["mediaType"], entry["tmdbId"])
        if key not in seen and len(matches) < MAX_MATCHES:
            seen.add(key)
            matches.append(entry)

    for kind, field in (("movie", "movie_results"), ("show", "tv_results")):
        for item in _dicts(raw.get(field)):
            tmdb_id = _int(item.get("id"))
            if tmdb_id is not None and tmdb_id >= 1:
                add({"mediaType": kind, "tmdbId": tmdb_id, "showTmdbId": None,
                     "seasonNumber": None, "episodeNumber": None})
    for item in _dicts(raw.get("tv_episode_results")):
        tmdb_id = _int(item.get("id"))
        show_id = _int(item.get("show_id"))
        season = _int(item.get("season_number"))
        number = _int(item.get("episode_number"))
        if tmdb_id is None or show_id is None or season is None or number is None:
            continue
        if tmdb_id < 1 or show_id < 1 or number < 1:
            continue
        add({"mediaType": "episode", "tmdbId": tmdb_id, "showTmdbId": show_id,
             "seasonNumber": season, "episodeNumber": number})
    return matches


def map_candidates(candidates: Iterable[GatewayCandidate]) -> list[dict]:
    """`GatewayCandidate`s -> `Candidate` dicts: score descending then tmdbId
    ascending, one per (kind, tmdbId), at most 10, score clamped to 0..1. A
    hit with neither a title nor an original title is skipped (a card with no
    name is unusable)."""
    out: list[dict] = []
    seen: set[tuple[str, int]] = set()
    ordered = sorted(candidates, key=lambda c: (-c.score, c.tmdb_id))
    for cand in ordered:
        key = (cand.kind, cand.tmdb_id)
        title = _s(cand.title) or _s(cand.original_title)
        if key in seen or title is None:
            continue
        seen.add(key)
        year = _int(cand.year)
        out.append({
            "tmdbId": cand.tmdb_id,
            "mediaType": cand.kind,
            "title": title,
            "originalTitle": _s(cand.original_title),
            "year": year if year and year >= 1 else None,
            "overview": _s(cand.overview),
            "posterPath": _s(cand.poster_path),
            "score": min(1.0, max(0.0, float(cand.score))),
        })
        if len(out) >= MAX_CANDIDATES:
            break
    return out
