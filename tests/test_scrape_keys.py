"""B1-07 : clés de cache canoniques de la passerelle de scraping (ADR 0009 D2.2).

Le test des vecteurs d'or GÉNÈRE `tests/fixtures/scrape_key_vectors.json` (lu
par l'oracle de plexhubTMM) et échoue si le fichier commité diffère. Pour le
régénérer après une évolution volontaire : `REGEN_SCRAPE_KEY_VECTORS=1 pytest
tests/test_scrape_keys.py` (et incrémenter KEY_VERSION si la normalisation change).
"""
from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path

import pytest

from app.services import scrape_cache_service, scrape_keys
from app.services.scrape_keys import (
    KEY_VERSION,
    SCHEMA_VERSION,
    CacheKey,
    EmptyNormalizedTitle,
    InvalidCacheKey,
    find_key,
    match_key,
    movie_key,
    omdb_key,
    parse,
    season_key,
    show_key,
    validate_language,
)
from app.utils.string_normalizer import normalize_for_sorting

VECTORS_PATH = Path(__file__).resolve().parent / "fixtures" / "scrape_key_vectors.json"

# (mediaType, title, year, language)
_VECTOR_INPUTS = [
    ("movie", "Terminator (VF)", 1984, "fr-FR"),  # vecteur existant
    ("show", "Breaking Bad", None, "en-US"),  # vecteur existant
    ("movie", "Amélie Poulain", 2001, "fr-FR"),  # accents
    ("movie", "Léon", 1994, "fr-FR"),
    ("movie", "The Matrix", 1999, "fr-FR"),  # article anglais
    ("movie", "Les Misérables", 2012, "fr-FR"),  # article français
    ("movie", "L'Armée des ombres", 1969, "fr-FR"),  # article élidé
    ("movie", "1917", 2019, "fr-FR"),  # chiffres seuls
    ("movie", "603", None, "fr-FR"),  # chiffres seuls, sans année (piège d'analyse)
    ("show", "24", 2001, "en-US"),
    ("movie", "Mission：Impossible｜Fallout", 2018, "fr-FR"),  # pleine chasse ｜ et ：
    ("movie", "Ocean's Eleven", 2001, "en-US"),  # apostrophe droite
    ("movie", "Ocean’s Eleven", 2001, "en-US"),  # apostrophe typographique
    ("movie", "Se7en", None, "fr-FR"),  # année absente
    ("movie", "  Le   Parrain  ", 1972, "fr-FR"),  # espaces multiples
    ("movie", "!!!", 1999, "fr-FR"),  # ponctuation seule -> vide -> erreur
    ("show", "...", None, "fr-FR"),  # ponctuation seule -> vide -> erreur
]


def _vector(media_type, title, year, language):
    normalized = normalize_for_sorting(title)
    try:
        cache_key = match_key(media_type, title, year, language).to_text()
    except EmptyNormalizedTitle:
        cache_key = None
    return {
        "mediaType": media_type,
        "title": title,
        "year": year,
        "language": language,
        "normalized": normalized,
        "cacheKey": cache_key,
    }


def _render_vectors() -> str:
    vectors = [_vector(*inputs) for inputs in _VECTOR_INPUTS]
    vectors.sort(key=lambda v: (v["mediaType"], v["title"], v["year"] or 0, v["language"]))
    return json.dumps(vectors, ensure_ascii=False, indent=2) + "\n"


