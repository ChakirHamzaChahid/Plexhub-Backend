"""ADR 0005 D6 (Wave W4) — `manual_scrape_service.search_candidates`:
the TMDB widening chain, the OMDb `?s=` fallback, poster re-ranking and the
combined score / `recommended` rules.

Network is faked by monkeypatching the singletons the service module holds
(`mss.tmdb_service`, `mss.omdb_service`, `mss.poster_match_service`) — the
same convention `tests/test_manual_scrape_apply.py` uses, and the only way
to assert on CALL SHAPE (which language, which year, how many times) rather
than just on results.
"""
from __future__ import annotations

from app.config import settings
from app.services import manual_scrape_service as mss
from app.services.omdb_service import OMDbHit
from app.services.poster_match_service import PosterComparison
from app.services.tmdb_service import (
    CandidateSearch,
    MatchExtras,
    ScoredCandidate,
    TMDBMatch,
    TMDBSearchOutcome,
)


# ─── fakes ───────────────────────────────────────────────────────────────


def _scored(tmdb_id, title, year, *, confidence, title_score=0.95, votes=100):
    return ScoredCandidate(
        tmdb_id=tmdb_id, kind="movie", title=title, original_title=None,
        year=year, overview=f"overview of {title}", poster_path=f"/{tmdb_id}.jpg",
        vote_count=votes, title_score=title_score, confidence=confidence,
    )


def _outcome(result, tmdb_id=None):
    match = (
        TMDBMatch(tmdb_id=tmdb_id, title="x", year=None, confidence=0.9)
        if tmdb_id is not None else None
    )
    return TMDBSearchOutcome(result, match=match, best=match)


class FakeTMDBSearch:
    """Returns a scripted `CandidateSearch` per attempt, recording the exact
    (title, year, language) triple each attempt was made with."""

    is_configured = True

    def __init__(self, scripted, extras_by_id=None):
        self.scripted = list(scripted)
        self.extras_by_id = dict(extras_by_id or {})
        self.searches: list[tuple] = []
        self.extras_calls: list[int] = []

    async def search_candidates(self, kind, title, year, *, language=None, summary=None):
        self.searches.append((kind, title, year, language))
        if self.scripted:
            return self.scripted.pop(0)
        return CandidateSearch(verdict=TMDBSearchOutcome("nomatch"), candidates=[])

    async def get_match_extras(self, tmdb_id, kind):
        self.extras_calls.append(tmdb_id)
        return self.extras_by_id.get(tmdb_id)


class FakeOMDbSearch:
    is_configured = True

    def __init__(self, hits=None):
        self.hits = list(hits or [])
        self.calls: list[tuple] = []

    async def search_list(self, query, year, media_type):
        self.calls.append((query, year, media_type))
        return list(self.hits)


class FakePosterMatch:
    """Stands in for the whole `poster_match_service` module."""

    def __init__(self, by_url=None, default=None):
        self.by_url = dict(by_url or {})
        self.default = default
        self.calls: list[tuple] = []

    async def compare_posters(self, xtream_url, variants):
        self.calls.append((xtream_url, tuple(variants)))
        for variant in variants:
            if variant in self.by_url:
                return self.by_url[variant]
        return self.default or _comparison("different", 20)


def _comparison(badge, phash, *, generic=False, url=None):
    return PosterComparison(
        badge=badge, phash_distance=phash, dhash_distance=phash,
        image_score=max(0.0, 1 - phash / 32) if phash is not None else None,
        best_poster_url=url, variants_compared=1, xtream_generic=generic,
        shortcut=False,
    )


def _extras(tmdb_id, imdb_id, posters):
    return MatchExtras(
        tmdb_id=tmdb_id, kind="movie", imdb_id=imdb_id, title=f"T{tmdb_id}",
        original_title=None, year=1999, overview=None, poster_urls=list(posters),
    )


def _query(**kw):
    base = dict(media_type="movie", title="The Matrix", year=1999)
    base.update(kw)
    return mss.ScrapeQuery(**base)


# ─── the TMDB widening chain ─────────────────────────────────────────────


