"""ADR 0005 D10 (Wave W4) — the manual-scraper admin UI: catalogue browse
(type toggle + `ids` filter + aliases), the poster proxy, the scrape panel,
candidate cards and the id lookup.

Everything here is HTTP-level against the real Jinja templates: a fragment
that stops rendering the Apply form, the confirm text or the OOB panel-close
is a real regression the service-level tests cannot see.
"""
from __future__ import annotations

import itertools

import pytest

from app.config import settings
from app.db import database as db_module
from app.models.database import Media
from app.services import manual_scrape_service as mss
from app.services.poster_match_service import FetchedImage, PosterFetchError
from tests.test_manual_scrape_search import (  # noqa: F401 — reused fakes
    FakeOMDbSearch,
    FakePosterMatch,
    FakeTMDBSearch,
    _comparison,
    _extras,
    _outcome,
    _scored,
)
from app.services.tmdb_service import CandidateSearch

pytestmark = pytest.mark.asyncio

ADMIN_USER = "admin"
ADMIN_PASS = "test-admin-pass-scrape-ui"
ADMIN_AUTH = (ADMIN_USER, ADMIN_PASS)


@pytest.fixture(autouse=True)
def _admin_creds(monkeypatch, db_factory):
    from app.api import admin as admin_module

    monkeypatch.setattr(settings, "ADMIN_USERNAME", ADMIN_USER)
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", ADMIN_PASS)
    monkeypatch.setattr(db_module, "async_session_factory", db_factory)
    # `admin.py` binds `async_session_factory` at import time — the
    # manual-scrape routes use their OWN reference, so patch it too (same
    # reason as tests/test_admin_manual_scrape.py).
    monkeypatch.setattr(admin_module, "async_session_factory", db_factory)


_page_offset = itertools.count()


def _media(rating_key, title, **kw):
    # `media` has a UNIQUE on (server_id, library_section_id, filter,
    # sort_order, page_offset) on top of its 4-column PK — hand every seeded
    # row a distinct page_offset so a test can seed several items.
    base = dict(
        rating_key=rating_key, server_id="xtream_a", library_section_id="1",
        title=title, type="movie", year=1999, page_offset=next(_page_offset),
    )
    base.update(kw)
    return Media(**base)


# ─── catalogue browse ────────────────────────────────────────────────────


async def test_index_lists_movies_and_stats(api_client, db_factory):
    async with db_factory() as s:
        s.add(_media("vod_1.mp4", "Inception", year=2010))
        await s.commit()

    resp = await api_client.get("/admin", auth=ADMIN_AUTH)
    assert resp.status_code == 200
    assert "Inception" in resp.text
    assert "Films" in resp.text and "Séries" in resp.text
    assert "Sans IMDb" in resp.text and "Sans TMDB" in resp.text
    # ADR 0005 F9: the htmx trigger must not carry a `from:` selector with a
    # space in it, nor `changed` on the form.
    assert 'hx-trigger="change, keyup delay:300ms, submit"' in resp.text
    assert "from:#filters" not in resp.text


async def test_type_toggle_selects_shows(api_client, db_factory):
    async with db_factory() as s:
        s.add(_media("vod_1.mp4", "A Movie"))
        s.add(_media("series_1", "A Show", type="show"))
        await s.commit()

    movies = await api_client.get("/admin/catalogue?type=movie", auth=ADMIN_AUTH)
    shows = await api_client.get("/admin/catalogue?type=show", auth=ADMIN_AUTH)

    assert "A Movie" in movies.text and "A Show" not in movies.text
    assert "A Show" in shows.text and "A Movie" not in shows.text


async def test_ids_filter_variants(api_client, db_factory):
    async with db_factory() as s:
        s.add(_media("both", "Both Ids", imdb_id="tt1", tmdb_id="1"))
        s.add(_media("imdb_only", "Imdb Only", imdb_id="tt2"))
        s.add(_media("neither", "No Ids"))
        await s.commit()

    async def titles(ids: str) -> str:
        resp = await api_client.get(f"/admin/catalogue?ids={ids}", auth=ADMIN_AUTH)
        assert resp.status_code == 200
        return resp.text

    all_text = await titles("all")
    assert "Both Ids" in all_text and "No Ids" in all_text

    incomplete = await titles("incomplete")
    assert "Both Ids" not in incomplete
    assert "Imdb Only" in incomplete and "No Ids" in incomplete

    missing_both = await titles("missing_both")
    assert "No Ids" in missing_both and "Imdb Only" not in missing_both

    missing_tmdb = await titles("missing_tmdb")
    assert "Imdb Only" in missing_tmdb and "Both Ids" not in missing_tmdb

    # `review` is a W5 filter (migration 027): it matches nothing rather
    # than silently behaving like `all`.
    assert "Aucun résultat" in await titles("review")


