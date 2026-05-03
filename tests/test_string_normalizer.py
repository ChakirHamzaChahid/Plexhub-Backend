"""Tests for app.utils.string_normalizer.parse_title_and_year."""
import pytest

from app.utils.string_normalizer import parse_title_and_year, normalize_for_sorting


class TestParseTitleAndYear:
    # --- Basic cases ---

    def test_plain_title(self):
        assert parse_title_and_year("The Matrix") == ("The Matrix", None)

    def test_title_with_year(self):
        assert parse_title_and_year("The Matrix (1999)") == ("The Matrix", 1999)

    def test_empty_returns_unknown(self):
        assert parse_title_and_year("") == ("Unknown", None)

    # --- Existing IPTV prefixes (regression) ---

    def test_pipe_prefix(self):
        assert parse_title_and_year("|VM| Le Monde apres nous (2023)") == (
            "Le Monde apres nous",
            2023,
        )

    def test_bracket_prefix(self):
        assert parse_title_and_year("[VF] Foo (2020)") == ("Foo", 2020)

    # --- Country prefix (NEW) ---

    def test_country_prefix_fr(self):
        assert parse_title_and_year("FR - Better Man (2024)") == ("Better Man", 2024)

    def test_country_prefix_nf(self):
        assert parse_title_and_year("NF - Chernobyl") == ("Chernobyl", None)

    def test_country_prefix_sc(self):
        assert parse_title_and_year("SC - Some Show (2010)") == ("Some Show", 2010)

    def test_country_prefix_three_letters_not_stripped(self):
        # 3 uppercase letters should NOT match — avoids false positives like "USA - " or
        # a real title starting with 3 caps.
        assert parse_title_and_year("USA - Network (2010)") == ("USA - Network", 2010)

    def test_country_prefix_lowercase_not_stripped(self):
        assert parse_title_and_year("fr - Foo") == ("fr - Foo", None)

    # --- Quality suffix as bare word (NEW) ---

    def test_trailing_lq(self):
        assert parse_title_and_year("FR - Aquaman et le Royaume perdu (2023) LQ") == (
            "Aquaman et le Royaume perdu",
            2023,
        )

    def test_trailing_hq(self):
        assert parse_title_and_year("Foo (2020) HQ") == ("Foo", 2020)

    def test_trailing_fhd_word(self):
        assert parse_title_and_year("Foo (2020) FHD") == ("Foo", 2020)

    def test_quality_word_case_insensitive(self):
        assert parse_title_and_year("Foo (2020) lq") == ("Foo", 2020)

    # --- Quality suffix in brackets (NEW) ---

    def test_trailing_bracket_quality(self):
        assert parse_title_and_year("Black Widow (2021) [FHD MULTi-SUBAR]") == (
            "Black Widow",
            2021,
        )

    def test_trailing_bracket_vostfr(self):
        assert parse_title_and_year("Foo (2020) [VOSTFR]") == ("Foo", 2020)

    def test_trailing_bracket_4k(self):
        assert parse_title_and_year("Foo (2020) [4K]") == ("Foo", 2020)

    def test_trailing_bracket_no_year(self):
        assert parse_title_and_year("Some Show [FHD]") == ("Some Show", None)

    # --- Stacked / combined patterns ---

    def test_country_prefix_plus_trailing_quality(self):
        assert parse_title_and_year("FR - Foo (2020) [FHD] LQ") == ("Foo", 2020)

    def test_pipe_prefix_plus_country_prefix(self):
        # Edge case: leading pipe stripped first, then country prefix
        assert parse_title_and_year("|VM| FR - Foo (2020)") == ("Foo", 2020)

    def test_multiple_trailing_brackets(self):
        assert parse_title_and_year("Foo (2020) [FHD] [VOSTFR]") == ("Foo", 2020)

    # --- Idempotence ---

    @pytest.mark.parametrize(
        "raw",
        [
            "FR - Better Man (2024)",
            "FR - Aquaman (2023) LQ",
            "Black Widow (2021) [FHD MULTi-SUBAR]",
            "NF - Chernobyl",
            "Plain Title",
            "Plain Title (2020)",
        ],
    )
    def test_idempotent(self, raw):
        title1, year1 = parse_title_and_year(raw)
        title2, year2 = parse_title_and_year(title1 if year1 is None else f"{title1} ({year1})")
        assert title1 == title2
        assert year1 == year2

    # --- Year extraction edge cases ---

    def test_year_only_at_end(self):
        # Year mid-title should not be extracted
        assert parse_title_and_year("Live in (1999) Concert") == (
            "Live in (1999) Concert",
            None,
        )

    def test_empty_after_strip_returns_unknown(self):
        assert parse_title_and_year("FR - ") == ("Unknown", None)


class TestNormalizeForSorting:
    def test_strips_leading_the(self):
        assert normalize_for_sorting("The Matrix") == "Matrix"

    def test_strips_leading_le(self):
        assert normalize_for_sorting("Le Monde") == "Monde"

    def test_removes_diacritics(self):
        assert normalize_for_sorting("Amélie") == "Amelie"


class TestParseTitleYearAndSuffix:
    """Suffix-aware parser used by the Jellyfin-style folder naming."""

    def _p(self, raw):
        from app.utils.string_normalizer import parse_title_year_and_suffix
        return parse_title_year_and_suffix(raw)

    def test_canonical(self):
        assert self._p("Les Experts (2000)") == ("Les Experts", 2000, None)

    def test_us_suffix(self):
        assert self._p("Les Experts (2000) (US)") == ("Les Experts", 2000, "US")

    def test_hd_suffix(self):
        assert self._p("Les Experts (2000) (HD)") == ("Les Experts", 2000, "HD")

    def test_country_prefix_then_year_then_suffix(self):
        assert self._p("FR - Les Experts (2000) (US)") == ("Les Experts", 2000, "US")

    def test_year_anywhere(self):
        # Year may appear before the qualifier; result must still pull both.
        assert self._p("Foo (2020) (HD)") == ("Foo", 2020, "HD")

    def test_qualifier_only_no_year(self):
        assert self._p("Foo (US)") == ("Foo", None, "US")

    def test_multiple_qualifiers_concatenated(self):
        assert self._p("Foo (2020) (US) (HD)") == ("Foo", 2020, "US HD")

    def test_quality_brackets_stripped(self):
        # [FHD] is trailing junk handled before paren extraction.
        assert self._p("Better Man (2024) [FHD]") == ("Better Man", 2024, None)

    def test_qualifier_before_year(self):
        # Less common but possible; both still recovered.
        assert self._p("Foo (US) (2020)") == ("Foo", 2020, "US")

    def test_empty(self):
        assert self._p("") == ("Unknown", None, None)