async def test_first_attempt_matched_stops_the_chain(monkeypatch):
    fake = FakeTMDBSearch([
        CandidateSearch(
            verdict=_outcome("matched", 603),
            candidates=[_scored(603, "The Matrix", 1999, confidence=0.98)],
        ),
    ])
    monkeypatch.setattr(mss, "tmdb_service", fake)
    monkeypatch.setattr(mss, "omdb_service", FakeOMDbSearch())

    result = await mss.search_candidates(
        _query(), xtream_poster_url=None, poster_top_n=0,
    )

    assert len(fake.searches) == 1
    assert result.text_verdict == "matched"
    assert [c.tmdb_id for c in result.candidates] == [603]
    assert result.candidates[0].text_safe is True
    assert result.candidates[0].recommended is True


async def test_chain_widens_without_year_then_en_us(monkeypatch):
    fake = FakeTMDBSearch([
        CandidateSearch(verdict=_outcome("nomatch"), candidates=[]),
        CandidateSearch(verdict=_outcome("ambiguous"), candidates=[
            _scored(1, "Matrix", 1999, confidence=0.70),
        ]),
        CandidateSearch(verdict=_outcome("nomatch"), candidates=[
            _scored(2, "Matrix II", 2003, confidence=0.60),
        ]),
    ])
    monkeypatch.setattr(mss, "tmdb_service", fake)
    monkeypatch.setattr(mss, "omdb_service", FakeOMDbSearch())

    result = await mss.search_candidates(
        _query(), xtream_poster_url=None, poster_top_n=0,
    )

    assert [(s[2], s[3]) for s in fake.searches] == [
        (1999, settings.TMDB_LANGUAGE),   # with the year, default language
        (None, settings.TMDB_LANGUAGE),   # without the year
        (1999, "en-US"),                  # year back, English
    ]
    # No attempt matched but one was ambiguous.
    assert result.text_verdict == "ambiguous"
    assert {c.tmdb_id for c in result.candidates} == {1, 2}
    assert all(c.text_safe is False for c in result.candidates)


async def test_candidates_are_deduped_keeping_best_confidence(monkeypatch):
    fake = FakeTMDBSearch([
        CandidateSearch(verdict=_outcome("nomatch"), candidates=[
            _scored(603, "The Matrix", 1999, confidence=0.55),
        ]),
        CandidateSearch(verdict=_outcome("nomatch"), candidates=[
            _scored(603, "The Matrix", 1999, confidence=0.91),
        ]),
        CandidateSearch(verdict=_outcome("nomatch"), candidates=[
            _scored(603, "The Matrix", 1999, confidence=0.70),
        ]),
    ])
    monkeypatch.setattr(mss, "tmdb_service", fake)
    monkeypatch.setattr(mss, "omdb_service", FakeOMDbSearch())

    result = await mss.search_candidates(
        _query(), xtream_poster_url=None, poster_top_n=0,
    )
    assert len(result.candidates) == 1
    assert result.candidates[0].text_confidence == 0.91


async def test_no_year_query_skips_the_without_year_attempt(monkeypatch):
    fake = FakeTMDBSearch([
        CandidateSearch(verdict=_outcome("nomatch"), candidates=[]),
        CandidateSearch(verdict=_outcome("nomatch"), candidates=[]),
    ])
    monkeypatch.setattr(mss, "tmdb_service", fake)
    monkeypatch.setattr(mss, "omdb_service", FakeOMDbSearch())

    await mss.search_candidates(
        _query(year=None), xtream_poster_url=None, poster_top_n=0,
    )
    assert [(s[2], s[3]) for s in fake.searches] == [
        (None, settings.TMDB_LANGUAGE), (None, "en-US"),
    ]


# ─── OMDb fallback / provider forcing ────────────────────────────────────