async def test_catalogue_dedupes_category_variants(api_client, db_factory):
    """ADR 0005 F1: the same item in two categories is two `media` rows but
    must be listed once."""
    async with db_factory() as s:
        s.add(_media("vod_1.mp4", "Twinned", filter="all", sort_order="default"))
        s.add(_media("vod_1.mp4", "Twinned", filter="action", sort_order="title"))
        await s.commit()

    resp = await api_client.get("/admin/catalogue?ids=all", auth=ADMIN_AUTH)
    assert resp.text.count(">Twinned<") == 1
    assert "1 résultat(s)" in resp.text


async def test_locked_and_batch_filters(api_client, db_factory):
    async with db_factory() as s:
        s.add(_media("locked", "Locked One", match_locked=True, match_source="manual"))
        s.add(_media("batched", "Batched One", match_source="batch_auto"))
        s.add(_media("plain", "Plain One"))
        await s.commit()

    locked = await api_client.get("/admin/catalogue?ids=locked", auth=ADMIN_AUTH)
    assert "Locked One" in locked.text and "Plain One" not in locked.text

    batch = await api_client.get("/admin/catalogue?ids=batch", auth=ADMIN_AUTH)
    assert "Batched One" in batch.text and "Plain One" not in batch.text


async def test_search_filters_by_title(api_client, db_factory):
    async with db_factory() as s:
        s.add(_media("a", "Matrix Reloaded"))
        s.add(_media("b", "Amélie"))
        await s.commit()

    resp = await api_client.get("/admin/catalogue?ids=all&search=matrix", auth=ADMIN_AUTH)
    assert "Matrix Reloaded" in resp.text and "Amélie" not in resp.text


# ─── back-compat aliases ─────────────────────────────────────────────────


async def test_movies_alias_maps_missing_imdb(api_client, db_factory):
    async with db_factory() as s:
        s.add(_media("a", "Has Imdb", imdb_id="tt1"))
        s.add(_media("b", "No Imdb"))
        await s.commit()

    resp = await api_client.get("/admin/movies?missing_imdb=true", auth=ADMIN_AUTH)
    assert resp.status_code == 200
    assert "No Imdb" in resp.text and "Has Imdb" not in resp.text


async def test_movies_alias_without_flags_lists_everything(api_client, db_factory):
    async with db_factory() as s:
        s.add(_media("a", "Has Imdb", imdb_id="tt1", tmdb_id="1"))
        await s.commit()

    resp = await api_client.get("/admin/movies", auth=ADMIN_AUTH)
    assert "Has Imdb" in resp.text


async def test_stats_aliases_both_return_the_fragment(api_client, db_factory):
    async with db_factory() as s:
        s.add(_media("a", "One", imdb_id="tt1", tmdb_id="1"))
        await s.commit()

    for path in ("/admin/stats", "/admin/movies/stats"):
        resp = await api_client.get(path, auth=ADMIN_AUTH)
        assert resp.status_code == 200, path
        assert "Sans IMDb" in resp.text
        assert "Séries" in resp.text


# ─── poster proxy ────────────────────────────────────────────────────────


class _FakePoster:
    def __init__(self, image=None, raises=False):
        self.image = image
        self.raises = raises
        self.urls: list[str] = []
        self.PosterFetchError = PosterFetchError

    async def fetch_image(self, url):
        self.urls.append(url)
        if self.raises:
            raise PosterFetchError("unsafe host (SSRF guard)")
        return self.image


async def test_poster_proxy_serves_the_db_url(api_client, db_factory, monkeypatch):
    from app.api import admin as admin_module

    async with db_factory() as s:
        s.add(_media("vod_1.mp4", "With Poster", thumb_url="http://xtream/p.jpg"))
        await s.commit()

    fake = _FakePoster(image=FetchedImage(content=b"\xff\xd8binary", content_type="image/jpeg"))
    monkeypatch.setattr(admin_module, "poster_match_service", fake)

    resp = await api_client.get(
        "/admin/media/xtream_a/vod_1.mp4/poster?which=xtream", auth=ADMIN_AUTH,
    )
    assert resp.status_code == 200
    assert resp.content == b"\xff\xd8binary"
    assert resp.headers["content-type"].startswith("image/jpeg")
    assert resp.headers["cache-control"] == "private, max-age=86400"
    assert fake.urls == ["http://xtream/p.jpg"]


