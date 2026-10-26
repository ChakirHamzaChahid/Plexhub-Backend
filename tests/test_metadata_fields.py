"""B2-04a — colonnes protégées, conversions colonnes ⇄ champs, validation (ADR 0011
§ D2.2, contrat B2 § 6.2/§ 6.3). Noyau PUR : aucune I/O.
"""
from __future__ import annotations

import json

import pytest

from app.services.metadata_columns import (
    DERIVED_COLUMNS,
    FIELD_COLUMNS,
    FIELDS_BY_TYPE,
    LIMITS,
    METADATA_WRITABLE_COLUMNS,
)
from app.services.metadata_fields import (
    FieldError,
    apply_patch,
    changed_field_names,
    columns_to_flat,
    field_columns,
    flat_to_nested,
    normalize_patch,
)

MOVIE_FIELDS = sorted([
    "cast", "contentRating", "countries", "durationMs", "genres", "ids.imdb", "ids.tmdb",
    "ids.tvdb", "ids.wikidata", "originalTitle", "premiered", "ratings.imdb", "ratings.tmdb",
    "studios", "summary", "tagline", "title", "year", "youtubeTrailer",
    # lot B4
    "availableTrailers", "directors", "fanartGallery", "fanartUrl", "posterGallery", "posterUrl",
    "ratings.metacritic", "ratings.rottenTomatoes", "spokenLanguages", "writers",
])


# ─── Tableau des champs par type (§ 6.3) ────────────────────────────────────


def test_fields_by_type_matches_the_contract_table():
    assert list(FIELDS_BY_TYPE["movie"]) == MOVIE_FIELDS
    assert list(FIELDS_BY_TYPE["show"]) == sorted(MOVIE_FIELDS + ["status"])
    assert list(FIELDS_BY_TYPE["episode"]) == [
        "durationMs", "premiered", "ratings.imdb", "ratings.tmdb", "summary", "title",
    ]


def test_every_field_maps_to_columns_and_writable_set_covers_them():
    for names in FIELDS_BY_TYPE.values():
        for name in names:
            assert FIELD_COLUMNS[name], name
    written = {c for cols in FIELD_COLUMNS.values() for c in cols}
    assert written <= METADATA_WRITABLE_COLUMNS
    assert set(DERIVED_COLUMNS) <= METADATA_WRITABLE_COLUMNS
    # never writable: images, flow, read state, keys, hash, flags
    for forbidden in (
        "thumb_url", "art_url", "resolved_thumb_url", "view_count", "is_broken",
        "file_size", "content_hash", "rating_key", "server_id", "is_adult",
        "is_in_allowed_categories", "scraped_rating", "audience_rating", "rating",
        "tmdb_match_confidence", "match_locked", "match_source", "type",
    ):
        assert forbidden not in METADATA_WRITABLE_COLUMNS, forbidden


def test_writable_columns_include_the_protected_derived_ones():
    assert {"title_sortable", "unification_id", "history_group_key", "display_rating", "cast"} <= (
        METADATA_WRITABLE_COLUMNS
    )


# ─── columns → flat fields ──────────────────────────────────────────────────


def _cols(**kw):
    base = dict(
        title="The Matrix", original_title=None, summary=None, tagline=None, genres=None,
        studio=None, country=None, content_rating=None, year=None, premiered=None,
        status=None, duration=None, imdb_id=None, tmdb_id=None, tvdb_id=None,
        wikidata_id=None, imdb_rating=None, imdb_votes=None, tmdb_rating=None,
        tmdb_votes=None, cast=None, cast_json=None, youtube_trailer=None,
    )
    base.update(kw)
    return base


def test_empty_columns_give_null_scalars_and_empty_lists():
    flat = columns_to_flat(_cols(), "movie")
    assert set(flat) == set(FIELDS_BY_TYPE["movie"])
    assert flat["title"] == "The Matrix"
    assert flat["genres"] == [] and flat["studios"] == [] and flat["countries"] == []
    assert flat["cast"] == []
    assert flat["summary"] is None and flat["ratings.imdb"] is None
    assert flat["ids.imdb"] is None and flat["durationMs"] is None


def test_blank_strings_read_as_null_and_csv_is_split():
    flat = columns_to_flat(
        _cols(summary="", imdb_id="", genres="Action, Science-Fiction", studio="A, B", country="US, FR"),
        "movie",
    )
    assert flat["summary"] is None and flat["ids.imdb"] is None
    assert flat["genres"] == ["Action", "Science-Fiction"]
    assert flat["studios"] == ["A", "B"] and flat["countries"] == ["US", "FR"]


