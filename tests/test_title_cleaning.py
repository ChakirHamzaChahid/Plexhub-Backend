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

    @pytest.mark.parametrize("raw, expected_title", [
        # A bare space was NOT accepted as a prefix separator, so 213 real
        # catalogue titles reached TMDB as "FR <titre>" and never matched.
        ("FR Les Châtiments", "Les Châtiments"),
        ("FR Casque et talons hauts", "Casque et talons hauts"),
        ("VOSTFR Parasite", "Parasite"),
        ("VF Le Roi Lion", "Le Roi Lion"),
        # `[` as separator: no closing bracket ever comes, so the leftover
        # bracket pass could not help either.
        ("FR[ Les Enfants perdus", "Les Enfants perdus"),
        # Separator with no space after it.
        ("FR|Les Visiteurs", "Les Visiteurs"),
        # A quality tag in front hides the language prefix behind it, and
        # quality tags are only stripped after the prefix pass.
        ("4K FR Avatar", "Avatar"),
        ("FR - Le Parrain", "Le Parrain"),
    ])
    def test_strips_panel_language_prefixes(self, raw, expected_title):
        assert clean_title(raw)[0] == expected_title

    @pytest.mark.parametrize("raw", [
        # Every one of these starts with 2-4 uppercase letters followed by a
        # space. Stripping that blindly to fix "FR " would have destroyed
        # them — LEGO(33), WWE(16), USS(4), OSS(3) rows in the real catalogue.
        "LEGO Batman Le Film",
        "USS Indianapolis",
        "OSS 117 Rio ne répond plus",
        "WWE Raw",
        "THE Matrix",
        "US Marshals",
        "IT Chapter Two",
        "UK 18",
        "IFRI Story",
        # Pre-existing bug fixed in passing: `:` is ordinary punctuation after
        # an acronym, and accepting it as a prefix separator turned this into
        # "Los Angeles".
        "NCIS: Los Angeles",
    ])
    def test_never_eats_a_real_title_word(self, raw):
        assert clean_title(raw)[0] == raw

    @pytest.mark.parametrize("raw, expected", [
        # The number IS the title. Reading it as a year left an EMPTY title,
        # which fell back to the RAW string — prefix included, unmatchable.
        ("FR| 1992", ("1992", None)),
        ("1917", ("1917", None)),
        ("2012", ("2012", None)),
        ("FR| 1978 [VOSTFR]", ("1978", None)),
        # A bare number too far in the future is not a release year.
        ("Blade Runner 2049", ("Blade Runner 2049", None)),
        # ...but a plausible one still is, and a parenthesised year is
        # trusted as-is because the author marked it.
        ("Le Film 2024", ("Le Film", 2024)),
        ("Le Parrain (1972)", ("Le Parrain", 1972)),
    ])
    def test_a_number_that_is_the_title_is_not_a_year(self, raw, expected):
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