async def test_poster_proxy_which_current_uses_resolved_url(
    api_client, db_factory, monkeypatch,
):
    from app.api import admin as admin_module

    async with db_factory() as s:
        s.add(_media(
            "vod_1.mp4", "With Poster",
            thumb_url="http://xtream/p.jpg",
            resolved_thumb_url="https://image.tmdb.org/t/p/w500/x.jpg",
        ))
        await s.commit()

    fake = _FakePoster(image=FetchedImage(content=b"x", content_type="image/png"))
    monkeypatch.setattr(admin_module, "poster_match_service", fake)

    resp = await api_client.get(
        "/admin/media/xtream_a/vod_1.mp4/poster?which=current", auth=ADMIN_AUTH,
    )
    assert resp.status_code == 200
    assert fake.urls == ["https://image.tmdb.org/t/p/w500/x.jpg"]


async def test_poster_proxy_ignores_a_client_supplied_url(
    api_client, db_factory, monkeypatch,
):
    """The proxy key is the media PK — a client-supplied URL must never be
    fetched (that would be an open SSRF/credential-probing proxy)."""
    from app.api import admin as admin_module

    async with db_factory() as s:
        s.add(_media("vod_1.mp4", "With Poster", thumb_url="http://xtream/p.jpg"))
        await s.commit()

    fake = _FakePoster(image=FetchedImage(content=b"x", content_type="image/jpeg"))
    monkeypatch.setattr(admin_module, "poster_match_service", fake)

    resp = await api_client.get(
        "/admin/media/xtream_a/vod_1.mp4/poster"
        "?which=xtream&url=http://169.254.169.254/latest/meta-data/",
        auth=ADMIN_AUTH,
    )
    assert resp.status_code == 200
    assert fake.urls == ["http://xtream/p.jpg"]


async def test_poster_proxy_unknown_row_is_404(api_client, db_factory):
    resp = await api_client.get(
        "/admin/media/xtream_a/nope/poster", auth=ADMIN_AUTH,
    )
    assert resp.status_code == 404


async def test_poster_proxy_without_url_is_404(api_client, db_factory):
    async with db_factory() as s:
        s.add(_media("vod_1.mp4", "No Poster"))
        await s.commit()

    resp = await api_client.get(
        "/admin/media/xtream_a/vod_1.mp4/poster", auth=ADMIN_AUTH,
    )
    assert resp.status_code == 404


async def test_poster_proxy_ssrf_block_is_404_without_leaking_the_url(
    api_client, db_factory, monkeypatch,
):
    from app.api import admin as admin_module

    async with db_factory() as s:
        s.add(_media("vod_1.mp4", "Evil", thumb_url="http://127.0.0.1:8080/p.jpg"))
        await s.commit()

    monkeypatch.setattr(admin_module, "poster_match_service", _FakePoster(raises=True))

    resp = await api_client.get(
        "/admin/media/xtream_a/vod_1.mp4/poster", auth=ADMIN_AUTH,
    )
    assert resp.status_code == 404
    assert "127.0.0.1" not in resp.text


# ─── scrape panel / candidates / lookup ──────────────────────────────────


async def test_scrape_panel_renders_prefilled_form(api_client, db_factory):
    async with db_factory() as s:
        s.add(_media("vod_1.mp4", "The Matrix", thumb_url="http://xtream/p.jpg"))
        await s.commit()

    resp = await api_client.get(
        "/admin/media/xtream_a/vod_1.mp4/scrape", auth=ADMIN_AUTH,
    )
    assert resp.status_code == 200
    assert 'value="The Matrix"' in resp.text
    assert 'value="1999"' in resp.text
    assert "Poster Xtream" in resp.text and "Poster actuel" in resp.text
    assert "/admin/media/xtream_a/vod_1.mp4/candidates" in resp.text
    assert "/admin/media/xtream_a/vod_1.mp4/lookup" in resp.text
    assert "OMDb" in resp.text


async def test_scrape_panel_unknown_row_is_404(api_client, db_factory):
    resp = await api_client.get(
        "/admin/media/xtream_a/nope/scrape", auth=ADMIN_AUTH,
    )
    assert resp.status_code == 404