def test_ratings_read_value_and_votes():
    flat = columns_to_flat(_cols(imdb_rating=8.7, imdb_votes=1000, tmdb_rating=8.2), "movie")
    assert flat["ratings.imdb"] == {"value": 8.7, "votes": 1000}
    assert flat["ratings.tmdb"] == {"value": 8.2, "votes": None}


def test_cast_prefers_cast_json_and_falls_back_to_csv_names():
    cj = json.dumps([{"name": "Keanu Reeves", "role": "Neo", "thumb": "https://x/t.jpg", "tvdbid": "7"}])
    flat = columns_to_flat(_cols(cast="Someone", cast_json=cj), "movie")
    assert flat["cast"] == [
        {"name": "Keanu Reeves", "role": "Neo", "thumb": "https://x/t.jpg", "profile": None, "tvdbId": "7"}
    ]
    csv = columns_to_flat(_cols(cast="Keanu Reeves, Carrie-Anne Moss"), "movie")
    assert csv["cast"] == [
        {"name": "Keanu Reeves", "role": None, "thumb": None, "profile": None, "tvdbId": None},
        {"name": "Carrie-Anne Moss", "role": None, "thumb": None, "profile": None, "tvdbId": None},
    ]
    broken = columns_to_flat(_cols(cast="A, B", cast_json="{not json"), "movie")
    assert [c["name"] for c in broken["cast"]] == ["A", "B"]


def test_episode_exposes_only_its_fields():
    flat = columns_to_flat(_cols(duration=2_700_000, premiered="2019-01-02"), "episode")
    assert set(flat) == set(FIELDS_BY_TYPE["episode"])
    assert flat["durationMs"] == 2_700_000


def test_flat_to_nested_groups_ids_and_ratings():
    flat = columns_to_flat(_cols(imdb_id="tt0133093", imdb_rating=7.0), "movie")
    nested = flat_to_nested(flat)
    assert nested["ids"] == {"imdb": "tt0133093", "tmdb": None, "tvdb": None, "wikidata": None}
    assert nested["ratings"]["imdb"] == {"value": 7.0, "votes": None}
    assert nested["ratings"]["tmdb"] is None
    assert "ids.imdb" not in nested


# ─── normalize_patch ────────────────────────────────────────────────────────


def n(patch, type_="movie", *, adult=False):
    return normalize_patch(patch, type_, is_adult=adult)


def test_normalization_trims_dedupes_and_flattens():
    out = n({
        "title": "  1917  ",
        "genres": ["Action", " Action ", "Drame"],
        "studios": ["  A  "],
        "ids": {"imdb": "tt0133093"},
        "ratings": {"imdb": {"value": 8, "votes": 5}, "tmdb": None},
        "premiered": "1999-03-31",
        "durationMs": 8_160_000,
        "youtubeTrailer": "dQw4w9WgXcQ",
        "year": 1999,
    })
    assert out["title"] == "1917"
    assert out["genres"] == ["Action", "Drame"]
    assert out["studios"] == ["A"]
    assert out["ids.imdb"] == "tt0133093"
    assert out["ratings.imdb"] == {"value": 8.0, "votes": 5}
    assert out["ratings.tmdb"] is None
    assert out["premiered"] == "1999-03-31" and out["year"] == 1999


def test_only_present_subfields_are_returned():
    assert n({"ids": {"tmdb": "603"}}) == {"ids.tmdb": "603"}
    assert n({}) == {}


def test_null_clears_nullable_fields_and_lists():
    out = n({"summary": None, "genres": [], "cast": [], "ids": {"imdb": None}, "durationMs": None})
    assert out == {"summary": None, "genres": [], "cast": [], "ids.imdb": None, "durationMs": None}