class TestGoldenVectors:
    def test_versions_are_declared_here(self):
        assert KEY_VERSION == 1
        assert SCHEMA_VERSION == 1

    def test_make_key_existing_vectors(self):
        # Les deux vecteurs historiques de tests/test_enrichment_scraping.py.
        assert scrape_cache_service.make_key("movie", "Terminator (VF)", 1984) == "movie|terminator vf|1984"
        assert scrape_cache_service.make_key("show", "Breaking Bad", None) == "show|breaking bad|"
        assert match_key("movie", "Terminator (VF)", 1984, "fr-FR").to_text() == "1|fr-FR|movie|terminator vf|1984"
        assert match_key("show", "Breaking Bad", None, "en-US").to_text() == "1|en-US|show|breaking bad|"

    @pytest.mark.parametrize(
        ("media_type", "title", "year", "expected"),
        [
            ("movie", "Amélie Poulain", 2001, "movie|amelie poulain|2001"),  # accents
            ("movie", "The Matrix", 1999, "movie|matrix|1999"),  # article
            ("movie", "Les Misérables", 2012, "movie|miserables|2012"),
            ("movie", "L'Armée des ombres", 1969, "movie|armee des ombres|1969"),  # l'
            ("movie", "1917", 2019, "movie|1917|2019"),  # chiffres seuls
            ("movie", "Mission：Impossible｜Fallout", 2018, "movie|mission impossible fallout|2018"),
            ("movie", "Ocean's Eleven", 2001, "movie|ocean s eleven|2001"),  # apostrophe
            ("movie", "Ocean’s Eleven", 2001, "movie|ocean s eleven|2001"),
            ("movie", "Se7en", None, "movie|se7en|"),  # année absente
        ],
    )
    def test_make_key_vectors(self, media_type, title, year, expected):
        assert scrape_cache_service.make_key(media_type, title, year) == expected
        assert match_key(media_type, title, year, "fr-FR").key == expected

    def test_full_width_bar_never_leaves_a_pipe_in_the_key(self):
        key = match_key("movie", "A｜B", 2000, "fr-FR").key
        assert key.count("|") == 2

    @pytest.mark.parametrize("title", ["!!!", "...", "?", "   ", "()", "—", "｜｜"])
    def test_punctuation_only_title_is_refused(self, title):
        # US2-AC5 / X12 : "movie||1999" serait commune à tous ces titres.
        with pytest.raises(EmptyNormalizedTitle):
            match_key("movie", title, 1999, "fr-FR")

    def test_empty_title_error_is_an_invalid_cache_key(self):
        with pytest.raises(InvalidCacheKey):
            match_key("movie", "", None, "fr-FR")

    def test_title_603_without_year_is_a_match_not_a_detail(self):
        match = match_key("movie", "603", None, "fr-FR")
        assert match.family == "match"
        assert match.to_text() == "1|fr-FR|movie|603|"
        assert parse("1|fr-FR|movie|603|") == match

        detail = movie_key(603, "fr-FR")
        assert detail.family == "movie"
        assert detail.to_text() == "1|fr-FR|movie|603"
        assert parse("1|fr-FR|movie|603") == detail
        assert match != detail

    def test_fixture_file_matches_generated_vectors(self):
        generated = _render_vectors()
        if os.environ.get("REGEN_SCRAPE_KEY_VECTORS") == "1":
            VECTORS_PATH.parent.mkdir(parents=True, exist_ok=True)
            VECTORS_PATH.write_bytes(generated.encode("utf-8"))
        assert VECTORS_PATH.exists(), "relancer avec REGEN_SCRAPE_KEY_VECTORS=1"
        committed = VECTORS_PATH.read_bytes().decode("utf-8").replace("\r\n", "\n")
        assert committed == generated, (
            "scrape_key_vectors.json diffère des vecteurs générés ; si c'est voulu, "
            "REGEN_SCRAPE_KEY_VECTORS=1 et KEY_VERSION += 1 si la normalisation change"
        )

    def test_fixture_is_valid_and_complete(self):
        data = json.loads(VECTORS_PATH.read_bytes().decode("utf-8"))
        assert len(data) == len(_VECTOR_INPUTS)
        assert all(set(v) == {"mediaType", "title", "year", "language", "normalized", "cacheKey"} for v in data)
        assert any(v["cacheKey"] is None for v in data)  # les cas refusés sont exportés
        for v in data:
            if v["cacheKey"] is not None:
                assert parse(v["cacheKey"]).language == v["language"]


_ROUND_TRIP = [
    (match_key("movie", "Matrix", 1999, "fr-FR"), "1|fr-FR|movie|matrix|1999"),
    (match_key("show", "Breaking Bad", None, "en-US"), "1|en-US|show|breaking bad|"),
    (movie_key(603, "fr-FR"), "1|fr-FR|movie|603"),
    (show_key(1396, "en-US"), "1|en-US|show|1396"),
    (season_key(1396, 0, "fr-FR"), "1|fr-FR|season|1396|0"),
    (season_key(1396, 12, "fr-FR"), "1|fr-FR|season|1396|12"),
    (omdb_key("tt0133093"), "1|-|omdb|tt0133093"),
    (find_key("imdb", "tt0133093"), "1|-|find|imdb|tt0133093"),
    (find_key("tvdb", "81189"), "1|-|find|tvdb|81189"),
]


