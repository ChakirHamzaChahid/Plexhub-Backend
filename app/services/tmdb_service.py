import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Literal, Optional

import httpx
from rapidfuzz import fuzz

from app.config import settings
from app.utils.string_normalizer import normalize_for_sorting
from app.utils.ttl_cache import TTLCache

logger = logging.getLogger("plexhub.tmdb")

_RETRY_DELAYS = (1, 2, 4)
_RETRYABLE = (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError)

POSTER_BASE = "https://image.tmdb.org/t/p/w342"
BACKDROP_BASE = "https://image.tmdb.org/t/p/w1280"
PROFILE_BASE = "https://image.tmdb.org/t/p/w185"
PERSON_URL_BASE = "https://www.themoviedb.org/person"

# Top-N actors kept for the `cast` / `cast_json` columns (mirrors NFO behaviour).
_CAST_LIMIT = 20

# Matching thresholds (transposed from PlexHubTV ScraperMatcher.kt).
TITLE_WEIGHT = 0.7
YEAR_WEIGHT = 0.3
AUTO_MATCH_THRESHOLD = 0.85   # min weighted confidence to auto-match
MIN_TITLE_SCORE = 0.90        # min title similarity to auto-match
MIN_MARGIN = 0.05             # min confidence gap vs 2nd candidate (anti-ambiguity)
# When title+year are strong but the top-2 are within MIN_MARGIN, the Xtream
# summary breaks the tie if one candidate's overview is clearly closer.
SUMMARY_TIEBREAK_MARGIN = 0.10
SUMMARY_MIN_SIM = 0.30

# Search results rarely change within a day; bounded to keep memory predictable.
_SEARCH_CACHE_SIZE = 5000
_SEARCH_CACHE_TTL = 24 * 3600  # 24 h

# Sentinel distinct from None (a real cached "no match" result).
_MISSING = object()


@dataclass
class TMDBMatch:
    tmdb_id: int
    title: str
    year: int | None
    confidence: float
    title_score: float = 0.0
    vote_count: int = 0


@dataclass
class TMDBSearchOutcome:
    """Result of a search+score. `match` is set only on auto-match; `best` is
    the top candidate regardless (kept so callers can record the best score
    even on ambiguous/nomatch for later manual review)."""
    result: str                 # "matched" | "ambiguous" | "nomatch"
    match: TMDBMatch | None = None
    best: TMDBMatch | None = None

    @property
    def confidence(self) -> float:
        return self.best.confidence if self.best else 0.0


@dataclass
class TMDBEnrichmentData:
    """Rich metadata from TMDB movie/{id} or tv/{id} with
    append_to_response=credits,external_ids,release_dates|content_ratings.

    The trailing fields mirror the NFO-imported columns (see nfo_import_service)
    so the TMDB enrichment path can populate them too. They carry defaults so
    that older scrape-cache payloads (written before these existed) still
    deserialize via ``TMDBEnrichmentData(**json.loads(payload))``.
    `imdb_rating`/`imdb_votes` are intentionally absent — TMDB does not expose
    IMDb scores (that needs OMDb); those columns stay NFO-only.
    """
    tmdb_id: int
    imdb_id: str | None
    overview: str | None
    poster_url: str | None
    backdrop_url: str | None
    vote_average: float | None
    genres: str | None  # comma-separated
    year: int | None
    cast: str | None  # comma-separated actor names
    # --- rich metadata (mirrors the NFO columns), all optional/back-compatible ---
    original_title: str | None = None
    tagline: str | None = None
    premiered: str | None = None      # ISO date "YYYY-MM-DD"
    status: str | None = None
    studio: str | None = None         # comma-separated
    country: str | None = None        # comma-separated
    content_rating: str | None = None
    tvdb_id: str | None = None
    wikidata_id: str | None = None
    tmdb_rating: float | None = None  # = vote_average, kept distinct per NFO schema
    tmdb_votes: int | None = None
    cast_json: str | None = None      # JSON [{name, role, thumb, profile}]