async def test_omdb_fallback_when_tmdb_returns_nothing(monkeypatch):
    fake_tmdb = FakeTMDBSearch([
        CandidateSearch(verdict=_outcome("nomatch"), candidates=[]),
        CandidateSearch(verdict=_outcome("nomatch"), candidates=[]),
        CandidateSearch(verdict=_outcome("nomatch"), candidates=[]),
    ])
    fake_omdb = FakeOMDbSearch([
        OMDbHit(imdb_id="tt0133093", title="The Matrix", year=1999,
                type="movie", poster_url="https://img/omdb.jpg"),
    ])
    monkeypatch.setattr(mss, "tmdb_service", fake_tmdb)
    monkeypatch.setattr(mss, "omdb_service", fake_omdb)

    result = await mss.search_candidates(
        _query(), xtream_poster_url=None, poster_top_n=0,
    )

    assert fake_omdb.calls == [("The Matrix", 1999, "movie")]
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.provider == "omdb"
    assert candidate.imdb_id == "tt0133093"
    assert candidate.tmdb_id is None
    assert candidate.title_score > 0.9  # exact title
    assert candidate.recommended is False  # OMDb hits are never `text_safe`


async def test_provider_tmdb_never_calls_omdb(monkeypatch):
    fake_tmdb = FakeTMDBSearch([
        CandidateSearch(verdict=_outcome("nomatch"), candidates=[]),
        CandidateSearch(verdict=_outcome("nomatch"), candidates=[]),
        CandidateSearch(verdict=_outcome("nomatch"), candidates=[]),
    ])
    fake_omdb = FakeOMDbSearch([
        OMDbHit(imdb_id="tt1", title="x", year=1999, type="movie", poster_url=None),
    ])
    monkeypatch.setattr(mss, "tmdb_service", fake_tmdb)
    monkeypatch.setattr(mss, "omdb_service", fake_omdb)

    result = await mss.search_candidates(
        _query(provider="tmdb"), xtream_poster_url=None, poster_top_n=0,
    )
    assert fake_omdb.calls == []
    assert result.candidates == []


async def test_provider_omdb_skips_tmdb_entirely(monkeypatch):
    fake_tmdb = FakeTMDBSearch([
        CandidateSearch(
            verdict=_outcome("matched", 603),
            candidates=[_scored(603, "The Matrix", 1999, confidence=0.99)],
        ),
    ])
    fake_omdb = FakeOMDbSearch([
        OMDbHit(imdb_id="tt0133093", title="The Matrix", year=1999,
                type="movie", poster_url=None),
    ])
    monkeypatch.setattr(mss, "tmdb_service", fake_tmdb)
    monkeypatch.setattr(mss, "omdb_service", fake_omdb)

    result = await mss.search_candidates(
        _query(provider="omdb"), xtream_poster_url=None, poster_top_n=0,
    )
    assert fake_tmdb.searches == []
    assert [c.provider for c in result.candidates] == ["omdb"]


async def test_tmdb_hits_suppress_the_omdb_fallback_in_auto(monkeypatch):
    fake_tmdb = FakeTMDBSearch([
        CandidateSearch(verdict=_outcome("nomatch"), candidates=[
            _scored(603, "The Matrix", 1999, confidence=0.5),
        ]),
        CandidateSearch(verdict=_outcome("nomatch"), candidates=[]),
        CandidateSearch(verdict=_outcome("nomatch"), candidates=[]),
    ])
    fake_omdb = FakeOMDbSearch([
        OMDbHit(imdb_id="tt1", title="x", year=1999, type="movie", poster_url=None),
    ])
    monkeypatch.setattr(mss, "tmdb_service", fake_tmdb)
    monkeypatch.setattr(mss, "omdb_service", fake_omdb)

    await mss.search_candidates(_query(), xtream_poster_url=None, poster_top_n=0)
    assert fake_omdb.calls == []


# ─── poster comparison & re-ranking ──────────────────────────────────────


