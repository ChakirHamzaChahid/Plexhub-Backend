"""`unified_download_catalog_service` — the cross-source merge of the Xtream
(`media`) and Plex (`plex_media_item`) download catalogues (feature "écran de
téléchargement unifié", Vague W2).

Proves the linchpin: a title present in BOTH sources under the same id-based
`unification_id` (`imdb://`/`tmdb://`) collapses into ONE card carrying both
origins; a title with no shared id stays two cards (no false merge); genre +
search filter both; and the `truncated` flag fires when a source exceeds the
cap.
"""
from __future__ import annotations

from app.models.database import Media, PlexMediaItem, PlexServer
from app.services import unified_download_catalog_service as svc
from app.utils.server_id import build_plex_server_id, build_server_id
from app.utils.time import now_ms

XTREAM_A = build_server_id("accA")
PLEX_CID = "cid-1"
PLEX_SID = build_plex_server_id(PLEX_CID)


def _media(
    rating_key: str, *, server_id: str = XTREAM_A, type: str = "movie",
    title: str, unification_id: str, year: int | None = 2014,
    genres: str | None = None, page_offset: int = 0,
) -> Media:
    # page_offset is part of the (server_id, library_section_id, filter,
    # sort_order, page_offset) UNIQUE key — distinct per row on the same account.
    return Media(
        rating_key=rating_key, server_id=server_id, filter="all", sort_order="default",
        library_section_id="xtream", title=title, type=type, year=year,
        unification_id=unification_id, genres=genres, page_offset=page_offset,
        is_in_allowed_categories=True, is_broken=False,
    )


def _plex(
    rating_key: str, *, type: str = "movie", title: str, unification_id: str,
    year: int | None = 2014, genres: str | None = None,
) -> PlexMediaItem:
    return PlexMediaItem(
        server_id=PLEX_SID, rating_key=rating_key, type=type, title=title, year=year,
        unification_id=unification_id, genres=genres, added_at=now_ms(), synced_at=now_ms(),
    )


def _plex_server() -> PlexServer:
    return PlexServer(
        client_identifier=PLEX_CID, name="PMS", owned=True,
        access_token="tok", base_uri="https://x.plex.direct:32400",
        is_reachable=True, created_at=now_ms(), updated_at=now_ms(),
    )


# ─── cross-source dedup ─────────────────────────────────────────────────────


async def test_same_movie_in_both_sources_appears_once_with_both_origins(db_session):
    db_session.add_all([
        _plex_server(),
        _media("x1", title="Interstellar", unification_id="imdb://tt0816692"),
        _plex("p1", title="Interstellar", unification_id="imdb://tt0816692"),
    ])
    await db_session.commit()

    cards, total, truncated = await svc.list_unified(db_session, media_type="movie")

    assert total == 1
    assert len(cards) == 1
    card = cards[0]
    assert card.unification_id == "imdb://tt0816692"
    assert card.origins == ["plex", "xtream"]
    assert card.source_count == 2  # one Xtream version + one Plex source
    assert truncated is False


async def test_xtream_only_and_plex_only_stay_separate(db_session):
    db_session.add_all([
        _plex_server(),
        _media("x1", title="Only Xtream", unification_id="imdb://tt111"),
        _plex("p1", title="Only Plex", unification_id="imdb://tt222"),
    ])
    await db_session.commit()

    cards, total, _ = await svc.list_unified(db_session, media_type="movie")

    by_id = {c.unification_id: c for c in cards}
    assert total == 2
    assert by_id["imdb://tt111"].origins == ["xtream"]
    assert by_id["imdb://tt222"].origins == ["plex"]


async def test_no_shared_id_never_false_merges(db_session):
    # Same title/year, but only fallback keys (title_ vs plexsrc://) -> 2 cards.
    db_session.add_all([
        _plex_server(),
        _media("x1", title="Nameless", unification_id="title_nameless_2014"),
        _plex("p1", title="Nameless", unification_id="plexsrc://plex_cid-1/p1"),
    ])
    await db_session.commit()

    cards, total, _ = await svc.list_unified(db_session, media_type="movie")

    assert total == 2  # never merged on title+year alone


async def test_series_dedup_across_sources(db_session):
    db_session.add_all([
        _plex_server(),
        _media("x1", type="show", title="Firefly", unification_id="imdb://tt0303461"),
        _plex("p1", type="show", title="Firefly", unification_id="imdb://tt0303461"),
    ])
    await db_session.commit()

    cards, total, _ = await svc.list_unified(db_session, media_type="show")

    assert total == 1
    assert cards[0].origins == ["plex", "xtream"]
    assert cards[0].type == "show"


# ─── genre filter (both origins) ────────────────────────────────────────────


async def test_genre_filters_both_origins(db_session):
    db_session.add_all([
        _plex_server(),
        _media("x1", title="Action X", unification_id="imdb://tt1", genres="Action, Thriller",
               page_offset=0),
        _media("x2", title="Comedy X", unification_id="imdb://tt2", genres="Comedy",
               page_offset=1),
        _plex("p1", title="Action P", unification_id="imdb://tt3", genres="Action"),
        _plex("p2", title="Drama P", unification_id="imdb://tt4", genres="Drama"),
    ])
    await db_session.commit()

    cards, total, _ = await svc.list_unified(db_session, media_type="movie", genre="Action")

    ids = {c.unification_id for c in cards}
    assert total == 2
    assert ids == {"imdb://tt1", "imdb://tt3"}


# ─── truncation flag (bounded merge) ────────────────────────────────────────


async def test_truncated_flag_when_source_exceeds_cap(db_session):
    db_session.add(_plex_server())
    for i in range(3):
        db_session.add(_media(f"x{i}", title=f"M{i}", unification_id=f"imdb://ttx{i}",
                              page_offset=i))
    await db_session.commit()

    _cards, _total, truncated = await svc.list_unified(db_session, media_type="movie", cap=1)
    assert truncated is True


# ─── get_group_availability ─────────────────────────────────────────────────


async def test_availability_reports_both_origins(db_session):
    db_session.add_all([
        _plex_server(),
        _media("x1", title="Interstellar", unification_id="imdb://tt0816692"),
        _plex("p1", title="Interstellar", unification_id="imdb://tt0816692"),
    ])
    await db_session.commit()

    avail = await svc.get_group_availability(db_session, "movie", "imdb://tt0816692")

    assert avail is not None
    assert avail.has_xtream and avail.has_plex
    assert avail.xtream_source_count == 1
    assert avail.plex_source_count == 1
    assert avail.origins == ["plex", "xtream"]


async def test_availability_none_for_unknown_group(db_session):
    assert await svc.get_group_availability(db_session, "movie", "imdb://nope") is None
