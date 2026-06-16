"""Tests for the hardened Xtream title cleaner + normalizer (scraping plan §4)."""
import pytest

from app.utils.string_normalizer import clean_title, normalize_for_sorting


class TestCleanTitle:
    @pytest.mark.parametrize("raw, expected", [
        ("Avatar (FR)", ("Avatar", None)),
        ("Avatar (2009) (FR)", ("Avatar", 2009)),
        ("VOSTFR - Dune", ("Dune", None)),
        ("Fr - Le Parrain", ("Le Parrain", None)),
        ("FRA - Le Parrain (1972)", ("Le Parrain", 1972)),
        ("Spider-Man : No Way Home (2021) MULTI 1080p", ("Spider-Man : No Way Home", 2021)),
        ("Le.Cygne.Noir.2010.MULTI.1080p", ("Le Cygne Noir", 2010)),
        ("Oppenheimer 2023", ("Oppenheimer", 2023)),
        ("John Wick [4K] [MULTI]", ("John Wick", None)),
        ("|VM| Tulsa King  (2022)", ("Tulsa King", 2022)),
        ("Black Widow (2021) [FHD MULTi-SUBAR]", ("Black Widow", 2021)),
        ("Skarb narodow-Ksiega tajemnic (2007) [PL]", ("Skarb narodow-Ksiega tajemnic", 2007)),
    ])
    def test_table(self, raw, expected):
        assert clean_title(raw) == expected

    def test_empty_falls_back_to_raw(self):
        # Nothing but tags/year -> keep raw rather than collapse to "Unknown".
        title, year = clean_title("(2020)")
        assert year == 2020
        assert title == "(2020)"

    def test_none_safe(self):
        assert clean_title("") == ("Unknown", None)


class TestNormalizeForSorting:
    def test_strips_punctuation_and_lowercases(self):
        assert normalize_for_sorting("Spider-Man : No Way Home") == "spider man no way home"

    def test_strips_leading_article(self):
        assert normalize_for_sorting("The Matrix") == "matrix"

    def test_strips_accents(self):
        assert normalize_for_sorting("Les Misérables") == "miserables"

    def test_collapses_whitespace(self):
        assert normalize_for_sorting("A   :  B") == "b"