class TMDBService:
    BASE_URL = "https://api.themoviedb.org/3"

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None
        self._search_cache: TTLCache[tuple, TMDBSearchOutcome] = TTLCache(
            max_size=_SEARCH_CACHE_SIZE, ttl_seconds=_SEARCH_CACHE_TTL,
        )
        # imdb_id -> tmdb_id mapping is stable; cache 7 days. Bounded to keep RAM
        # predictable. Negative results (None) are also cached.
        self._imdb_find_cache: TTLCache[tuple[str, str], int | None] = TTLCache(
            max_size=5000, ttl_seconds=7 * 24 * 3600,
        )

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=10.0,
                params={"api_key": settings.TMDB_API_KEY},
                limits=httpx.Limits(
                    max_connections=50,
                    max_keepalive_connections=35,
                    keepalive_expiry=30,
                ),
            )
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    @property
    def is_configured(self) -> bool:
        return bool(settings.TMDB_API_KEY)

    @staticmethod
    def _metric_kind(path: str) -> str:
        """Map a TMDB path to a coarse `kind` label for metrics."""
        if path.startswith("/search/movie"):
            return "search_movie"
        if path.startswith("/search/tv"):
            return "search_tv"
        if path.startswith("/movie/") or path.startswith("/tv/"):
            return "details"
        return "other"

    async def _request(self, path: str, params: dict | None = None) -> dict:
        """GET with retry + exponential backoff + 429 rate-limit handling."""
        from app.utils.metrics import tmdb_requests_total

        client = await self._get_client()
        url = f"{self.BASE_URL}{path}"
        kind = self._metric_kind(path)
        rate_limited = False
        last_exc: Exception | None = None
        for attempt, delay in enumerate((*_RETRY_DELAYS, None)):
            try:
                resp = await client.get(url, params=params)
                # Handle 429 rate limit with Retry-After header
                if resp.status_code == 429:
                    rate_limited = True
                    retry_after = int(resp.headers.get("Retry-After", delay or 4))
                    if delay is not None:
                        logger.warning(f"TMDB 429 rate limited, waiting {retry_after}s")
                        await asyncio.sleep(retry_after)
                        continue
                    resp.raise_for_status()  # last attempt: raise
                resp.raise_for_status()
                tmdb_requests_total.labels(
                    kind=kind,
                    result="rate_limited" if rate_limited else "ok",
                ).inc()
                return resp.json()
            except _RETRYABLE as e:
                last_exc = e
                if delay is not None:
                    logger.warning(f"TMDB {path} attempt {attempt+1} failed ({e}), retrying in {delay}s")
                    await asyncio.sleep(delay)
            except httpx.HTTPStatusError as e:
                if e.response.status_code in (502, 503, 504) and delay is not None:
                    last_exc = e
                    logger.warning(f"TMDB {path} got {e.response.status_code}, retrying in {delay}s")
                    await asyncio.sleep(delay)
                else:
                    tmdb_requests_total.labels(kind=kind, result="error").inc()
                    raise
        tmdb_requests_total.labels(kind=kind, result="error").inc()
        raise last_exc  # type: ignore[misc]

    async def search_movie(
        self, title: str, year: int | None,
        *, summary: str | None = None, language: str | None = None,
    ) -> TMDBSearchOutcome:
        if not self.is_configured:
            return TMDBSearchOutcome("nomatch")
        lang = language or settings.TMDB_LANGUAGE
        cache_key = ("movie", title, year, lang)
        cached = self._search_cache.get(cache_key, default=_MISSING)
        if cached is not _MISSING:
            return cached
        params: dict = {"query": title, "language": lang}
        if year:
            params["year"] = year
        data = await self._request("/search/movie", params=params)
        results = data.get("results", [])
        outcome = self._best_match(
            results, title, year, summary,
            title_key="title", orig_key="original_title", date_key="release_date",
        )
        self._search_cache.set(cache_key, outcome)
        return outcome

    async def search_tv(
        self, title: str, year: int | None,
        *, summary: str | None = None, language: str | None = None,
    ) -> TMDBSearchOutcome:
        if not self.is_configured:
            return TMDBSearchOutcome("nomatch")
        lang = language or settings.TMDB_LANGUAGE
        cache_key = ("tv", title, year, lang)
        cached = self._search_cache.get(cache_key, default=_MISSING)
        if cached is not _MISSING:
            return cached
        params: dict = {"query": title, "language": lang}
        if year:
            params["first_air_date_year"] = year
        data = await self._request("/search/tv", params=params)
        results = data.get("results", [])
        outcome = self._best_match(
            results, title, year, summary,
            title_key="name", orig_key="original_name", date_key="first_air_date",
        )
        self._search_cache.set(cache_key, outcome)
        return outcome

    async def search_multi(
        self, title: str, media_type: Literal["movie", "tv"],
        *, language: str | None = None,
    ) -> TMDBSearchOutcome:
        """Last-resort title-only search via /search/multi (no year filter)."""
        if not self.is_configured:
            return TMDBSearchOutcome("nomatch")
        lang = language or settings.TMDB_LANGUAGE
        data = await self._request("/search/multi", params={"query": title, "language": lang})
        wanted = "movie" if media_type == "movie" else "tv"
        results = [r for r in data.get("results", []) if r.get("media_type") == wanted]
        title_key = "title" if media_type == "movie" else "name"
        orig_key = "original_title" if media_type == "movie" else "original_name"
        date_key = "release_date" if media_type == "movie" else "first_air_date"
        return self._best_match(
            results, title, None, None,
            title_key=title_key, orig_key=orig_key, date_key=date_key,
        )

    async def get_movie_details(self, tmdb_id: int) -> TMDBEnrichmentData:
        """Fetch movie details + external_ids + certifications in one API call."""
        data = await self._request(
            f"/movie/{tmdb_id}",
            params={
                "append_to_response": "credits,external_ids,release_dates",
                "language": settings.TMDB_LANGUAGE,
            },
        )
        return self._parse_details(data, tmdb_id, media_kind="movie")

    async def get_tv_details(self, tmdb_id: int) -> TMDBEnrichmentData:
        """Fetch TV details + external_ids + certifications in one API call."""
        data = await self._request(
            f"/tv/{tmdb_id}",
            params={
                "append_to_response": "credits,external_ids,content_ratings",
                "language": settings.TMDB_LANGUAGE,
            },
        )
        return self._parse_details(data, tmdb_id, media_kind="tv")

    async def find_by_imdb_id(
        self,
        imdb_id: str,
        media_type: Literal["movie", "tv"],
    ) -> int | None:
        """Resolve an imdb_id (e.g. 'tt0111161') to tmdb_id via TMDB /find endpoint.

        Returns None when the id resolves to an episode/season/person, is unknown,
        or when TMDB is unconfigured / unreachable. Mapping is stable so the
        result (including negatives) is cached 7 days. media_type must be
        'movie' or 'tv' — anything else returns None.
        """
        if media_type not in ("movie", "tv"):
            return None
        if not imdb_id:
            return None
        if not self.is_configured:
            return None

        cache_key = (imdb_id, media_type)
        cached = self._imdb_find_cache.get(cache_key, default=_MISSING)
        if cached is not _MISSING:
            return cached

        try:
            data = await self._request(
                f"/find/{imdb_id}",
                params={"external_source": "imdb_id", "language": settings.TMDB_LANGUAGE},
            )
        except Exception as exc:
            logger.warning("TMDB find_by_imdb_id failed for %s (%s): %s", imdb_id, media_type, exc)
            return None

        # Defensively ignore tv_episode_results, tv_season_results, person_results.
        if media_type == "movie":
            results = data.get("movie_results") or []
        else:
            results = data.get("tv_results") or []
        tmdb_id: int | None = None
        if results:
            raw_id = results[0].get("id")
            if isinstance(raw_id, int):
                tmdb_id = raw_id

        # Cache positives and negatives alike.
        self._imdb_find_cache.set(cache_key, tmdb_id)
        return tmdb_id

    @staticmethod
    def _preferred_region() -> str:
        """Country code from TMDB_LANGUAGE (e.g. 'fr-FR' -> 'FR'), default 'US'."""
        lang = settings.TMDB_LANGUAGE or ""
        if "-" in lang:
            return lang.rsplit("-", 1)[-1].upper()
        return "US"

    @classmethod
    def _movie_certification(cls, data: dict) -> str | None:
        """Pick the certification for the preferred region (then US, then first country with a
        cert; theatrical type==3 first) from /movie/{id}?append_to_response=release_dates.

        Delegates to the module-level pure parser so the live enrichment path and the unit-tested
        backfill share ONE implementation (single source of truth)."""
        return _parse_movie_certification(data)

    @classmethod
    def _tv_certification(cls, data: dict) -> str | None:
        """Pick the content rating for the preferred region (then US, then first country with a
        rating) from /tv/{id}?append_to_response=content_ratings.

        Delegates to the module-level pure parser (see [_movie_certification])."""
        return _parse_tv_certification(data)

    @staticmethod
    def _join_names(items: list[dict], limit: int | None = None) -> str | None:
        names = [i["name"].strip() for i in items if i.get("name") and i["name"].strip()]
        if limit is not None:
            names = names[:limit]
        # De-dup while preserving order.
        seen: set[str] = set()
        uniq = [n for n in names if not (n in seen or seen.add(n))]
        return ", ".join(uniq) or None

    @classmethod
    def _build_cast_json(cls, cast_list: list[dict]) -> str | None:
        """Serialize the top actors to the same JSON shape the NFO importer uses:
        [{name, role?, thumb?, profile?}]. `role` = TMDB character."""
        actors: list[dict[str, str]] = []
        for a in cast_list[:_CAST_LIMIT]:
            name = (a.get("name") or "").strip()
            if not name:
                continue
            entry: dict[str, str] = {"name": name}
            role = (a.get("character") or "").strip()
            if role:
                entry["role"] = role
            profile_path = a.get("profile_path")
            if profile_path:
                entry["thumb"] = f"{PROFILE_BASE}{profile_path}"
            person_id = a.get("id")
            if person_id:
                entry["profile"] = f"{PERSON_URL_BASE}/{person_id}"
            actors.append(entry)
        return json.dumps(actors, ensure_ascii=False) if actors else None

    def _parse_details(
        self, data: dict, tmdb_id: int, *, media_kind: str,
    ) -> TMDBEnrichmentData:
        """Parse a TMDB movie/tv detail response into TMDBEnrichmentData.

        `media_kind` is "movie" or "tv" — it selects the right localized title /
        date / studio / certification source between the two TMDB schemas."""
        is_movie = media_kind == "movie"
        date_key = "release_date" if is_movie else "first_air_date"
        title_key = "original_title" if is_movie else "original_name"

        external = data.get("external_ids") or {}
        # IMDB ID with guaranteed 'tt' prefix
        imdb_id = external.get("imdb_id")
        if imdb_id and not imdb_id.startswith("tt"):
            imdb_id = f"tt{imdb_id}"

        # TVDB id only exists for TV on TMDB; wikidata for both.
        tvdb_raw = external.get("tvdb_id")
        tvdb_id = str(tvdb_raw) if tvdb_raw not in (None, "", 0) else None
        wikidata_id = external.get("wikidata_id") or None

        poster = data.get("poster_path")
        backdrop = data.get("backdrop_path")
        genres = self._join_names(data.get("genres") or [])

        release_date = data.get(date_key, "") or ""
        premiered = release_date if len(release_date) >= 10 else None
        year = int(release_date[:4]) if len(release_date) >= 4 and release_date[:4].isdigit() else None

        # Studio: networks for TV (the channel), production companies otherwise.
        studio_sources = (data.get("networks") or []) if not is_movie else []
        studio_sources = studio_sources or (data.get("production_companies") or [])
        studio = self._join_names(studio_sources, limit=5)

        # Country: production_countries (named) preferred, else origin_country codes.
        prod_countries = data.get("production_countries") or []
        country = ", ".join(
            c["name"].strip() for c in prod_countries if c.get("name") and c["name"].strip()
        ) or None
        if not country:
            origin = [c for c in (data.get("origin_country") or []) if c]
            country = ", ".join(origin) or None

        content_rating = (
            self._movie_certification(data) if is_movie else self._tv_certification(data)
        )

        vote_average = data.get("vote_average")
        vote_count = data.get("vote_count")
        tmdb_votes = int(vote_count) if isinstance(vote_count, (int, float)) and vote_count else None

        # Extract cast (top N actors) — flat names + structured JSON.
        cast_list = (data.get("credits") or {}).get("cast") or []
        cast = ", ".join(
            a["name"].strip() for a in cast_list[:_CAST_LIMIT]
            if a.get("name") and a["name"].strip()
        ) or None
        cast_json = self._build_cast_json(cast_list)

        return TMDBEnrichmentData(
            tmdb_id=tmdb_id,
            imdb_id=imdb_id,
            overview=data.get("overview") or None,
            poster_url=f"{POSTER_BASE}{poster}" if poster else None,
            backdrop_url=f"{BACKDROP_BASE}{backdrop}" if backdrop else None,
            vote_average=vote_average,
            genres=genres,
            year=year,
            cast=cast,
            original_title=data.get(title_key) or None,
            tagline=(data.get("tagline") or "").strip() or None,
            premiered=premiered,
            status=(data.get("status") or "").strip() or None,
            studio=studio,
            country=country,
            content_rating=content_rating,
            tvdb_id=tvdb_id,
            wikidata_id=wikidata_id,
            tmdb_rating=vote_average if vote_average else None,
            tmdb_votes=tmdb_votes,
            cast_json=cast_json,
        )

    @staticmethod
    def _title_sim(query_norm: str, raw: str) -> float:
        """Best of ratio (Levenshtein-like) and token_set_ratio (order/extra-word
        robust), on normalized titles. 0..1."""
        if not raw:
            return 0.0
        cand = normalize_for_sorting(raw)
        if not cand:
            return 0.0
        return max(fuzz.ratio(query_norm, cand), fuzz.token_set_ratio(query_norm, cand)) / 100.0

    @staticmethod
    def _year_score(query_year: int | None, r_year: int | None) -> float:
        """1.0 exact · 0.8 ±1 · 0.5 unknown on a side · 0.0 mismatch."""
        if query_year and r_year:
            if query_year == r_year:
                return 1.0
            if abs(query_year - r_year) <= 1:
                return 0.8
            return 0.0
        return 0.5

    def _best_match(
        self,
        results: list[dict],
        title: str,
        year: int | None,
        summary: str | None,
        *,
        title_key: str,
        orig_key: str,
        date_key: str,
    ) -> TMDBSearchOutcome:
        """Score candidates (title vs localized+original, weighted with year),
        enforce anti-ambiguity margin, and break ties with the Xtream summary."""
        if not results:
            return TMDBSearchOutcome("nomatch")

        query_norm = normalize_for_sorting(title)
        scored: list[tuple[TMDBMatch, str]] = []  # (match, overview)
        for r in results[:10]:
            r_date = r.get(date_key, "") or ""
            r_year = int(r_date[:4]) if len(r_date) >= 4 and r_date[:4].isdigit() else None
            title_score = max(
                self._title_sim(query_norm, r.get(title_key, "")),
                self._title_sim(query_norm, r.get(orig_key, "")),
            )
            confidence = TITLE_WEIGHT * title_score + YEAR_WEIGHT * self._year_score(year, r_year)
            scored.append((
                TMDBMatch(
                    tmdb_id=r["id"], title=r.get(title_key, ""), year=r_year,
                    confidence=confidence, title_score=title_score,
                    vote_count=r.get("vote_count", 0) or 0,
                ),
                r.get("overview") or "",
            ))

        # confidence desc, then vote_count desc.
        scored.sort(key=lambda mo: (mo[0].confidence, mo[0].vote_count), reverse=True)
        best = scored[0][0]
        second = scored[1][0] if len(scored) > 1 else None
        margin = best.confidence - (second.confidence if second else 0.0)

        # Below the title/confidence bar → genuine no-match (keep best for recording).
        if best.confidence < AUTO_MATCH_THRESHOLD or best.title_score < MIN_TITLE_SCORE:
            return TMDBSearchOutcome("nomatch", match=None, best=best)

        # Clear winner.
        if margin >= MIN_MARGIN:
            return TMDBSearchOutcome("matched", match=best, best=best)

        # Ambiguous (top-2 too close) → try to break the tie with the Xtream summary.
        if summary:
            close = [(m, ov) for (m, ov) in scored if best.confidence - m.confidence < MIN_MARGIN]
            sim_ranked = sorted(
                ((m, self._summary_sim(summary, ov)) for (m, ov) in close),
                key=lambda ms: ms[1], reverse=True,
            )
            if sim_ranked:
                top_m, top_sim = sim_ranked[0]
                runner_sim = sim_ranked[1][1] if len(sim_ranked) > 1 else 0.0
                if top_sim >= SUMMARY_MIN_SIM and (top_sim - runner_sim) >= SUMMARY_TIEBREAK_MARGIN:
                    return TMDBSearchOutcome("matched", match=top_m, best=best)

        return TMDBSearchOutcome("ambiguous", match=None, best=best)

    @staticmethod
    def _summary_sim(xtream_summary: str, tmdb_overview: str) -> float:
        """Token-set similarity between the Xtream plot and a TMDB overview. 0..1."""
        if not xtream_summary or not tmdb_overview:
            return 0.0
        return fuzz.token_set_ratio(xtream_summary.lower(), tmdb_overview.lower()) / 100.0


