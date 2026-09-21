"""Duration is a property of the CONTENT, not of the source serving it.

Real prod defect (2026-09-21): MAO S01E01 is served by three sources
carrying 1 519 000 ms, 125 000 ms and 2 000 ms. `best_row` elects the
representative on enrichment/poster/rating/title — never on duration — so
the API exposed **2 000 ms** for a 25-minute episode. Measured impact:
1 490 multi-source slots with a short AND a long version, plus 15 254 slots
where no source has a believable duration at all (120 whole series).
"""
from __future__ import annotations

import pytest

from app.models.database import Media
from app.services.aggregation_service import (
    IMPLAUSIBLE_DURATION_MS,
    consolidated_duration,
)
from app.services.media_identity_writer import build_identity_values
from app.services.tmdb_service import TMDBEnrichmentData, _episode_runtime_ms


def _row(duration):
    return Media(rating_key="rk", server_id="sid", title="t", duration=duration)


class TestConsolidatedDuration:
    def test_mao_s01e01_real_case(self):
        rows = [_row(1_519_000), _row(125_000), _row(2_000)]
        assert consolidated_duration(rows) == 1_519_000

    def test_order_does_not_matter(self):
        assert consolidated_duration([_row(2_000), _row(1_519_000)]) == 1_519_000

    @pytest.mark.parametrize("values", [[], [None], [0], [None, 0]])
    def test_no_usable_value_yields_none(self, values):
        assert consolidated_duration([_row(v) for v in values]) is None

    def test_single_believable_source_is_kept(self):
        assert consolidated_duration([_row(1_400_000)]) == 1_400_000


class TestTmdbFloor:
    """The floor fills a HOLE. It never rewrites a reported duration.

    First design did override short durations, on the assumption that panels
    publish garbage. An `ffprobe` on the real stream disproved it: MAO S01E01
    from xtream_8fb2c0f3 genuinely is 125.1 s (49.9 MB, 1920x1080, 2 993
    frames). The panel is honest; the FILE is truncated. Advertising TMDB's
    24 minutes there would promise a runtime nothing can play.
    """

    def test_fills_in_when_no_source_reports_anything(self):
        assert consolidated_duration([_row(None), _row(0)], floor_ms=1_320_000) == 1_320_000

    def test_never_overrides_a_reported_short_duration(self):
        # The 2 min 05 really is what the file contains.
        assert consolidated_duration([_row(125_000)], floor_ms=1_440_000) == 125_000

    def test_a_complete_sibling_still_wins(self):
        rows = [_row(1_519_000), _row(125_000), _row(2_000)]
        assert consolidated_duration(rows, floor_ms=1_440_000) == 1_519_000

    def test_no_floor_and_no_source_yields_none(self):
        assert consolidated_duration([_row(None)], floor_ms=None) is None


class TestEpisodeRuntimeParsing:
    @pytest.mark.parametrize("raw, expected", [
        ([22], 1_320_000),
        # Mixed formats: the SHORTEST plausible value, because this is a floor
        # used to reject garbage — overstating is worse than understating.
        ([22, 44], 1_320_000),
        ([0, 24], 1_440_000),          # TMDB sometimes carries a 0
        ([], None),
        ([0], None),                   # a zero floor would reject nothing
        (None, None),
        ("24", None),                  # field is a LIST; a string is not one
    ])
    def test_parses(self, raw, expected):
        assert _episode_runtime_ms({"episode_run_time": raw}) == expected

    def test_absent_field(self):
        assert _episode_runtime_ms({}) is None


class TestIdentityWriterWiring:
    def _data(self, runtime_ms):
        return TMDBEnrichmentData(
            tmdb_id=1, imdb_id="tt1", overview=None, poster_url=None,
            backdrop_url=None, vote_average=None, genres=None, year=None,
            cast=None, episode_runtime_ms=runtime_ms,
        )

    def test_show_runtime_is_written(self):
        v = build_identity_values(
            self._data(1_320_000), confidence=1.0, omdb=None, mode="fill",
        )
        assert "duration" in v

    def test_movies_never_touch_duration(self):
        # `episode_run_time` is TV-only, so a movie carries None and the
        # column must be left entirely alone.
        v = build_identity_values(
            self._data(None), confidence=1.0, omdb=None, mode="fill",
        )
        assert "duration" not in v

    def test_threshold_is_five_minutes(self):
        # Both the API consolidation and the DB write must agree on what
        # "implausible" means, or a value could be rejected by one and
        # accepted by the other.
        assert IMPLAUSIBLE_DURATION_MS == 300_000