@pytest.mark.parametrize(
    "patch,field",
    [
        ({"title": None}, "title"),
        ({"title": "   "}, "title"),
        ({"title": "x" * 301}, "title"),
        ({"title": "a\x00b"}, "title"),
        ({"title": "a\nb"}, "title"),
        ({"originalTitle": ""}, "originalTitle"),
        ({"summary": "x" * 10_001}, "summary"),
        ({"tagline": "x" * 501}, "tagline"),
        ({"genres": ["Action, Drame"]}, "genres"),
        ({"genres": ["x"] * 21}, "genres"),
        ({"genres": ["x" * 65]}, "genres"),
        ({"genres": [""]}, "genres"),
        ({"studios": ["a,b"]}, "studios"),
        ({"studios": ["x"] * 11}, "studios"),
        ({"countries": ["US,FR"]}, "countries"),
        ({"contentRating": "x" * 33}, "contentRating"),
        ({"year": 1849}, "year"),
        ({"year": 2201}, "year"),
        ({"premiered": "1999-13-01"}, "premiered"),
        ({"premiered": "1999-3-1"}, "premiered"),
        ({"premiered": "31/03/1999"}, "premiered"),
        ({"durationMs": 0}, "durationMs"),
        ({"durationMs": 86_400_001}, "durationMs"),
        ({"ids": {"imdb": "0133093"}}, "ids.imdb"),
        ({"ids": {"imdb": "tt123"}}, "ids.imdb"),
        ({"ids": {"tmdb": "1234567890"}}, "ids.tmdb"),
        ({"ids": {"tmdb": "abc"}}, "ids.tmdb"),
        ({"ids": {"tvdb": "1" * 13}}, "ids.tvdb"),
        ({"ids": {"wikidata": "Q"}}, "ids.wikidata"),
        ({"ids": {"wikidata": "q42"}}, "ids.wikidata"),
        ({"ratings": {"imdb": {"value": 10.1}}}, "ratings.imdb"),
        ({"ratings": {"imdb": {"value": -0.1}}}, "ratings.imdb"),
        ({"ratings": {"imdb": {"value": float("nan")}}}, "ratings.imdb"),
        ({"ratings": {"tmdb": {"value": 5, "votes": -1}}}, "ratings.tmdb"),
        ({"youtubeTrailer": "short"}, "youtubeTrailer"),
        ({"youtubeTrailer": "dQw4w9WgXc!"}, "youtubeTrailer"),
        ({"cast": [{"name": ""}]}, "cast"),
        ({"cast": [{"name": "x" * 201}]}, "cast"),
        ({"cast": [{"name": "A"}] * 51}, "cast"),
        ({"cast": [{"name": "A", "thumb": "http://insecure/x.jpg"}]}, "cast"),
        ({"cast": [{"name": "A", "profile": "not a url"}]}, "cast"),
        ({"cast": [{"name": "A", "thumb": "https://x/" + "a" * 2048}]}, "cast"),
        ({"cast": [{"name": "A", "tvdbId": "abc"}]}, "cast"),
    ],
)
def test_invalid_values_name_the_offending_field(patch, field):
    with pytest.raises(FieldError) as exc:
        n(patch)
    assert exc.value.field == field


def test_status_is_only_writable_on_a_show():
    assert n({"status": "Ended"}, "show") == {"status": "Ended"}
    with pytest.raises(FieldError) as exc:
        n({"status": "Ended"}, "movie")
    assert exc.value.field == "status"


@pytest.mark.parametrize(
    "patch,field",
    [
        ({"originalTitle": "x"}, "originalTitle"),
        ({"tagline": "x"}, "tagline"),
        ({"genres": ["x"]}, "genres"),
        ({"studios": ["x"]}, "studios"),
        ({"countries": ["x"]}, "countries"),
        ({"ids": {"imdb": "tt0133093"}}, "ids.imdb"),
        ({"cast": [{"name": "A"}]}, "cast"),
        ({"year": 2000}, "year"),
        ({"youtubeTrailer": "dQw4w9WgXcQ"}, "youtubeTrailer"),
        ({"contentRating": "PG"}, "contentRating"),
        ({"status": "Ended"}, "status"),
    ],
)
def test_episode_refuses_movie_only_fields(patch, field):
    with pytest.raises(FieldError) as exc:
        n(patch, "episode")
    assert exc.value.field == field


def test_episode_accepts_its_own_fields():
    out = n({"title": "Pilot", "summary": "s", "premiered": "2019-01-02", "durationMs": 2_700_000,
             "ratings": {"imdb": {"value": 7.5, "votes": 3}}}, "episode")
    assert set(out) == {"title", "summary", "premiered", "durationMs", "ratings.imdb"}


def test_content_rating_is_refused_on_an_adult_item():
    for value in ("PG", None):  # clearing would drop the adult classification too
        with pytest.raises(FieldError) as exc:
            n({"contentRating": value}, adult=True)
        assert exc.value.field == "contentRating"
    assert n({"contentRating": "PG"}, adult=False) == {"contentRating": "PG"}


def test_title_is_not_cleaned_like_the_sync_does():
    assert n({"title": "1917"}) == {"title": "1917"}