class TestRoundTrip:
    @pytest.mark.parametrize(("key", "text"), _ROUND_TRIP, ids=[t for _, t in _ROUND_TRIP])
    def test_to_text_and_parse_round_trip(self, key, text):
        assert key.to_text() == text
        assert parse(text) == key
        assert parse(key.to_text()).to_text() == text

    def test_every_family_is_covered(self):
        assert {k.family for k, _ in _ROUND_TRIP} == set(scrape_keys.FAMILIES)

    def test_cache_key_is_frozen(self):
        key = movie_key(603, "fr-FR")
        with pytest.raises(dataclasses.FrozenInstanceError):
            key.language = "en-US"  # type: ignore[misc]

    def test_cache_key_cannot_be_built_malformed(self):
        with pytest.raises(InvalidCacheKey):
            CacheKey("movie", 1, "fr-FR", "movie|0603")
        with pytest.raises(InvalidCacheKey):
            CacheKey("bogus", 1, "fr-FR", "movie|603")

    def test_foreign_version_still_parses(self):
        # La lecture filtre sur KEY_VERSION ; l'analyse nomme toute version positive.
        key = parse("7|fr-FR|movie|603")
        assert key.key_version == 7
        assert key.to_text() == "7|fr-FR|movie|603"

    def test_family_is_decided_by_segment_count_without_filtering_empties(self):
        assert parse("1|fr-FR|show|breaking bad|").family == "match"
        assert parse("1|fr-FR|show|1396").family == "show"


_MALFORMED = [
    "",
    "1",
    "1|fr-FR",
    "1|fr-FR|movie",
    "1|fr-FR|movie|603|1999|x",  # 6 segments
    "1|fr-FR|movie||1999",  # titre vide
    "1|fr-FR|movie| matrix|1999",  # titre non rogné
    "1|fr-FR|movie|matrix|19999",  # année à 5 chiffres
    "1|fr-FR|movie|matrix|abc",
    "1|fr-FR|movie|0603",  # zéro de tête
    "1|fr-FR|movie|0",
    "1|fr-FR|movie|-5",
    "1|fr-FR|movie|603 ",
    "1|fr-FR|movie|603\n",  # `$` laisserait passer le saut de ligne
    "1|fr-FR|movie|١٢٣",  # chiffres arabo-indiens
    "1|fr-FR|movie|1000000000",  # > 999 999 999
    "1|fr-FR|show|1396|extra|x",
    "0|fr-FR|movie|603",  # version non positive
    "01|fr-FR|movie|603",
    "x|fr-FR|movie|603",
    "-1|fr-FR|movie|603",
    "1|fr_FR|movie|603",
    "1|FR-fr|movie|603",
    "1|fr|movie|603",
    "1|fr-FRA|movie|603",
    "1|-|movie|603",  # '-' réservé à omdb/find
    "1||movie|603",
    "1|fr-FR|tv|603",  # type inconnu
    "1|fr-FR|episode|1|1",
    "1|fr-FR|season|1396",  # saison manquante
    "1|fr-FR|season|1396|1000",
    "1|fr-FR|season|1396|-1",
    "1|fr-FR|season|1396|01",
    "1|fr-FR|season|0|1",
    "1|fr-FR|season|1396|1|x",
    "1|fr-FR|omdb|tt0133093",  # langue doit valoir '-'
    "1|-|omdb|tt123456",  # 6 chiffres
    "1|-|omdb|tt12345678901",  # 11 chiffres
    "1|-|omdb|0133093",
    "1|-|omdb|tt0133093|x",
    "1|-|omdb|TT0133093",
    "1|-|find|imdb|",
    "1|-|find|imdb|0133093",
    "1|-|find|imdb|tt0133093|x",
    "1|-|find|tvdb|007",  # forme non canonique
    "1|-|find|tvdb|0",
    "1|-|find|tvdb|1234567890123",  # 13 chiffres
    "1|-|find|tmdb|1",
    "1|fr-FR|find|imdb|tt0133093",
    "1|-|find|imdb",
    "1|-|bogus|1",
]


class TestMalformed:
    @pytest.mark.parametrize("text", _MALFORMED)
    def test_malformed_forms_are_rejected(self, text):
        with pytest.raises(InvalidCacheKey):
            parse(text)

    @pytest.mark.parametrize("value", [None, 5, b"1|fr-FR|movie|603"])
    def test_non_string_is_rejected(self, value):
        with pytest.raises(InvalidCacheKey):
            parse(value)  # type: ignore[arg-type]

    def test_invalid_cache_key_is_a_value_error(self):
        assert issubclass(InvalidCacheKey, ValueError)