async def test_poster_identical_reranks_above_a_better_text_score(monkeypatch):
    fake_tmdb = FakeTMDBSearch(
        [CandidateSearch(verdict=_outcome("nomatch"), candidates=[
            _scored(1, "Matrix A", 1999, confidence=0.90),
            _scored(2, "Matrix B", 1999, confidence=0.80),
        ])],
        extras_by_id={
            1: _extras(1, "tt1", ["https://image.tmdb.org/t/p/w185/1.jpg"]),
            2: _extras(2, "tt2", ["https://image.tmdb.org/t/p/w185/2.jpg"]),
        },
    )
    monkeypatch.setattr(mss, "tmdb_service", fake_tmdb)
    monkeypatch.setattr(mss, "omdb_service", FakeOMDbSearch())
    monkeypatch.setattr(mss, "poster_match_service", FakePosterMatch(by_url={
        "https://image.tmdb.org/t/p/w185/1.jpg": _comparison("different", 24),
        "https://image.tmdb.org/t/p/w185/2.jpg": _comparison("identical", 0),
    }))

    result = await mss.search_candidates(
        _query(), xtream_poster_url="http://xtream/poster.jpg",
    )

    # 2: 0.6*0.80 + 0.4*1.00 = 0.88 ; 1: 0.6*0.90 + 0.4*0.25 = 0.64
    assert [c.tmdb_id for c in result.candidates] == [2, 1]
    assert result.candidates[0].poster.badge == "identical"
    assert result.candidates[0].recommended is True
    # `get_match_extras` also resolves each compared candidate's imdb id.
    assert result.candidates[0].imdb_id == "tt2"


async def test_uncompared_candidate_keeps_its_text_score(monkeypatch):
    fake_tmdb = FakeTMDBSearch(
        [CandidateSearch(verdict=_outcome("nomatch"), candidates=[
            _scored(1, "A", 1999, confidence=0.90),
            _scored(2, "B", 1999, confidence=0.80),
        ])],
        extras_by_id={1: _extras(1, "tt1", ["https://image.tmdb.org/t/p/w185/1.jpg"])},
    )
    monkeypatch.setattr(mss, "tmdb_service", fake_tmdb)
    monkeypatch.setattr(mss, "omdb_service", FakeOMDbSearch())
    monkeypatch.setattr(mss, "poster_match_service", FakePosterMatch(
        default=_comparison("identical", 0),
    ))

    result = await mss.search_candidates(
        _query(), xtream_poster_url="http://xtream/poster.jpg", poster_top_n=1,
    )

    assert fake_tmdb.extras_calls == [1]  # only the top-N by TEXT score
    by_id = {c.tmdb_id: c for c in result.candidates}
    assert by_id[2].poster is None
    assert by_id[2].combined_score == by_id[2].text_confidence


async def test_no_xtream_poster_skips_comparison_but_still_resolves_imdb(monkeypatch):
    fake_tmdb = FakeTMDBSearch(
        [CandidateSearch(verdict=_outcome("nomatch"), candidates=[
            _scored(1, "A", 1999, confidence=0.90),
        ])],
        extras_by_id={1: _extras(1, "tt1", ["https://image.tmdb.org/t/p/w185/1.jpg"])},
    )
    poster = FakePosterMatch(default=_comparison("identical", 0))
    monkeypatch.setattr(mss, "tmdb_service", fake_tmdb)
    monkeypatch.setattr(mss, "omdb_service", FakeOMDbSearch())
    monkeypatch.setattr(mss, "poster_match_service", poster)

    result = await mss.search_candidates(_query(), xtream_poster_url=None)

    assert poster.calls == []
    assert result.xtream_poster_available is False
    assert result.candidates[0].imdb_id == "tt1"
    assert result.candidates[0].poster is None


async def test_unknown_badge_does_not_dilute_the_text_score(monkeypatch):
    fake_tmdb = FakeTMDBSearch(
        [CandidateSearch(verdict=_outcome("nomatch"), candidates=[
            _scored(1, "A", 1999, confidence=0.90),
        ])],
        extras_by_id={1: _extras(1, "tt1", ["https://image.tmdb.org/t/p/w185/1.jpg"])},
    )
    monkeypatch.setattr(mss, "tmdb_service", fake_tmdb)
    monkeypatch.setattr(mss, "omdb_service", FakeOMDbSearch())
    monkeypatch.setattr(mss, "poster_match_service", FakePosterMatch(
        default=PosterComparison(
            badge="unknown", phash_distance=None, dhash_distance=None,
            image_score=None, best_poster_url=None, variants_compared=0,
            xtream_generic=False, shortcut=False,
        ),
    ))

    result = await mss.search_candidates(
        _query(), xtream_poster_url="http://xtream/poster.jpg",
    )
    assert result.candidates[0].combined_score == 0.90


