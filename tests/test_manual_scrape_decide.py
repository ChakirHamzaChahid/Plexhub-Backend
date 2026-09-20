"""ADR 0005 D6 — `manual_scrape_service.decide()`, the batch auto-apply rule
engine.

`decide()` is PURE (no I/O, no DB, no clock), which is the whole point: the
rule table that decides whether thousands of identities get written AND
locked automatically must be exhaustively testable without a single network
call. Every `Decision.reason` the ADR enumerates gets a case here.
"""
from __future__ import annotations

import pytest

from app.services import manual_scrape_service as mss
from app.services.poster_match_service import PosterComparison


def _poster(badge: str, phash: int | None = 2) -> PosterComparison:
    return PosterComparison(
        badge=badge,
        phash_distance=phash,
        dhash_distance=phash,
        image_score=None if badge == "unknown" else 0.9,
        best_poster_url="http://img/p.jpg",
        variants_compared=1,
        xtream_generic=False,
        shortcut=False,
    )


def _candidate(
    *, tmdb_id=1, title="The Matrix", year=1999, title_score=1.0,
    text_confidence=0.95, poster=None, text_safe=False,
) -> mss.Candidate:
    return mss.Candidate(
        provider="tmdb", tmdb_id=tmdb_id, imdb_id=f"tt{tmdb_id:07d}",
        media_type="movie", title=title, original_title=None, year=year,
        overview=None, poster_url="http://img/p.jpg", title_score=title_score,
        text_confidence=text_confidence, poster=poster,
        combined_score=text_confidence, text_safe=text_safe, recommended=False,
    )


def _result(
    candidates, *, query_year=1999, poster_available=True, poster_generic=False,
) -> mss.SearchResult:
    return mss.SearchResult(
        query=mss.ScrapeQuery(media_type="movie", title="The Matrix", year=query_year),
        candidates=candidates,
        text_verdict="matched" if any(c.text_safe for c in candidates) else "ambiguous",
        xtream_poster_available=poster_available,
        xtream_poster_generic=poster_generic,
        tmdb_calls=1,
        omdb_calls=0,
    )


# ─── the reason table ──────────────────────────────────────────────────────


def test_no_candidates_goes_to_review():
    d = mss.decide(_result([]), poster_only_auto=True)
    assert (d.action, d.rule, d.reason) == ("review", None, "no_candidates")


def test_no_xtream_poster_goes_to_review():
    d = mss.decide(
        _result([_candidate()], poster_available=False), poster_only_auto=True,
    )
    assert (d.action, d.reason) == ("review", "no_xtream_poster")


def test_no_xtream_poster_but_text_safe_reports_the_text_safe_reason():
    """A text-safe candidate with nothing to corroborate it is a DIFFERENT
    operator decision from "no poster at all" — the reasons must not merge."""
    d = mss.decide(
        _result([_candidate(text_safe=True)], poster_available=False),
        poster_only_auto=True,
    )
    assert (d.action, d.reason) == ("review", "text_safe_no_poster_match")


def test_rule_a_applies_on_text_safe_plus_identical_poster():
    candidate = _candidate(text_safe=True, poster=_poster("identical"))
    d = mss.decide(_result([candidate]), poster_only_auto=False)
    assert (d.action, d.rule, d.reason) == ("apply", "A", "text_safe_and_identical")
    assert d.candidate is candidate


def test_rule_a_needs_both_signals_on_the_same_candidate():
    """Text-safe on candidate #1, identical poster on candidate #2 is NOT
    rule A — that is exactly the two-films-confused case the rule guards."""
    d = mss.decide(
        _result([
            _candidate(tmdb_id=1, text_safe=True, poster=_poster("different", 25)),
            _candidate(tmdb_id=2, poster=_poster("identical")),
        ]),
        poster_only_auto=False,
    )
    assert d.action == "review"
    assert d.reason == "text_safe_no_poster_match"


def test_rule_a_beats_a_generic_xtream_poster():
    """Ordering contract: A is evaluated BEFORE the generic-poster bail-out,
    because A doesn't rest on the image alone."""
    d = mss.decide(
        _result(
            [_candidate(text_safe=True, poster=_poster("identical"))],
            poster_generic=True,
        ),
        poster_only_auto=False,
    )
    assert (d.action, d.rule) == ("apply", "A")


def test_generic_xtream_poster_blocks_image_only_evidence():
    d = mss.decide(
        _result([_candidate(poster=_poster("identical"))], poster_generic=True),
        poster_only_auto=True,
    )
    assert (d.action, d.reason) == ("review", "xtream_poster_generic")


def test_rule_b_applies_behind_the_flag():
    candidate = _candidate(poster=_poster("identical"), text_confidence=0.7)
    d = mss.decide(_result([candidate]), poster_only_auto=True)
    assert (d.action, d.rule, d.reason) == ("apply", "B", "poster_only_identical")
    assert d.candidate is candidate


def test_rule_b_is_off_by_default():
    d = mss.decide(
        _result([_candidate(poster=_poster("identical"))]), poster_only_auto=False,
    )
    assert (d.action, d.reason) == ("review", "no_identical_poster")


def test_rule_b_refuses_two_identical_posters():
    d = mss.decide(
        _result([
            _candidate(tmdb_id=1, poster=_poster("identical")),
            _candidate(tmdb_id=2, poster=_poster("identical")),
        ]),
        poster_only_auto=True,
    )
    assert (d.action, d.reason) == ("review", "ambiguous_posters")


def test_rule_b_refuses_a_weak_title():
    d = mss.decide(
        _result([_candidate(poster=_poster("identical"), title_score=0.4)]),
        poster_only_auto=True,
    )
    assert (d.action, d.reason) == ("review", "no_identical_poster")


@pytest.mark.parametrize(
    "query_year,candidate_year,expected",
    [
        (1999, 1999, "apply"),
        (1999, 2000, "apply"),   # one year of provider drift is tolerated
        (1999, 2005, "review"),
        (None, 1999, "apply"),   # unknown year is not evidence against
        (1999, None, "apply"),
    ],
)
def test_rule_b_year_gate(query_year, candidate_year, expected):
    d = mss.decide(
        _result(
            [_candidate(poster=_poster("identical"), year=candidate_year)],
            query_year=query_year,
        ),
        poster_only_auto=True,
    )
    assert d.action == expected


def test_close_poster_is_never_enough():
    d = mss.decide(
        _result([_candidate(poster=_poster("close", 10))]), poster_only_auto=True,
    )
    assert (d.action, d.reason) == ("review", "no_identical_poster")


def test_unknown_poster_comparison_is_not_identical():
    d = mss.decide(
        _result([_candidate(poster=_poster("unknown", None), text_safe=True)]),
        poster_only_auto=True,
    )
    assert (d.action, d.reason) == ("review", "text_safe_no_poster_match")
