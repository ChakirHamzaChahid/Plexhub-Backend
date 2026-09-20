"""ADR 0005 W0 — `_best_match` must stay byte-identical to
`_verdict(_score_results(...))`, and `title_similarity`/`year_score` must
agree with the private `_title_sim`/`_year_score` they wrap.
"""
from __future__ import annotations

import pytest
import pytest_asyncio

from app.services.tmdb_service import TMDBService, title_similarity, year_score


# NOTE: no module-level `pytestmark = pytest.mark.asyncio` (see
# tests/test_rating_blend.py) — every test in this file is a plain sync
# function consuming an already-resolved `svc` fixture; only the fixture
# itself is async.


@pytest_asyncio.fixture
async def svc(monkeypatch):
    from app.services import tmdb_service as mod

    monkeypatch.setattr(mod.settings, "TMDB_API_KEY", "test_key")
    monkeypatch.setattr(mod.settings, "TMDB_LANGUAGE", "en-US")
    s = TMDBService()
    try:
        yield s
    finally:
        await s.close()


def _movie(id_, title, date="", overview="", vote=0, original=None):
    r = {"id": id_, "title": title, "release_date": date, "vote_count": vote, "overview": overview}
    if original is not None:
        r["original_title"] = original
    return r


_KWARGS = dict(title_key="title", orig_key="original_title", date_key="release_date")


def _assert_split_matches_best_match(svc, results, title, year, summary):
    expected = svc._best_match(results, title, year, summary, **_KWARGS)
    scored = svc._score_results(results, title, year, **_KWARGS)
    actual = svc._verdict(scored, summary)
    assert actual == expected
    return actual


class TestScoreVerdictSplit:
    def test_empty_results_nomatch(self, svc):
        outcome = _assert_split_matches_best_match(svc, [], "Whatever", 2000, None)
        assert outcome.result == "nomatch"

    def test_clear_winner_matched(self, svc):
        results = [
            _movie(1, "The Matrix", "1999-03-31", vote=1000),
            _movie(2, "The Matrix Reloaded", "2003-05-15", vote=800),
        ]
        outcome = _assert_split_matches_best_match(svc, results, "The Matrix", 1999, None)
        assert outcome.result == "matched"
        assert outcome.match.tmdb_id == 1

    def test_below_threshold_nomatch(self, svc):
        results = [_movie(1, "Completely Unrelated Title", "2010-01-01", vote=5)]
        outcome = _assert_split_matches_best_match(svc, results, "The Matrix", 1999, None)
        assert outcome.result == "nomatch"
        assert outcome.match is None
        assert outcome.best is not None

    def test_ambiguous_without_summary(self, svc):
        # Two near-identical titles/years -> confidence gap < MIN_MARGIN, no
        # summary to break the tie -> ambiguous.
        results = [
            _movie(1, "Alpha City", "2015-01-01", overview="A story about aliens.", vote=100),
            _movie(2, "Alpha City", "2015-06-01", overview="A story about robots.", vote=99),
        ]
        outcome = _assert_split_matches_best_match(svc, results, "Alpha City", 2015, None)
        assert outcome.result == "ambiguous"
        assert outcome.match is None

    def test_ambiguous_resolved_by_summary_tiebreak(self, svc):
        results = [
            _movie(1, "Alpha City", "2015-01-01", overview="A gritty story about aliens invading earth.", vote=100),
            _movie(2, "Alpha City", "2015-06-01", overview="A story about robots taking over a factory.", vote=99),
        ]
        summary = "A gritty story about aliens invading earth."
        outcome = _assert_split_matches_best_match(svc, results, "Alpha City", 2015, summary)
        assert outcome.result == "matched"
        assert outcome.match.tmdb_id == 1

    def test_score_results_sorted_confidence_then_vote_count(self, svc):
        results = [
            _movie(1, "Zzz", "2000-01-01", vote=1),
            _movie(2, "The Matrix", "1999-03-31", vote=50),
            _movie(3, "The Matrix", "1999-03-31", vote=999),
        ]
        scored = svc._score_results(results, "The Matrix", 1999, **_KWARGS)
        # Both id=2 and id=3 score identically on title/year -> vote_count
        # breaks the tie, higher vote_count first.
        assert scored[0][0].tmdb_id == 3
        assert scored[1][0].tmdb_id == 2

    def test_score_results_caps_at_ten(self, svc):
        results = [_movie(i, "The Matrix", "1999-03-31", vote=i) for i in range(15)]
        scored = svc._score_results(results, "The Matrix", 1999, **_KWARGS)
        assert len(scored) == 10


class TestPureHelpers:
    def test_title_similarity_matches_title_sim(self, svc):
        from app.utils.string_normalizer import normalize_for_sorting

        query = "The Matrix"
        candidate = "The Matrix Reloaded"
        assert title_similarity(query, candidate) == svc._title_sim(
            normalize_for_sorting(query), candidate,
        )

    def test_title_similarity_identical(self):
        assert title_similarity("Terminator", "Terminator") == 1.0

    @pytest.mark.parametrize(
        "q, c, expected",
        [
            (1999, 1999, 1.0),
            (1999, 2000, 0.8),
            (1999, 2005, 0.0),
            (None, 1999, 0.5),
            (1999, None, 0.5),
        ],
    )
    def test_year_score_matches_year_score(self, q, c, expected):
        assert year_score(q, c) == expected == TMDBService._year_score(q, c)
