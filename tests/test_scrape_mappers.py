"""B1-09 — pure mappers of the scraping gateway (`app/services/scrape_mappers.py`).

Realistic TMDB / OMDb JSON frozen under `tests/fixtures/tmdb/` (hand-written
from the TMDB / OMDb documentation, no network). Every mapper output is
validated through `app.models.scrape_schemas` and must round-trip
(`model_dump(by_alias=True) == dict`), which proves the mapper emits exactly
the contract fields: no missing key, no extra key.
"""
from __future__ import annotations

import copy
import json
import random
from pathlib import Path

import pytest

from app.models import scrape_schemas as S
from app.services import nfo_import_service, scrape_mappers as M
from app.services.omdb_service import OmdbRatings, _parse_ratings
from app.services.tmdb_service import GatewayCandidate

FIXTURES = Path(__file__).parent / "fixtures" / "tmdb"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def roundtrip(model, data: dict) -> dict:
    """Validate `data` against `model` and return its by-alias dump."""
    return model.model_validate(data).model_dump(by_alias=True)


def assert_no_empty_string(value, path: str = "$") -> None:
    """Contract section 6: absent is null, never an empty string."""
    if isinstance(value, str):
        assert value != "", f"empty string at {path}"
    elif isinstance(value, dict):
        for key, item in value.items():
            assert_no_empty_string(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            assert_no_empty_string(item, f"{path}[{index}]")


def omdb_full() -> OmdbRatings:
    return _parse_ratings(load("omdb_tt0133093.json"))


# --- Reuse of the existing rules (import, not copy) -------------------------

def test_external_id_validators_are_the_nfo_importer_ones():
    assert M._validate_imdb is nfo_import_service._validate_imdb
    assert M._validate_tvdb is nfo_import_service._validate_tvdb
    assert M._validate_wikidata is nfo_import_service._validate_wikidata


# --- Movie ------------------------------------------------------------------

def test_map_movie_fr_full_payload():
    raw = load("movie_603_fr.json")
    out = M.map_movie(raw, language="fr-FR")

    assert out["tmdbId"] == 603
    assert out["title"] == "Matrix"
    assert out["originalTitle"] == "The Matrix"
    assert out["releaseDate"] == "1999-03-31"
    assert out["year"] == 1999
    assert out["runtimeMinutes"] == 136
    assert out["status"] == "Released"
    assert out["tagline"] == "Bienvenue dans le monde réel."
    assert out["contentRating"] == "12"
    assert out["genres"] == ["Action", "Science-Fiction"]
    assert out["countries"] == ["AU", "US"]
    assert out["collection"] == {"tmdbId": 2344, "name": "Matrix - Saga"}
    assert out["externalIds"] == {"imdb": "tt0133093", "tvdb": None, "wikidata": "Q83495"}
    assert out["trailer"] == {"site": "YouTube", "key": "frOfficialTrailer"}
    assert out["images"]["poster"] == "/f89U3ADr1oiB1s9GkdPOEpXUk5H.jpg"
    assert out["images"]["backdrop"] == "/fNG7i7RqMErkcqhohV2a6cV1Ehy.jpg"
    assert out["imageBaseUrl"] == "https://image.tmdb.org/t/p/"


def test_map_movie_is_the_cache_form_without_omdb_part():
    out = M.map_movie(load("movie_603_fr.json"), language="fr-FR")
    assert "omdbStatus" not in out
    # TMDB rating only; the OMDb ratings are composed at every response.
    assert out["ratings"] == [{"source": "tmdb", "value": 8.2, "max": 10.0, "votes": 25312}]
    json.dumps(out, sort_keys=True)  # plain JSON types only (ADR D2.4)


def test_map_movie_studios_are_deduplicated_and_capped_at_five():
    out = M.map_movie(load("movie_603_fr.json"), language="fr-FR")
    assert out["studios"] == [
        "Village Roadshow Pictures", "Groucho II Film Partnership", "Silver Pictures",
        "Warner Bros. Pictures", "Studio Six",
    ]


def test_map_movie_cast_dedupes_same_person_and_character_only():
    out = M.map_movie(load("movie_603_fr.json"), language="fr-FR")
    cast = out["cast"]
    assert [(c["tmdbPersonId"], c["character"]) for c in cast] == [
        (6384, "Thomas A. Anderson / Neo"),
        (2975, "Morpheus"),
        (530, "Trinity"),
        (1331, "Agent Smith"),
        (1331, "Voix de la Matrice"),   # same person, other role: kept
        (1000, None),                   # empty character -> null
    ]
    assert [c["order"] for c in cast] == [0, 1, 2, 3, 5, 6]  # TMDB order kept
    assert cast[0]["profilePath"] == "/rRdru6REr9i3WIHv2mntpcgxnoY.jpg"
    assert cast[-1]["profilePath"] is None


def test_map_movie_crew_filtered_and_deduplicated_on_person_and_job():
    out = M.map_movie(load("movie_603_fr.json"), language="fr-FR")
    assert [(c["tmdbPersonId"], c["job"]) for c in out["crew"]] == [
        (9340, "Director"),
        (9339, "Director"),
        (9339, "Screenplay"),
        (9340, "Writer"),
    ]
    assert out["crew"][0]["department"] == "Directing"
    assert all(c["job"] in M.MAIN_CREW_JOBS for c in out["crew"])


def test_map_movie_images_sorted_by_language_rank_then_tmdb_order():
    raw = load("movie_603_fr.json")
    fr = M.map_movie(raw, language="fr-FR")["images"]
    # fr, then en, then untagged, then any other; TMDB order inside a rank;
    # duplicate path and empty path dropped.
    assert fr["posters"] == [
        "/p_fr_a.jpg", "/p_fr_b.jpg", "/p_en_a.jpg", "/p_en_b.jpg", "/p_null_a.jpg", "/p_de_a.jpg",
    ]
    assert fr["backdrops"] == ["/bd_fr_1.jpg", "/bd_en_1.jpg", "/bd_null_1.jpg"]
    assert fr["logos"] == ["/logo_fr.png", "/logo_en.png"]

    en = M.map_movie(raw, language="en-US")["images"]
    assert en["posters"] == [
        "/p_en_a.jpg", "/p_en_b.jpg", "/p_null_a.jpg", "/p_fr_a.jpg", "/p_de_a.jpg", "/p_fr_b.jpg",
    ]
    assert en["backdrops"] == ["/bd_en_1.jpg", "/bd_null_1.jpg", "/bd_fr_1.jpg"]


@pytest.mark.parametrize(
    "language, expected",
    [("fr-FR", "12"), ("en-US", "R"), ("de-DE", "R"), ("pt-BR", "R"), ("fr-CA", "R")],
)
def test_content_rating_region_follows_the_language(language, expected):
    out = M.map_movie(load("movie_603_fr.json"), language=language)
    assert out["contentRating"] == expected


def test_region_derivation():
    assert M._region("fr-FR") == "FR"
    assert M._region("pt-BR") == "BR"
    assert M._region("en-us") == "US"
    assert M._region("fr") is None


def test_trailer_uses_the_existing_worker_rule():
    raw = load("movie_603_fr.json")
    # official before non-official, French before English, YouTube Trailer only
    assert M.map_movie(raw, language="fr-FR")["trailer"]["key"] == "frOfficialTrailer"
    raw["videos"]["results"] = [v for v in raw["videos"]["results"] if v["iso_639_1"] != "fr"]
    assert M.map_movie(raw, language="fr-FR")["trailer"]["key"] == "enOfficialTrailer"
    raw["videos"]["results"] = []
    assert M.map_movie(raw, language="fr-FR")["trailer"] is None
    del raw["videos"]
    assert M.map_movie(raw, language="fr-FR")["trailer"] is None


def test_map_movie_empty_fields_are_null_never_empty_string():
    raw = load("movie_empty_fields.json")
    out = M.map_movie(raw, language="fr-FR")
    assert out["tmdbId"] == 424242
    assert out["title"] is None            # whitespace only
    for key in ("originalTitle", "overview", "tagline", "releaseDate", "year", "status",
                "contentRating", "collection", "trailer"):
        assert out[key] is None, key
    assert out["runtimeMinutes"] is None   # runtime 0 -> null
    assert out["ratings"] == []            # vote_count 0 -> TMDB rating omitted
    assert out["images"]["poster"] is None
    assert out["images"]["backdrop"] is None   # "" -> null
    # invalid external ids -> null
    assert out["externalIds"] == {"imdb": None, "tvdb": None, "wikidata": None}
    for key in ("genres", "studios", "countries", "cast", "crew"):
        assert out[key] == []
    assert_no_empty_string(out)


@pytest.mark.parametrize("raw_id", [None, 0, -3, True, "603", 603.0])
def test_map_movie_requires_an_integer_id(raw_id):
    raw = load("movie_603_fr.json")
    raw["id"] = raw_id
    with pytest.raises(M.ScrapeMappingError):
        M.map_movie(raw, language="fr-FR")


def test_runtime_none_negative_or_fractional():
    raw = load("movie_603_fr.json")
    for value, expected in ((None, None), (-5, None), (90.0, 90), ("136", None), (True, None)):
        raw["runtime"] = value
        assert M.map_movie(raw, language="fr-FR")["runtimeMinutes"] == expected


# --- External ids -----------------------------------------------------------

@pytest.mark.parametrize(
    "ext, expected",
    [
        ({"imdb_id": "tt0133093"}, {"imdb": "tt0133093", "tvdb": None, "wikidata": None}),
        ({"imdb_id": "tt12"}, {"imdb": None, "tvdb": None, "wikidata": None}),
        ({"imdb_id": "TT0133093"}, {"imdb": None, "tvdb": None, "wikidata": None}),
        ({"imdb_id": "tt01330931234"}, {"imdb": None, "tvdb": None, "wikidata": None}),
        ({"tvdb_id": 81189}, {"imdb": None, "tvdb": "81189", "wikidata": None}),
        ({"tvdb_id": "81189"}, {"imdb": None, "tvdb": "81189", "wikidata": None}),
        ({"tvdb_id": 0}, {"imdb": None, "tvdb": None, "wikidata": None}),
        ({"tvdb_id": None}, {"imdb": None, "tvdb": None, "wikidata": None}),
        ({"tvdb_id": 1234567890123}, {"imdb": None, "tvdb": None, "wikidata": None}),  # 13 digits
        ({"tvdb_id": "٣٤"}, {"imdb": None, "tvdb": None, "wikidata": None}),  # non-ASCII digits
        ({"wikidata_id": "Q83495"}, {"imdb": None, "tvdb": None, "wikidata": "Q83495"}),
        ({"wikidata_id": "q83495"}, {"imdb": None, "tvdb": None, "wikidata": None}),
        ({"wikidata_id": "P12"}, {"imdb": None, "tvdb": None, "wikidata": None}),
        ({"imdb_id": ["tt0133093"]}, {"imdb": None, "tvdb": None, "wikidata": None}),
    ],
)
def test_external_ids_validated_with_the_nfo_rules(ext, expected):
    raw = load("movie_empty_fields.json")
    raw["imdb_id"] = ""
    raw["external_ids"] = ext
    assert M.map_movie(raw, language="fr-FR")["externalIds"] == expected


def test_imdb_id_falls_back_to_the_top_level_field():
    raw = load("movie_empty_fields.json")
    raw["external_ids"] = {}
    raw["imdb_id"] = "tt0133093"
    assert M.map_movie(raw, language="fr-FR")["externalIds"]["imdb"] == "tt0133093"


# --- Show -------------------------------------------------------------------

def test_map_show_fr_full_payload():
    out = M.map_show(load("tv_1396_fr.json"), language="fr-FR")
    assert out["tmdbId"] == 1396
    assert out["title"] == "Breaking Bad"
    assert out["originalTitle"] == "Breaking Bad"
    assert out["firstAirDate"] == "2008-01-20"
    assert out["year"] == 2008
    assert out["episodeRuntimeMinutes"] == 45   # smallest value >= 1 ([0, 47, 45])
    assert out["status"] == "Ended"
    assert out["contentRating"] == "16"         # FR rating
    assert out["numberOfSeasons"] == 5
    assert out["numberOfEpisodes"] == 62
    assert out["externalIds"] == {"imdb": "tt0903747", "tvdb": "81189", "wikidata": "Q1079"}
    assert out["trailer"] == {"site": "YouTube", "key": "frTvTrailer"}
    assert "firstAirDate" in out and "releaseDate" not in out
    assert "runtimeMinutes" not in out and "collection" not in out
    assert "omdbStatus" not in out
    assert out["ratings"] == [{"source": "tmdb", "value": 8.9, "max": 10.0, "votes": 12900}]


def test_map_show_content_rating_follows_language():
    raw = load("tv_1396_fr.json")
    assert M.map_show(raw, language="en-US")["contentRating"] == "TV-MA"
    assert M.map_show(raw, language="de-DE")["contentRating"] == "16"
    assert M.map_show(raw, language="pt-BR")["contentRating"] == "TV-MA"  # no BR: US fallback


def test_map_show_studios_networks_first_countries_production_first():
    raw = load("tv_1396_fr.json")
    out = M.map_show(raw, language="fr-FR")
    assert out["studios"] == ["AMC"]        # networks win, duplicates removed
    assert out["countries"] == ["US"]

    raw["networks"] = []
    assert M.map_show(raw, language="fr-FR")["studios"] == [
        "Sony Pictures Television Studios", "High Bridge Productions",
    ]
    raw["production_countries"] = []
    raw["origin_country"] = ["GB", "IE", "GB", ""]
    assert M.map_show(raw, language="fr-FR")["countries"] == ["GB", "IE"]


def test_map_show_crew_creators_first_then_main_team_deduplicated():
    out = M.map_show(load("tv_1396_fr.json"), language="fr-FR")
    assert [(c["tmdbPersonId"], c["job"]) for c in out["crew"]] == [
        (66633, "Creator"),     # created_by (duplicate entry removed)
        (66633, "Writer"),      # same person, other job: kept
        (1223, "Director"),
    ]
    assert out["crew"][0]["department"] is None
    assert out["crew"][0]["profilePath"] == "/z3E0DhBg1V1PZVEtS9vfFPzOWYB.jpg"


def test_map_show_seasons_sorted_with_specials_and_deduplicated():
    out = M.map_show(load("tv_1396_fr.json"), language="fr-FR")
    assert [s["seasonNumber"] for s in out["seasons"]] == [0, 1, 2, 3]
    special, one, two, three = out["seasons"]
    assert special["name"] == "Épisodes spéciaux"
    assert special["posterPath"] is None
    assert one == {
        "seasonNumber": 1, "name": "Saison 1", "airDate": "2008-01-20", "episodeCount": 7,
        "posterPath": "/1BP4xYv9ZG4ZVHkL7ocOziBMby4.jpg", "tmdbId": 3572,
    }
    assert (two["name"], two["airDate"], two["posterPath"]) == (None, None, None)
    assert three["tmdbId"] == 3573   # first wins over the duplicate number


def test_episode_runtime_minutes_edge_cases():
    raw = load("tv_1396_fr.json")
    for value, expected in (([], None), ([0], None), ([0, 30], 30), ([60, 45.0], 45),
                            (None, None), ("45", None), ([True], None)):
        raw["episode_run_time"] = value
        assert M.map_show(raw, language="fr-FR")["episodeRuntimeMinutes"] == expected


# --- Season and episodes ----------------------------------------------------

def test_map_season_full_payload():
    out = M.map_season(load("season_1396_1_fr.json"), show_tmdb_id=1396, language="fr-FR")
    assert out["showTmdbId"] == 1396
    assert out["tmdbId"] == 3572
    assert out["seasonNumber"] == 1
    assert out["name"] == "Saison 1"
    assert out["overview"] is None
    assert out["airDate"] == "2008-01-20"
    assert out["externalIds"] == {"tvdb": "30272"}
    assert out["imageBaseUrl"] == "https://image.tmdb.org/t/p/"
    assert out["images"] == {
        "poster": "/1BP4xYv9ZG4ZVHkL7ocOziBMby4.jpg",
        "backdrop": None,
        "posters": ["/s1_fr.jpg", "/s1_en.jpg", "/s1_null.jpg"],
        "backdrops": [],
        "logos": [],
    }
    assert [e["episodeNumber"] for e in out["episodes"]] == [1, 2, 3, 4, 5, 6, 7]


def test_map_season_episode_details():
    out = M.map_season(load("season_1396_1_fr.json"), show_tmdb_id=1396, language="fr-FR")
    ep1, ep2, ep3, ep4 = out["episodes"][:4]
    assert ep1["tmdbId"] == 62085
    assert ep1["seasonNumber"] == 1
    assert ep1["name"] == "Pilote"
    assert ep1["runtimeMinutes"] == 58
    assert ep1["stillPath"] == "/ydlY3iPfeOAvu8gVqrxPoMvzNCn.jpg"
    assert ep1["ratings"] == [{"source": "tmdb", "value": 8.3, "max": 10.0, "votes": 300}]
    assert [(c["job"], c["tmdbPersonId"]) for c in ep1["crew"]] == [
        ("Director", 66633), ("Writer", 66633),   # Editor excluded, duplicate Writer removed
    ]
    assert [(g["name"], g["order"]) for g in ep1["guestStars"]] == [
        ("John Koyama", 500), ("Carmen Serano", 501),
    ]
    assert ep1["guestStars"][1]["profilePath"] == "/g2.jpg"
    assert "externalIds" not in ep1
    # runtime 0 -> null, vote_count 0 -> no rating, empty strings -> null
    assert (ep3["runtimeMinutes"], ep3["ratings"], ep3["overview"], ep3["stillPath"]) == (
        None, [], None, None)
    assert (ep4["airDate"], ep4["stillPath"]) == (None, None)
    assert ep2["crew"] == [] and ep2["guestStars"] == []


def test_map_season_sorts_and_deduplicates_episodes():
    raw = load("season_1396_1_fr.json")
    episodes = raw["episodes"]
    random.Random(7).shuffle(episodes)
    episodes.append(copy.deepcopy(episodes[0]) | {"id": 111111})          # duplicate number
    episodes.append(copy.deepcopy(episodes[1]) | {"episode_number": 0})   # number < 1
    episodes.append(copy.deepcopy(episodes[1]) | {"id": None})            # no id
    out = M.map_season(raw, show_tmdb_id=1396, language="fr-FR")
    assert [e["episodeNumber"] for e in out["episodes"]] == [1, 2, 3, 4, 5, 6, 7]
    assert 111111 not in {e["tmdbId"] for e in out["episodes"]}


def test_map_season_specials_zero_and_missing_tvdb():
    raw = load("season_1396_1_fr.json")
    raw["season_number"] = 0
    raw["external_ids"] = {"tvdb_id": None}
    del raw["images"]
    out = M.map_season(raw, show_tmdb_id=1396, language="fr-FR")
    assert out["seasonNumber"] == 0
    assert out["externalIds"] == {"tvdb": None}
    assert out["images"]["posters"] == []


@pytest.mark.parametrize("field, value", [("id", None), ("season_number", None),
                                          ("season_number", -1), ("season_number", "1")])
def test_map_season_requires_ids(field, value):
    raw = load("season_1396_1_fr.json")
    raw[field] = value
    with pytest.raises(M.ScrapeMappingError):
        M.map_season(raw, show_tmdb_id=1396, language="fr-FR")


# --- Bounds -----------------------------------------------------------------

def _person(i: int, **extra) -> dict:
    return {"id": 1000 + i, "name": f"Person {i}", "profile_path": f"/p{i}.jpg", **extra}


def test_movie_list_bounds():
    raw = load("movie_603_fr.json")
    raw["credits"] = {
        "cast": [_person(i, character=f"Role {i}", order=i) for i in range(80)],
        "crew": [_person(i, job="Director", department="Directing") for i in range(80)],
    }
    raw["production_companies"] = [{"name": f"Studio {i}"} for i in range(12)]
    raw["images"] = {
        "posters": [{"iso_639_1": "fr", "file_path": f"/po{i}.jpg"} for i in range(40)],
        "backdrops": [{"iso_639_1": "fr", "file_path": f"/bd{i}.jpg"} for i in range(40)],
        "logos": [{"iso_639_1": "fr", "file_path": f"/lo{i}.png"} for i in range(40)],
    }
    out = M.map_movie(raw, language="fr-FR")
    assert len(out["cast"]) == M.MAX_CAST == 30
    assert [c["order"] for c in out["cast"]] == list(range(30))   # TMDB order, first 30
    assert len(out["crew"]) == M.MAX_CREW == 30
    assert len(out["studios"]) == M.MAX_STUDIOS == 5
    assert len(out["images"]["posters"]) == 20
    assert len(out["images"]["backdrops"]) == 20
    assert len(out["images"]["logos"]) == 10
    assert out["images"]["posters"][:3] == ["/po0.jpg", "/po1.jpg", "/po2.jpg"]  # stable
    roundtrip(S.MovieDetails, M.with_omdb(out, None, "no_imdb_id"))


def test_show_crew_bound_counts_creators_and_team_together():
    raw = load("tv_1396_fr.json")
    raw["created_by"] = [_person(i) for i in range(5)]
    raw["credits"] = {"cast": [], "crew": [_person(100 + i, job="Writer") for i in range(60)]}
    out = M.map_show(raw, language="fr-FR")
    assert len(out["crew"]) == 30
    assert [c["job"] for c in out["crew"][:5]] == ["Creator"] * 5

    raw["created_by"] = [_person(i) for i in range(40)]
    out = M.map_show(raw, language="fr-FR")
    assert len(out["crew"]) == 30
    assert all(c["job"] == "Creator" for c in out["crew"])


def test_season_and_episode_bounds():
    raw = load("season_1396_1_fr.json")
    template = raw["episodes"][0]
    episodes = []
    for number in range(520, 0, -1):    # descending: the mapper must sort
        episode = copy.deepcopy(template)
        episode.update(id=900000 + number, episode_number=number)
        episode["crew"] = [_person(i, job=job) for i, job in enumerate(
            ["Director", "Writer", "Screenplay", "Story", "Editor"] * 6)]
        episode["guest_stars"] = [_person(i, character=f"C{i}", order=i) for i in range(30)]
        episodes.append(episode)
    raw["episodes"] = episodes
    out = M.map_season(raw, show_tmdb_id=1396, language="fr-FR")

    assert len(out["episodes"]) == M.MAX_EPISODES == 500
    assert [e["episodeNumber"] for e in out["episodes"]] == list(range(1, 501))
    first = out["episodes"][0]
    assert len(first["guestStars"]) == 20
    assert len(first["crew"]) == 10
    # Episode crew: Director / Writer / Screenplay only ("Story" and "Editor" excluded).
    assert {c["job"] for c in first["crew"]} <= {"Director", "Writer", "Screenplay"}


def test_find_is_bounded_to_five_matches_in_kind_order():
    out = M.map_find(load("find_imdb_mixed.json"))
    assert [(m["mediaType"], m["tmdbId"]) for m in out] == [
        ("movie", 603), ("movie", 604), ("show", 1396), ("episode", 62085), ("episode", 62101),
    ]
    assert out[0] == {"mediaType": "movie", "tmdbId": 603, "showTmdbId": None,
                      "seasonNumber": None, "episodeNumber": None}
    assert out[3] == {"mediaType": "episode", "tmdbId": 62085, "showTmdbId": 1396,
                      "seasonNumber": 1, "episodeNumber": 1}
    assert out[4]["seasonNumber"] == 0 and out[4]["episodeNumber"] == 3  # specials kept


def test_find_ignores_seasons_and_persons_and_empty_input():
    raw = {"person_results": [{"id": 1}], "tv_season_results": [{"id": 2, "show_id": 3}]}
    assert M.map_find(raw) == []
    assert M.map_find({}) == []
    assert M.map_find({"movie_results": None, "tv_results": "x", "tv_episode_results": [None, 3]}) == []


# --- Candidates -------------------------------------------------------------

def _cand(tmdb_id, score, *, kind="movie", title="T", original=None, year=1999,
          overview=None, poster=None) -> GatewayCandidate:
    return GatewayCandidate(
        tmdb_id=tmdb_id, kind=kind, title=title, original_title=original, year=year,
        overview=overview, poster_path=poster, score=score,
    )


def test_candidates_sorted_by_score_desc_then_tmdb_id_asc():
    out = M.map_candidates([
        _cand(30, 0.5), _cand(10, 0.9), _cand(20, 0.9), _cand(5, 0.5), _cand(99, 1.0),
    ])
    assert [(c["tmdbId"], c["score"]) for c in out] == [
        (99, 1.0), (10, 0.9), (20, 0.9), (5, 0.5), (30, 0.5),
    ]


def test_candidates_bounded_to_ten_best():
    out = M.map_candidates([_cand(i, i / 100) for i in range(1, 31)])
    assert len(out) == 10
    assert [c["tmdbId"] for c in out] == list(range(30, 20, -1))


def test_candidate_fields_and_null_rules():
    out = M.map_candidates([
        _cand(1, 0.8, kind="show", title="  Serie ", original="  ", year=0,
              overview="", poster="/x.jpg"),
        _cand(2, 0.7, title="", original="Titre original"),       # falls back to original
        _cand(3, 0.6, title="  ", original=""),                    # nameless: skipped
        _cand(4, 1.7),                                              # clamped
        _cand(5, -0.2),
        _cand(1, 0.1, kind="show"),                                 # duplicate (kind, id)
    ])
    assert [c["tmdbId"] for c in out] == [4, 1, 2, 5]
    assert out[1] == {
        "tmdbId": 1, "mediaType": "show", "title": "Serie", "originalTitle": None,
        "year": None, "overview": None, "posterPath": "/x.jpg", "score": 0.8,
    }
    by_id = {c["tmdbId"]: c for c in out}
    assert set(by_id) == {1, 2, 4, 5}
    assert by_id[1]["title"] == "Serie" and by_id[1]["originalTitle"] is None
    assert by_id[1]["year"] is None and by_id[1]["overview"] is None
    assert by_id[2]["title"] == "Titre original" and by_id[2]["originalTitle"] == "Titre original"
    assert by_id[4]["score"] == 1.0
    assert by_id[5]["score"] == 0.0
    assert by_id[1]["score"] == 0.8   # the duplicate with the lower score is dropped
    assert_no_empty_string(out)
    for cand in out:
        roundtrip(S.Candidate, cand)


def test_same_tmdb_id_in_two_kinds_is_kept():
    out = M.map_candidates([_cand(7, 0.9, kind="movie"), _cand(7, 0.8, kind="show")])
    assert [(c["mediaType"], c["tmdbId"]) for c in out] == [("movie", 7), ("show", 7)]


# --- Ratings ----------------------------------------------------------------

def test_omdb_fixture_normalisation():
    ratings = omdb_full()
    assert ratings == OmdbRatings(imdb_rating=8.7, imdb_votes=2128458,
                                  rotten_tomatoes=83, metacritic=73)
    assert M.map_omdb_ratings(ratings) == [
        {"source": "imdb", "value": 8.7, "max": 10.0, "votes": 2128458},
        {"source": "metacritic", "value": 73.0, "max": 100.0, "votes": None},
        {"source": "rottenTomatoes", "value": 83.0, "max": 100.0, "votes": None},
    ]


def test_omdb_rotten_tomatoes_percent_and_metacritic_forms():
    raw = {"imdbRating": "N/A", "imdbVotes": "N/A", "Metascore": "N/A", "Ratings": [
        {"Source": "Rotten Tomatoes", "Value": "87%"},
        {"Source": "Metacritic", "Value": "74/100"},
    ]}
    parsed = _parse_ratings(raw)
    assert M.map_omdb_ratings(parsed) == [
        {"source": "metacritic", "value": 74.0, "max": 100.0, "votes": None},
        {"source": "rottenTomatoes", "value": 87.0, "max": 100.0, "votes": None},
    ]
    # Metascore field used when the Ratings[] entry is absent.
    parsed = _parse_ratings({"Metascore": "61", "Ratings": []})
    assert M.map_omdb_ratings(parsed) == [
        {"source": "metacritic", "value": 61.0, "max": 100.0, "votes": None}]


def test_omdb_partial_payload_only_keeps_present_sources():
    parsed = _parse_ratings(load("omdb_tt0903747_partial.json"))
    assert M.map_omdb_ratings(parsed) == [
        {"source": "imdb", "value": 9.5, "max": 10.0, "votes": 2300000}]


def test_map_omdb_ratings_none_and_out_of_range():
    assert M.map_omdb_ratings(None) == []
    assert M.map_omdb_ratings(OmdbRatings(None, None, None, None)) == []
    assert M.map_omdb_ratings(OmdbRatings(11.0, 5, 101, -1)) == []
    # imdb rating without votes keeps votes null
    assert M.map_omdb_ratings(OmdbRatings(7.0, None, None, None)) == [
        {"source": "imdb", "value": 7.0, "max": 10.0, "votes": None}]


def test_tmdb_rating_omitted_without_votes():
    assert M.map_tmdb_rating({"vote_average": 7.5, "vote_count": 0}) is None
    assert M.map_tmdb_rating({"vote_average": 7.5}) is None
    assert M.map_tmdb_rating({"vote_count": 10}) is None
    assert M.map_tmdb_rating({"vote_average": 11, "vote_count": 10}) is None
    assert M.map_tmdb_rating({"vote_average": 0, "vote_count": 3}) == {
        "source": "tmdb", "value": 0.0, "max": 10.0, "votes": 3}


def test_compose_ratings_order_is_tmdb_imdb_metacritic_rotten_tomatoes():
    tmdb = M.map_tmdb_rating({"vote_average": 8.2, "vote_count": 100})
    for omdb in (omdb_full(), M.map_omdb_ratings(omdb_full())[::-1]):
        out = M.compose_ratings(tmdb, omdb)
        assert [r["source"] for r in out] == ["tmdb", "imdb", "metacritic", "rottenTomatoes"]


def test_compose_ratings_without_tmdb_or_without_votes():
    tmdb_no_votes = {"source": "tmdb", "value": 5.0, "max": 10.0, "votes": 0}
    out = M.compose_ratings(tmdb_no_votes, omdb_full())
    assert [r["source"] for r in out] == ["imdb", "metacritic", "rottenTomatoes"]
    assert M.compose_ratings(None, None) == []
    assert M.compose_ratings(None, []) == []


def test_compose_ratings_one_entry_per_source():
    tmdb = {"source": "tmdb", "value": 8.0, "max": 10.0, "votes": 5}
    dup = [dict(tmdb, value=1.0), {"source": "nonsense", "value": 1, "max": 1, "votes": None}]
    out = M.compose_ratings(tmdb, dup)
    assert out == [tmdb]


# --- with_omdb --------------------------------------------------------------

def test_with_omdb_ok_composes_ratings_and_status():
    details = M.map_movie(load("movie_603_fr.json"), language="fr-FR")
    snapshot = copy.deepcopy(details)
    out = M.with_omdb(details, omdb_full(), "ok")

    assert details == snapshot, "the cached form must not be mutated"
    assert out is not details
    assert out["omdbStatus"] == "ok"
    assert [r["source"] for r in out["ratings"]] == ["tmdb", "imdb", "metacritic", "rottenTomatoes"]
    assert out["ratings"][1]["votes"] == 2128458
    dumped = roundtrip(S.MovieDetails, out)
    assert dumped == out


def test_with_omdb_accepts_the_cached_rating_dict_form():
    details = M.map_show(load("tv_1396_fr.json"), language="fr-FR")
    cached = M.map_omdb_ratings(_parse_ratings(load("omdb_tt0903747_partial.json")))
    out = M.with_omdb(details, cached, "ok")
    assert [r["source"] for r in out["ratings"]] == ["tmdb", "imdb"]
    assert roundtrip(S.ShowDetails, out) == out


@pytest.mark.parametrize("status", ["not_configured", "budget_exhausted", "not_found",
                                    "error", "no_imdb_id"])
def test_with_omdb_non_ok_statuses_omit_omdb_ratings(status):
    details = M.map_movie(load("movie_603_fr.json"), language="fr-FR")
    out = M.with_omdb(details, omdb_full(), status)   # even if ratings are passed
    assert out["omdbStatus"] == status
    assert [r["source"] for r in out["ratings"]] == ["tmdb"]
    roundtrip(S.MovieDetails, out)


def test_with_omdb_ok_without_ratings_and_without_tmdb_rating():
    details = M.map_movie(load("movie_empty_fields.json"), language="fr-FR")
    out = M.with_omdb(details, None, "ok")
    assert out["ratings"] == [] and out["omdbStatus"] == "ok"
    roundtrip(S.MovieDetails, out)


def test_with_omdb_rejects_unknown_status():
    details = M.map_movie(load("movie_603_fr.json"), language="fr-FR")
    with pytest.raises(ValueError):
        M.with_omdb(details, None, "whatever")


def test_cached_form_alone_is_not_a_valid_response_body():
    """`omdbStatus` is required: the cache form must go through `with_omdb`."""
    from pydantic import ValidationError
    details = M.map_movie(load("movie_603_fr.json"), language="fr-FR")
    with pytest.raises(ValidationError):
        S.MovieDetails.model_validate(details)


# --- Every output validates and round-trips ---------------------------------

def _all_outputs() -> dict[str, tuple[type, dict]]:
    movie = M.with_omdb(M.map_movie(load("movie_603_fr.json"), language="fr-FR"),
                        omdb_full(), "ok")
    movie_empty = M.with_omdb(M.map_movie(load("movie_empty_fields.json"), language="fr-FR"),
                              None, "no_imdb_id")
    show = M.with_omdb(M.map_show(load("tv_1396_fr.json"), language="fr-FR"),
                       _parse_ratings(load("omdb_tt0903747_partial.json")), "ok")
    season = M.map_season(load("season_1396_1_fr.json"), show_tmdb_id=1396, language="fr-FR")
    return {
        "movie": (S.MovieDetails, movie),
        "movie_empty": (S.MovieDetails, movie_empty),
        "show": (S.ShowDetails, show),
        "season": (S.SeasonDetails, season),
    }


@pytest.mark.parametrize("name", ["movie", "movie_empty", "show", "season"])
def test_mapper_output_validates_and_roundtrips(name):
    model, data = _all_outputs()[name]
    assert roundtrip(model, data) == data
    assert_no_empty_string(data)
    json.dumps(data, sort_keys=True)


def test_find_and_candidates_outputs_validate():
    for match in M.map_find(load("find_imdb_mixed.json")):
        assert roundtrip(S.FindMatch, match) == match
    cands = M.map_candidates([_cand(1, 0.9, overview="x", poster="/p.jpg", original="O")])
    assert roundtrip(S.Candidate, cands[0]) == cands[0]


# --- Purity -----------------------------------------------------------------

@pytest.mark.parametrize("fixture, call", [
    ("movie_603_fr.json", lambda raw: M.map_movie(raw, language="fr-FR")),
    ("tv_1396_fr.json", lambda raw: M.map_show(raw, language="fr-FR")),
    ("season_1396_1_fr.json",
     lambda raw: M.map_season(raw, show_tmdb_id=1396, language="fr-FR")),
    ("find_imdb_mixed.json", M.map_find),
])
def test_mappers_do_not_mutate_their_input_and_are_deterministic(fixture, call):
    raw = load(fixture)
    snapshot = copy.deepcopy(raw)
    first = call(raw)
    assert raw == snapshot
    assert call(raw) == first
    # The output shares no mutable object with the input.
    json.dumps(first)


def test_output_does_not_alias_input_lists():
    raw = load("movie_603_fr.json")
    out = M.map_movie(raw, language="fr-FR")
    out["genres"].append("mutated")
    out["cast"].clear()
    assert M.map_movie(raw, language="fr-FR")["genres"] == ["Action", "Science-Fiction"]
    assert len(raw["credits"]["cast"]) == 8


# --- Size -------------------------------------------------------------------

def test_full_500_episode_season_response_stays_under_4_mib():
    """FR-028: a season of 500 episodes with every list full serialises under
    4 MiB (the client refuses a larger body)."""
    raw = load("season_1396_1_fr.json")
    template = raw["episodes"][0]
    overview = "Résumé détaillé de l'épisode. " * 50          # ~1500 characters
    episodes = []
    for number in range(1, 501):
        episode = copy.deepcopy(template)
        episode.update(
            id=800000 + number, episode_number=number, name=f"Épisode {number} " + "x" * 60,
            overview=overview, still_path=f"/still{number:04d}" + "a" * 30 + ".jpg",
        )
        episode["crew"] = [
            _person(number * 100 + i, job=("Director", "Writer", "Screenplay")[i % 3],
                    department="Directing", name="Réalisateur " + "n" * 25)
            for i in range(10)
        ]
        episode["guest_stars"] = [
            _person(number * 100 + i, character="Personnage " + "c" * 30, order=i,
                    name="Acteur invité " + "n" * 25)
            for i in range(20)
        ]
        episodes.append(episode)
    raw["episodes"] = episodes
    raw["images"] = {"posters": [{"iso_639_1": "fr", "file_path": f"/s{i}.jpg"} for i in range(60)]}

    details = M.map_season(raw, show_tmdb_id=1396, language="fr-FR")
    assert len(details["episodes"]) == 500
    assert all(len(e["guestStars"]) == 20 and len(e["crew"]) == 10 for e in details["episodes"])

    response = S.SeasonResponse.model_validate({
        "schemaVersion": 1, "cacheKey": "1|fr-FR|season|1396|1", "language": "fr-FR",
        "fetchedAt": 1760745600000, "expiresAt": 1763337600000, "cache": "miss",
        "result": "found", "details": details,
    })
    size = len(response.model_dump_json(by_alias=True).encode("utf-8"))
    assert size < 4 * 1024 * 1024, f"{size} bytes"
    # The stored (cache) payload is the same dict, serialised sorted.
    assert len(json.dumps(details, sort_keys=True, ensure_ascii=False).encode("utf-8")) < 4 * 1024 * 1024
