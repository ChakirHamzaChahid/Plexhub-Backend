"""Unit tests for the module-level TMDb certification parsers.

These tests are pure — no HTTP, no DB, no async.  They exercise
_parse_movie_certification and _parse_tv_certification directly with sample
JSON fixtures that mirror the real TMDb append_to_response shape.

Scenarios covered:
  Movie:
    - preferred region (FR) returned when present
    - US fallback when preferred region absent
    - theatrical release type (type==3) preferred over other types
    - first non-empty cert returned when no theatrical entry
    - first-country fallback when neither FR nor US present
    - empty / missing release_dates → None
    - all entries have empty certification → None

  TV:
    - preferred region (FR) returned when present
    - US fallback when preferred region absent
    - first-country fallback when neither FR nor US present
    - empty / missing content_ratings → None
    - rating is whitespace-only → None
"""
from __future__ import annotations

import pytest

from app.services.tmdb_service import (
    _parse_movie_certification,
    _parse_tv_certification,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _movie_payload(results: list[dict]) -> dict:
    """Wrap a release_dates results list in the TMDb top-level shape."""
    return {"release_dates": {"results": results}}


def _tv_payload(results: list[dict]) -> dict:
    """Wrap a content_ratings results list in the TMDb top-level shape."""
    return {"content_ratings": {"results": results}}


def _rd(iso: str, *releases: dict) -> dict:
    """Build one release_dates country entry."""
    return {"iso_3166_1": iso, "release_dates": list(releases)}


def _release(cert: str, rtype: int = 3) -> dict:
    """Build one release_date entry (type 3 = theatrical by default)."""
    return {"certification": cert, "type": rtype}


def _cr(iso: str, rating: str) -> dict:
    """Build one content_ratings country entry."""
    return {"iso_3166_1": iso, "rating": rating}


# ---------------------------------------------------------------------------
# Movie certification — preferred region (FR from 'fr-FR')
# ---------------------------------------------------------------------------

class TestParseMovieCertification:
    def test_preferred_region_fr_returned(self, monkeypatch):
        """FR cert is picked when settings.TMDB_LANGUAGE is fr-FR."""
        from app.services import tmdb_service as mod
        monkeypatch.setattr(mod.settings, "TMDB_LANGUAGE", "fr-FR")

        payload = _movie_payload([
            _rd("FR", _release("U")),
            _rd("US", _release("G")),
        ])
        assert _parse_movie_certification(payload) == "U"

    def test_us_fallback_when_preferred_absent(self, monkeypatch):
        """US cert used when the preferred region is not in the results."""
        from app.services import tmdb_service as mod
        monkeypatch.setattr(mod.settings, "TMDB_LANGUAGE", "fr-FR")

        payload = _movie_payload([
            _rd("US", _release("PG-13")),
            _rd("GB", _release("12A")),
        ])
        assert _parse_movie_certification(payload) == "PG-13"

    def test_theatrical_type3_preferred_over_other_types(self, monkeypatch):
        """Within a country entry, type==3 (theatrical) should win over type==1 etc."""
        from app.services import tmdb_service as mod
        monkeypatch.setattr(mod.settings, "TMDB_LANGUAGE", "en-US")

        payload = _movie_payload([
            _rd("US",
                _release("NR", rtype=1),    # premiere — should lose
                _release("PG", rtype=3),    # theatrical — should win
                _release("PG-13", rtype=5), # digital — should lose
                ),
        ])
        assert _parse_movie_certification(payload) == "PG"

    def test_non_theatrical_cert_when_no_theatrical_entry(self, monkeypatch):
        """If there is no type==3 entry at all, return the first non-empty cert."""
        from app.services import tmdb_service as mod
        monkeypatch.setattr(mod.settings, "TMDB_LANGUAGE", "en-US")

        payload = _movie_payload([
            _rd("US",
                _release("", rtype=1),        # empty — skipped
                _release("PG-13", rtype=5),   # digital — only non-empty
                ),
        ])
        assert _parse_movie_certification(payload) == "PG-13"

    def test_first_country_fallback(self, monkeypatch):
        """When neither preferred nor US is present, first country with a cert wins."""
        from app.services import tmdb_service as mod
        monkeypatch.setattr(mod.settings, "TMDB_LANGUAGE", "fr-FR")

        payload = _movie_payload([
            _rd("DE", _release("FSK 12")),
            _rd("GB", _release("12A")),
        ])
        result = _parse_movie_certification(payload)
        # We just need a non-None result from one of the two countries.
        assert result in ("FSK 12", "12A")

    def test_empty_release_dates_returns_none(self, monkeypatch):
        from app.services import tmdb_service as mod
        monkeypatch.setattr(mod.settings, "TMDB_LANGUAGE", "fr-FR")

        assert _parse_movie_certification({}) is None
        assert _parse_movie_certification({"release_dates": {}}) is None
        assert _parse_movie_certification({"release_dates": {"results": []}}) is None

    def test_all_certs_empty_returns_none(self, monkeypatch):
        from app.services import tmdb_service as mod
        monkeypatch.setattr(mod.settings, "TMDB_LANGUAGE", "en-US")

        payload = _movie_payload([
            _rd("US", _release("", rtype=3), _release("", rtype=1)),
            _rd("FR", _release("")),
        ])
        assert _parse_movie_certification(payload) is None

    def test_whitespace_cert_treated_as_empty(self, monkeypatch):
        from app.services import tmdb_service as mod
        monkeypatch.setattr(mod.settings, "TMDB_LANGUAGE", "en-US")

        payload = _movie_payload([
            _rd("US", {"certification": "   ", "type": 3}),
        ])
        assert _parse_movie_certification(payload) is None

    def test_cert_stripped_of_whitespace(self, monkeypatch):
        from app.services import tmdb_service as mod
        monkeypatch.setattr(mod.settings, "TMDB_LANGUAGE", "en-US")

        payload = _movie_payload([
            _rd("US", {"certification": "  PG  ", "type": 3}),
        ])
        assert _parse_movie_certification(payload) == "PG"


# ---------------------------------------------------------------------------
# TV certification
# ---------------------------------------------------------------------------

class TestParseTvCertification:
    def test_preferred_region_fr_returned(self, monkeypatch):
        from app.services import tmdb_service as mod
        monkeypatch.setattr(mod.settings, "TMDB_LANGUAGE", "fr-FR")

        payload = _tv_payload([
            _cr("FR", "16"),
            _cr("US", "TV-14"),
        ])
        assert _parse_tv_certification(payload) == "16"

    def test_us_fallback_when_preferred_absent(self, monkeypatch):
        from app.services import tmdb_service as mod
        monkeypatch.setattr(mod.settings, "TMDB_LANGUAGE", "fr-FR")

        payload = _tv_payload([
            _cr("US", "TV-MA"),
            _cr("DE", "18"),
        ])
        assert _parse_tv_certification(payload) == "TV-MA"

    def test_first_country_fallback(self, monkeypatch):
        from app.services import tmdb_service as mod
        monkeypatch.setattr(mod.settings, "TMDB_LANGUAGE", "fr-FR")

        payload = _tv_payload([
            _cr("DE", "18"),
            _cr("GB", "18"),
        ])
        result = _parse_tv_certification(payload)
        assert result in ("18",)

    def test_empty_content_ratings_returns_none(self, monkeypatch):
        from app.services import tmdb_service as mod
        monkeypatch.setattr(mod.settings, "TMDB_LANGUAGE", "fr-FR")

        assert _parse_tv_certification({}) is None
        assert _parse_tv_certification({"content_ratings": {}}) is None
        assert _parse_tv_certification({"content_ratings": {"results": []}}) is None

    def test_whitespace_rating_treated_as_empty(self, monkeypatch):
        from app.services import tmdb_service as mod
        monkeypatch.setattr(mod.settings, "TMDB_LANGUAGE", "en-US")

        payload = _tv_payload([
            _cr("US", "   "),
        ])
        assert _parse_tv_certification(payload) is None

    def test_rating_stripped_of_whitespace(self, monkeypatch):
        from app.services import tmdb_service as mod
        monkeypatch.setattr(mod.settings, "TMDB_LANGUAGE", "en-US")

        payload = _tv_payload([
            _cr("US", "  TV-PG  "),
        ])
        assert _parse_tv_certification(payload) == "TV-PG"

    def test_all_ratings_empty_returns_none(self, monkeypatch):
        from app.services import tmdb_service as mod
        monkeypatch.setattr(mod.settings, "TMDB_LANGUAGE", "fr-FR")

        payload = _tv_payload([
            _cr("FR", ""),
            _cr("US", ""),
        ])
        assert _parse_tv_certification(payload) is None

    def test_none_rating_treated_as_empty(self, monkeypatch):
        """iso entry with missing 'rating' key should not crash."""
        from app.services import tmdb_service as mod
        monkeypatch.setattr(mod.settings, "TMDB_LANGUAGE", "en-US")

        payload = _tv_payload([
            {"iso_3166_1": "US"},  # no 'rating' key
            _cr("DE", "16"),
        ])
        assert _parse_tv_certification(payload) == "16"


# ---------------------------------------------------------------------------
# Edge cases shared between both parsers
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_movie_none_input_returns_none(self, monkeypatch):
        from app.services import tmdb_service as mod
        monkeypatch.setattr(mod.settings, "TMDB_LANGUAGE", "en-US")

        # Top-level None values guarded by (x or {})
        assert _parse_movie_certification({"release_dates": None}) is None

    def test_tv_none_input_returns_none(self, monkeypatch):
        from app.services import tmdb_service as mod
        monkeypatch.setattr(mod.settings, "TMDB_LANGUAGE", "en-US")

        assert _parse_tv_certification({"content_ratings": None}) is None

    def test_movie_language_without_region_defaults_to_us(self, monkeypatch):
        """A bare language code like 'fr' has no region → falls back to US logic."""
        from app.services import tmdb_service as mod
        monkeypatch.setattr(mod.settings, "TMDB_LANGUAGE", "fr")  # no '-XX' part

        payload = _movie_payload([
            _rd("US", _release("R")),
        ])
        # _preferred_region() returns 'US' for bare 'fr' (no '-' separator)
        assert _parse_movie_certification(payload) == "R"