async def test_generic_xtream_poster_is_surfaced(monkeypatch):
    fake_tmdb = FakeTMDBSearch(
        [CandidateSearch(verdict=_outcome("nomatch"), candidates=[
            _scored(1, "A", 1999, confidence=0.90),
        ])],
        extras_by_id={1: _extras(1, "tt1", ["https://image.tmdb.org/t/p/w185/1.jpg"])},
    )
    monkeypatch.setattr(mss, "tmdb_service", fake_tmdb)
    monkeypatch.setattr(mss, "omdb_service", FakeOMDbSearch())
    monkeypatch.setattr(mss, "poster_match_service", FakePosterMatch(
        default=_comparison("close", 4, generic=True),
    ))

    result = await mss.search_candidates(
        _query(), xtream_poster_url="http://xtream/placeholder.jpg",
    )
    assert result.xtream_poster_generic is True
    assert result.candidates[0].recommended is False  # 'close' is not 'identical'


async def test_omdb_candidate_compares_its_single_poster(monkeypatch):
    fake_tmdb = FakeTMDBSearch([
        CandidateSearch(verdict=_outcome("nomatch"), candidates=[]),
        CandidateSearch(verdict=_outcome("nomatch"), candidates=[]),
        CandidateSearch(verdict=_outcome("nomatch"), candidates=[]),
    ])
    poster = FakePosterMatch(default=_comparison("identical", 0))
    monkeypatch.setattr(mss, "tmdb_service", fake_tmdb)
    monkeypatch.setattr(mss, "omdb_service", FakeOMDbSearch([
        OMDbHit(imdb_id="tt1", title="The Matrix", year=1999,
                type="movie", poster_url="https://img/omdb.jpg"),
    ]))
    monkeypatch.setattr(mss, "poster_match_service", poster)

    result = await mss.search_candidates(
        _query(), xtream_poster_url="http://xtream/poster.jpg",
    )
    assert poster.calls == [("http://xtream/poster.jpg", ("https://img/omdb.jpg",))]
    assert result.candidates[0].poster.badge == "identical"
    assert result.candidates[0].recommended is True


# ─── failure containment ─────────────────────────────────────────────────


async def test_tmdb_exception_degrades_to_nomatch_without_leaking_the_url(
    monkeypatch, caplog,
):
    import logging

    import httpx

    class Boom:
        is_configured = True

        async def search_candidates(self, *a, **kw):
            request = httpx.Request(
                "GET", "https://api.themoviedb.org/3/search/movie?api_key=SECRET",
            )
            raise httpx.HTTPStatusError(
                "boom", request=request, response=httpx.Response(500, request=request),
            )

        async def get_match_extras(self, *a, **kw):
            return None

    monkeypatch.setattr(mss, "tmdb_service", Boom())
    monkeypatch.setattr(mss, "omdb_service", FakeOMDbSearch())

    with caplog.at_level(logging.DEBUG, logger="plexhub.manual_scrape"):
        result = await mss.search_candidates(_query(), xtream_poster_url=None)

    assert result.candidates == []
    assert result.text_verdict == "nomatch"
    assert "SECRET" not in " ".join(
        r.getMessage() for r in caplog.records if r.name.startswith("plexhub")
    )


async def test_poster_failure_leaves_the_candidate_on_its_text_score(monkeypatch):
    class BoomPoster:
        async def compare_posters(self, *a, **kw):
            raise RuntimeError("poster backend down")

    fake_tmdb = FakeTMDBSearch(
        [CandidateSearch(verdict=_outcome("nomatch"), candidates=[
            _scored(1, "A", 1999, confidence=0.90),
        ])],
        extras_by_id={1: _extras(1, "tt1", ["https://image.tmdb.org/t/p/w185/1.jpg"])},
    )
    monkeypatch.setattr(mss, "tmdb_service", fake_tmdb)
    monkeypatch.setattr(mss, "omdb_service", FakeOMDbSearch())
    monkeypatch.setattr(mss, "poster_match_service", BoomPoster())

    result = await mss.search_candidates(
        _query(), xtream_poster_url="http://xtream/poster.jpg",
    )
    assert result.candidates[0].poster is None
    assert result.candidates[0].combined_score == 0.90