class TestConstructors:
    def test_tvdb_id_is_normalised_to_an_integer(self):
        # X19 : "007" et "7" donnent la même clé.
        assert find_key("tvdb", "007") == find_key("tvdb", "7")
        assert find_key("tvdb", "007").to_text() == "1|-|find|tvdb|7"
        assert find_key("tvdb", "000081189").key == "find|tvdb|81189"

    @pytest.mark.parametrize("bad", ["", "abc", "0", "00", "-3", "1234567890123"])
    def test_bad_tvdb_ids_are_rejected(self, bad):
        with pytest.raises(InvalidCacheKey):
            find_key("tvdb", bad)

    def test_imdb_id_is_not_rewritten(self):
        assert find_key("imdb", "tt0133093").key == "find|imdb|tt0133093"
        with pytest.raises(InvalidCacheKey):
            find_key("imdb", "tt013")

    def test_unknown_find_source(self):
        with pytest.raises(InvalidCacheKey):
            find_key("tmdb", "1")

    def test_omdb_and_find_force_no_language(self):
        assert omdb_key("tt0133093").language == "-"
        assert find_key("imdb", "tt0133093").language == "-"

    @pytest.mark.parametrize("bad", ["tt1", "0133093", "TT0133093", "tt0133093 ", ""])
    def test_bad_imdb_id(self, bad):
        with pytest.raises(InvalidCacheKey):
            omdb_key(bad)

    @pytest.mark.parametrize("bad", [0, -1, 1_000_000_000, "603", None, True, 1.5])
    def test_bad_tmdb_ids(self, bad):
        with pytest.raises(InvalidCacheKey):
            movie_key(bad, "fr-FR")  # type: ignore[arg-type]
        with pytest.raises(InvalidCacheKey):
            show_key(bad, "fr-FR")  # type: ignore[arg-type]

    @pytest.mark.parametrize("bad", [-1, 1000, "1", None, True])
    def test_bad_season_numbers(self, bad):
        with pytest.raises(InvalidCacheKey):
            season_key(1396, bad, "fr-FR")  # type: ignore[arg-type]

    def test_season_zero_is_valid(self):
        assert season_key(1396, 0, "fr-FR").to_text() == "1|fr-FR|season|1396|0"

    @pytest.mark.parametrize("bad", ["movies", "tv", "", "Movie"])
    def test_match_key_media_type(self, bad):
        with pytest.raises(InvalidCacheKey):
            match_key(bad, "Matrix", 1999, "fr-FR")

    @pytest.mark.parametrize("bad", [0, -1, 10_000, True, "1999", 1999.0])
    def test_match_key_year(self, bad):
        with pytest.raises(InvalidCacheKey):
            match_key("movie", "Matrix", bad, "fr-FR")  # type: ignore[arg-type]

    def test_match_key_year_zero_is_not_silently_dropped(self):
        # make_key ferait `0 or ''` -> '' : on refuse plutôt que de changer de clé.
        with pytest.raises(InvalidCacheKey):
            match_key("movie", "Matrix", 0, "fr-FR")

    def test_match_key_delegates_to_make_key(self, monkeypatch):
        monkeypatch.setattr(scrape_cache_service, "make_key", lambda t, ti, y: f"{t}|sentinel|{y or ''}")
        assert match_key("movie", "Matrix", 1999, "fr-FR").key == "movie|sentinel|1999"


class TestLanguage:
    @pytest.mark.parametrize("value", ["fr-FR", "en-US", "pt-BR"])
    def test_valid(self, value):
        assert validate_language(value) == value

    @pytest.mark.parametrize(
        "value", ["", "fr", "FR-fr", "fr_FR", "fr-fr", "fr-FRA", "-", "fr-FR\n", " fr-FR", None, 5]
    )
    def test_invalid(self, value):
        with pytest.raises(InvalidCacheKey):
            validate_language(value)  # type: ignore[arg-type]

    @pytest.mark.parametrize("ctor", [
        lambda lang: movie_key(603, lang),
        lambda lang: show_key(603, lang),
        lambda lang: season_key(603, 1, lang),
        lambda lang: match_key("movie", "Matrix", 1999, lang),
    ])
    def test_constructors_validate_language(self, ctor):
        with pytest.raises(InvalidCacheKey):
            ctor("fr")
        with pytest.raises(InvalidCacheKey):
            ctor("-")