async def test_candidates_fragment_renders_cards(api_client, db_factory, monkeypatch):
    async with db_factory() as s:
        s.add(_media("vod_1.mp4", "The Matrix", thumb_url="http://xtream/p.jpg"))
        await s.commit()

    monkeypatch.setattr(mss, "tmdb_service", FakeTMDBSearch(
        [CandidateSearch(verdict=_outcome("matched", 603), candidates=[
            _scored(603, "The Matrix", 1999, confidence=0.98),
        ])],
        extras_by_id={603: _extras(603, "tt0133093", [
            "https://image.tmdb.org/t/p/w185/603.jpg",
        ])},
    ))
    monkeypatch.setattr(mss, "omdb_service", FakeOMDbSearch())
    monkeypatch.setattr(mss, "poster_match_service", FakePosterMatch(
        default=_comparison("identical", 0),
    ))

    resp = await api_client.get(
        "/admin/media/xtream_a/vod_1.mp4/candidates?title=The+Matrix&year=1999&type=movie",
        auth=ADMIN_AUTH,
    )
    assert resp.status_code == 200
    assert "The Matrix" in resp.text
    assert "Recommandé" in resp.text
    assert "poster identique" in resp.text
    assert "texte sûr" in resp.text
    assert "themoviedb.org/movie/603" in resp.text
    assert "imdb.com/title/tt0133093" in resp.text
    # The Apply form posts the candidate ids and warns about the overwrite.
    assert 'hx-post="/admin/media/xtream_a/vod_1.mp4/apply"' in resp.text
    assert 'name="propagate"' in resp.text
    assert "hx-confirm" in resp.text
    assert "aucun" in resp.text  # old ids shown in the confirm text


async def test_candidates_fragment_empty_result(api_client, db_factory, monkeypatch):
    async with db_factory() as s:
        s.add(_media("vod_1.mp4", "Unknown Film"))
        await s.commit()

    monkeypatch.setattr(mss, "tmdb_service", FakeTMDBSearch([]))
    monkeypatch.setattr(mss, "omdb_service", FakeOMDbSearch())

    resp = await api_client.get(
        "/admin/media/xtream_a/vod_1.mp4/candidates", auth=ADMIN_AUTH,
    )
    assert resp.status_code == 200
    assert "Aucun candidat" in resp.text


async def test_lookup_renders_single_card(api_client, db_factory, monkeypatch):
    async with db_factory() as s:
        s.add(_media("vod_1.mp4", "The Matrix"))
        await s.commit()

    monkeypatch.setattr(mss, "tmdb_service", FakeTMDBSearch(
        [], extras_by_id={603: _extras(603, "tt0133093", [])},
    ))
    monkeypatch.setattr(mss, "omdb_service", FakeOMDbSearch())

    resp = await api_client.post(
        "/admin/media/xtream_a/vod_1.mp4/lookup",
        data={"raw_id": "603", "type": "movie"},
        auth=ADMIN_AUTH,
    )
    assert resp.status_code == 200
    assert "1 candidat(s)" in resp.text
    assert "themoviedb.org/movie/603" in resp.text


async def test_lookup_unparsable_id_is_422(api_client, db_factory):
    async with db_factory() as s:
        s.add(_media("vod_1.mp4", "The Matrix"))
        await s.commit()

    resp = await api_client.post(
        "/admin/media/xtream_a/vod_1.mp4/lookup",
        data={"raw_id": "not an id", "type": "movie"},
        auth=ADMIN_AUTH,
    )
    assert resp.status_code == 422
    assert "illisible" in resp.text


async def test_lookup_unknown_row_is_404(api_client, db_factory):
    resp = await api_client.post(
        "/admin/media/xtream_a/nope/lookup",
        data={"raw_id": "603", "type": "movie"},
        auth=ADMIN_AUTH,
    )
    assert resp.status_code == 404


async def test_scrape_routes_require_auth(api_client):
    for path in (
        "/admin/media/xtream_a/vod_1.mp4/scrape",
        "/admin/media/xtream_a/vod_1.mp4/candidates",
        "/admin/media/xtream_a/vod_1.mp4/poster",
    ):
        resp = await api_client.get(path)
        assert resp.status_code == 401, path


async def test_row_hx_targets_escape_the_dot_in_rating_key(
    api_client, db_factory,
):
    """A movie/episode rating_key carries its file extension
    (`vod_439568.mkv`). `#row-xtream_a-vod_439568.mkv` is parsed by
    `querySelector` as "id row-…-vod_439568 WITH CLASS mkv" — no match, so
    htmx fires `htmx:targetError` and the swap is silently dropped.
    Verified in a real browser before the fix: Sauver / Appliquer /
    Re-scrape / Déverrouiller updated nothing at all on every movie row.
    The id attribute stays raw; only the SELECTOR is escaped."""
    async with db_factory() as s:
        s.add(_media("vod_439568.mkv", "Scarlet", thumb_url="http://xtream/p.jpg"))
        await s.commit()

    resp = await api_client.get("/admin?type=movie&ids=all", auth=ADMIN_AUTH)
    assert resp.status_code == 200
    assert 'id="row-xtream_a-vod_439568.mkv"' in resp.text, "the id itself stays raw"
    assert r'hx-target="#row-xtream_a-vod_439568\.mkv"' in resp.text
    assert 'hx-target="#row-xtream_a-vod_439568.mkv"' not in resp.text, (
        "an unescaped dot makes the selector match nothing"
    )
