"""Tests for app.utils.string_normalizer."""

from app.utils.string_normalizer import normalize_for_sorting


class TestNormalizeForSorting:
    # Aligned on Android StringNormalizer: lowercase + strip punctuation.
    def test_strips_leading_the(self):
        assert normalize_for_sorting("The Matrix") == "matrix"

    def test_strips_leading_le(self):
        assert normalize_for_sorting("Le Monde") == "monde"

    def test_removes_diacritics(self):
        assert normalize_for_sorting("Amélie") == "amelie"


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


class TestParseTitleYearAndSuffixEdgeCases:
    """Cases discovered while auditing real Xtream catalog data."""

    def _p(self, raw):
        from app.utils.string_normalizer import parse_title_year_and_suffix
        return parse_title_year_and_suffix(raw)

    def test_vfr_quality_stripped(self):
        # "VFR" is a French restored-version tag some IPTV providers append.
        assert self._p("6ixtynin9 La série VFR") == ("6ixtynin9 La série", None, None)

    def test_vfi_quality_stripped(self):
        assert self._p("Foo VFI") == ("Foo", None, None)

    def test_vfb_quality_stripped(self):
        assert self._p("Foo VFB") == ("Foo", None, None)

    def test_empty_after_strip_keeps_raw_title(self):
        # Pathological Xtream entry: title is literally just year + qualifier.
        # We must NOT collapse to "Unknown" — that would collide with every
        # other empty-after-strip entry.
        assert self._p("(2021) (US)") == ("(2021) (US)", None, None)

    def test_only_qualifier_keeps_raw(self):
        # Even with no year, an empty post-strip title falls back to raw.
        assert self._p("(US)") == ("(US)", None, None)

    def test_truly_empty_returns_unknown(self):
        assert self._p("") == ("Unknown", None, None)
        assert self._p("   ") == ("Unknown", None, None)