# Singleton
tmdb_service = TMDBService()


# ---------------------------------------------------------------------------
# Module-level pure parsers — importable without a service instance.
# These are thin wrappers around the classmethods above so that unit tests
# and the backfill script can validate parsing logic without any HTTP.
# ---------------------------------------------------------------------------

def _parse_movie_certification(release_dates_json: dict) -> str | None:
    """Parse a TMDb /movie/{id}?append_to_response=release_dates response.

    Prefers the configured TMDB_LANGUAGE region (e.g. 'FR' from 'fr-FR'),
    falls back to 'US', then accepts the first country that has a non-empty
    certification.  Within a country entry the theatrical release type
    (type==3) is tried first; the first non-empty certification in any
    release_date entry is returned otherwise.

    Returns the RAW certification string (e.g. 'PG-13', 'U', '12') or None
    when no certification is found.  The Android app normalises via
    ContentRatingHelper — do NOT normalise here.
    """
    results = (release_dates_json.get("release_dates") or {}).get("results") or []

    preferred = TMDBService._preferred_region()
    by_region: dict[str, dict] = {
        r["iso_3166_1"]: r for r in results if r.get("iso_3166_1")
    }

    def _pick(entry: dict) -> str | None:
        release_date_list = entry.get("release_dates") or []
        # Prefer theatrical (type 3) first, then any non-empty cert.
        for rd in sorted(release_date_list, key=lambda rd: 0 if rd.get("type") == 3 else 1):
            cert = (rd.get("certification") or "").strip()
            if cert:
                return cert
        return None

    for region in (preferred, "US"):
        entry = by_region.get(region)
        if entry:
            cert = _pick(entry)
            if cert:
                return cert

    # Final fallback: first country that has any cert.
    for entry in results:
        cert = _pick(entry)
        if cert:
            return cert

    return None


def _parse_tv_certification(content_ratings_json: dict) -> str | None:
    """Parse a TMDb /tv/{id}?append_to_response=content_ratings response.

    Prefers the configured TMDB_LANGUAGE region (e.g. 'FR'), falls back to
    'US', then the first country with a non-empty rating.

    Returns the RAW rating string (e.g. 'TV-MA', 'NR', '12') or None.
    """
    results = (content_ratings_json.get("content_ratings") or {}).get("results") or []

    preferred = TMDBService._preferred_region()
    by_region: dict[str, dict] = {
        r["iso_3166_1"]: r for r in results if r.get("iso_3166_1")
    }

    for region in (preferred, "US"):
        entry = by_region.get(region)
        if entry and (entry.get("rating") or "").strip():
            return entry["rating"].strip()

    # Final fallback: first country that has any rating.
    for entry in results:
        rating = (entry.get("rating") or "").strip()
        if rating:
            return rating

    return None