def test_boundary_values_are_accepted():
    n({"year": 1850, "durationMs": 1, "ratings": {"imdb": {"value": 0}}})
    n({"year": 2200, "durationMs": 86_400_000, "ratings": {"imdb": {"value": 10, "votes": 0}}})
    n({"title": "x" * 300, "summary": "x" * 10_000, "genres": ["x" * 64] * 20})
    n({"cast": [{"name": "A", "thumb": "https://x/" + "a" * 2000}] * 50})


# ─── apply / diff / field_columns ───────────────────────────────────────────


def test_apply_patch_and_changed_names_use_normalized_equality():
    before = columns_to_flat(_cols(genres="Action, Drame", imdb_rating=7.5, imdb_votes=10), "movie")
    after = apply_patch(before, n({"genres": ["Action", "Drame"], "ratings": {"imdb": {"value": 7.5, "votes": 10}}}))
    assert changed_field_names(before, after) == []  # equal after normalisation
    after = apply_patch(before, n({"genres": ["Drame"], "ratings": {"imdb": {"value": 7.5000000001, "votes": 11}}}))
    assert changed_field_names(before, after) == ["genres", "ratings.imdb"]


def test_float_tolerance_is_1e_9():
    a = {"ratings.imdb": {"value": 7.5, "votes": 1}}
    assert changed_field_names(a, {"ratings.imdb": {"value": 7.5 + 5e-10, "votes": 1}}) == []
    assert changed_field_names(a, {"ratings.imdb": {"value": 7.5 + 5e-9, "votes": 1}}) == ["ratings.imdb"]


def test_field_columns_conversions():
    assert field_columns("title", {"title": "Heat"}) == {"title": "Heat", "title_sortable": "heat"}
    assert field_columns("genres", {"genres": ["A", "B"]}) == {"genres": "A, B"}
    assert field_columns("genres", {"genres": []}) == {"genres": None}
    assert field_columns("studios", {"studios": ["S"]}) == {"studio": "S"}
    assert field_columns("countries", {"countries": ["US", "FR"]}) == {"country": "US, FR"}
    assert field_columns("durationMs", {"durationMs": 5000}) == {"duration": 5000}
    assert field_columns("ids.imdb", {"ids.imdb": "tt0133093"}) == {"imdb_id": "tt0133093"}
    assert field_columns("ratings.tmdb", {"ratings.tmdb": {"value": 8.0, "votes": 2}}) == {
        "tmdb_rating": 8.0, "tmdb_votes": 2,
    }
    assert field_columns("ratings.imdb", {"ratings.imdb": None}) == {"imdb_rating": None, "imdb_votes": None}
    assert field_columns("summary", {"summary": None}) == {"summary": None}


def test_cast_writes_json_and_csv_of_names():
    cast = [
        {"name": "Keanu Reeves", "role": "Neo", "thumb": "https://x/a.jpg", "profile": None, "tvdbId": "7"},
        {"name": "Keanu Reeves", "role": "Neo 2", "thumb": None, "profile": None, "tvdbId": None},
        {"name": "Laurence Fishburne", "role": None, "thumb": None, "profile": None, "tvdbId": None},
    ]
    cols = field_columns("cast", {"cast": cast})
    assert cols["cast"] == "Keanu Reeves, Laurence Fishburne"
    stored = json.loads(cols["cast_json"])
    assert stored[0] == {"name": "Keanu Reeves", "role": "Neo", "thumb": "https://x/a.jpg", "profile": None, "tvdbid": "7"}
    assert len(stored) == 3  # order and roles preserved (two roles for one actor)
    assert field_columns("cast", {"cast": []}) == {"cast": None, "cast_json": None}


def test_cast_round_trips_through_the_columns():
    entry = {"name": "Éva Ünal", "role": "Ç", "thumb": None, "profile": "https://x/p.jpg", "tvdbId": None}
    cols = field_columns("cast", {"cast": [entry]})
    flat = columns_to_flat(_cols(**cols), "movie")
    assert flat["cast"] == [entry]
    assert "Éva Ünal" in cols["cast_json"]  # ensure_ascii=False


def test_limits_are_exposed_for_fields_route():
    assert LIMITS["title"] == {"min": 1, "max": 300}
    assert LIMITS["genres"]["maxItems"] == 20
    assert LIMITS["cast"]["maxItems"] == 50
    assert LIMITS["year"] == {"min": 1850, "max": 2200}
    assert LIMITS["durationMs"] == {"min": 1, "max": 86_400_000}
