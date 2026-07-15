"""Tests for app.services.plex_catalog_service (feature "Télécharger Plex",
Tâche C4 — read-only, deduplicated Plex catalogue).

Fixtures insert `PlexServer`/`PlexMediaItem` rows directly (no sync worker
involved — C4 only reads what C2/C3 would have written).
"""
from __future__ import annotations

import pytest

from app.models.database import PlexMediaItem, PlexServer
from app.services import plex_catalog_service as svc

pytestmark = pytest.mark.asyncio


# --- Fixture helpers ----------------------------------------------------


def make_server(
    client_identifier: str,
    *,
    name: str,
    owner_title: str | None = None,
    owned: bool = True,
    is_reachable: bool = True,
    access_token: str = "secret-token",
    base_uri: str = "https://10.0.0.1:32400",
    last_synced_at: int | None = 1_700_000_000_000,
    last_sync_error: str | None = None,
) -> PlexServer:
    return PlexServer(
        client_identifier=client_identifier,
        name=name,
        owner_title=owner_title,
        owned=owned,
        access_token=access_token,
        base_uri=base_uri,
        is_reachable=is_reachable,
        last_synced_at=last_synced_at,
        last_sync_error=last_sync_error,
        created_at=1_700_000_000_000,
        updated_at=1_700_000_000_000,
    )


def make_item(
    server_id: str,
    rating_key: str,
    *,
    type: str,
    title: str,
    year: int | None = None,
    unification_id: str | None = None,
    parent_rating_key: str | None = None,
    grandparent_rating_key: str | None = None,
    parent_index: int | None = None,
    index: int | None = None,
    added_at: int = 1_700_000_000_000,
    height: int | None = None,
    part_size: int | None = None,
    video_codec: str | None = None,
    audio_codec: str | None = None,
    container: str | None = None,
    synced_at: int = 1_700_000_000_000,
) -> PlexMediaItem:
    return PlexMediaItem(
        server_id=server_id,
        rating_key=rating_key,
        type=type,
        title=title,
        year=year,
        unification_id=unification_id,
        parent_rating_key=parent_rating_key,
        grandparent_rating_key=grandparent_rating_key,
        parent_index=parent_index,
        index=index,
        added_at=added_at,
        height=height,
        part_size=part_size,
        video_codec=video_codec,
        audio_codec=audio_codec,
        container=container,
        synced_at=synced_at,
    )


# --- list_unified ---------------------------------------------------------


async def test_list_unified_dedups_two_servers_into_one_group(db_session):
    db_session.add_all([
        make_server("srv-a", name="Server A"),
        make_server("srv-b", name="Server B"),
        make_item(
            "plex_srv-a", "100", type="movie", title="Interstellar", year=2014,
            unification_id="tt0816692", height=1080,
        ),
        make_item(
            "plex_srv-b", "200", type="movie", title="Interstellar", year=2014,
            unification_id="tt0816692", height=2160,
        ),
    ])
    await db_session.commit()

    items, total = await svc.list_unified(db_session, "movie", None, 50, 0)

    assert total == 1
    assert len(items) == 1
    group = items[0]
    assert group.unification_id == "tt0816692"
    assert group.type == "movie"
    assert group.title == "Interstellar"
    assert group.year == 2014
    assert group.source_count == 2
    assert group.sources == []  # list_unified never hydrates per-source detail


async def test_list_unified_search_by_title(db_session):
    db_session.add_all([
        make_server("srv-a", name="Server A"),
        make_item("plex_srv-a", "1", type="movie", title="The Matrix", year=1999,
                   unification_id="tt0133093"),
        make_item("plex_srv-a", "2", type="movie", title="Inception", year=2010,
                   unification_id="tt1375666"),
    ])
    await db_session.commit()

    items, total = await svc.list_unified(db_session, "movie", "matrix", 50, 0)

    assert total == 1
    assert items[0].title == "The Matrix"


async def test_list_unified_pagination_and_total(db_session):
    db_session.add(make_server("srv-a", name="Server A"))
    for i in range(5):
        db_session.add(make_item(
            "plex_srv-a", str(i), type="movie", title=f"Movie {i}", year=2000 + i,
            unification_id=f"tt{i:07d}", added_at=1_700_000_000_000 + i,
        ))
    await db_session.commit()

    page1, total1 = await svc.list_unified(db_session, "movie", None, 2, 0)
    page2, total2 = await svc.list_unified(db_session, "movie", None, 2, 2)
    page3, total3 = await svc.list_unified(db_session, "movie", None, 2, 4)

    assert total1 == total2 == total3 == 5
    assert len(page1) == 2
    assert len(page2) == 2
    assert len(page3) == 1
    seen = {g.unification_id for g in (page1 + page2 + page3)}
    assert seen == {f"tt{i:07d}" for i in range(5)}


async def test_list_unified_sorted_by_recency_desc(db_session):
    db_session.add(make_server("srv-a", name="Server A"))
    db_session.add(make_item("plex_srv-a", "1", type="movie", title="Old", year=2000,
                              unification_id="tt_old", added_at=1_000))
    db_session.add(make_item("plex_srv-a", "2", type="movie", title="New", year=2020,
                              unification_id="tt_new", added_at=9_000))
    await db_session.commit()

    items, _total = await svc.list_unified(db_session, "movie", None, 50, 0)

    assert [g.unification_id for g in items] == ["tt_new", "tt_old"]


async def test_list_unified_excludes_null_and_empty_unification_id(db_session):
    db_session.add(make_server("srv-a", name="Server A"))
    db_session.add(make_item("plex_srv-a", "1", type="movie", title="No unif", year=2000,
                              unification_id=None))
    db_session.add(make_item("plex_srv-a", "2", type="movie", title="Empty unif", year=2001,
                              unification_id=""))
    db_session.add(make_item("plex_srv-a", "3", type="movie", title="Has unif", year=2002,
                              unification_id="tt123"))
    await db_session.commit()

    items, total = await svc.list_unified(db_session, "movie", None, 50, 0)

    assert total == 1
    assert items[0].unification_id == "tt123"


async def test_list_unified_filters_by_media_type(db_session):
    db_session.add(make_server("srv-a", name="Server A"))
    db_session.add(make_item("plex_srv-a", "1", type="movie", title="A Movie", year=2000,
                              unification_id="tt_movie"))
    db_session.add(make_item("plex_srv-a", "2", type="show", title="A Show",
                              unification_id="tt_show"))
    await db_session.commit()

    movies, movies_total = await svc.list_unified(db_session, "movie", None, 50, 0)
    shows, shows_total = await svc.list_unified(db_session, "show", None, 50, 0)

    assert movies_total == 1 and movies[0].unification_id == "tt_movie"
    assert shows_total == 1 and shows[0].unification_id == "tt_show"


# --- get_group ------------------------------------------------------------


async def test_get_group_movie_sources_sorted_by_resolution_then_size(db_session):
    db_session.add_all([
        make_server("srv-a", name="Server A", owner_title=None),
        make_server("srv-b", name="Server B", owner_title="Friend"),
        make_item(
            "plex_srv-a", "100", type="movie", title="Dune", year=2021,
            unification_id="tt1160419", height=720, part_size=1_500_000_000,
            video_codec="h264", audio_codec="aac", container="mp4",
        ),
        make_item(
            "plex_srv-b", "200", type="movie", title="Dune", year=2021,
            unification_id="tt1160419", height=1080, part_size=4_000_000_000,
            video_codec="hevc", audio_codec="ac3", container="mkv",
        ),
    ])
    await db_session.commit()

    group = await svc.get_group(db_session, "movie", "tt1160419")

    assert group is not None
    assert group.unification_id == "tt1160419"
    assert group.title == "Dune"
    assert group.year == 2021
    assert group.source_count == 2
    assert [s.resolution for s in group.sources] == ["1080p", "720p"]
    assert group.sources[0].server_id == "plex_srv-b"
    assert group.sources[0].size_bytes == 4_000_000_000
    assert group.sources[0].video_codec == "hevc"
    assert group.sources[0].container == "mkv"
    assert group.sources[0].server_name == "Server B"
    assert group.sources[0].owner_title == "Friend"
    assert group.sources[1].server_id == "plex_srv-a"


async def test_get_group_ties_broken_by_size(db_session):
    db_session.add(make_server("srv-a", name="Server A"))
    db_session.add(make_item(
        "plex_srv-a", "1", type="movie", title="Same Res", year=2020,
        unification_id="tt_tie", height=1080, part_size=1_000,
    ))
    db_session.add(make_item(
        "plex_srv-a", "2", type="movie", title="Same Res", year=2020,
        unification_id="tt_tie", height=1080, part_size=9_000,
    ))
    await db_session.commit()

    group = await svc.get_group(db_session, "movie", "tt_tie")

    assert group is not None
    assert [s.size_bytes for s in group.sources] == [9_000, 1_000]


async def test_get_group_unknown_unification_id_returns_none(db_session):
    result = await svc.get_group(db_session, "movie", "does-not-exist")
    assert result is None


# --- list_seasons_with_sources ---------------------------------------------


async def test_list_seasons_with_sources_two_servers(db_session):
    db_session.add_all([
        make_server("srv-a", name="Server A"),
        make_server("srv-b", name="Server B"),
        make_item("plex_srv-a", "show-a", type="show", title="Breaking Bad",
                   unification_id="tt0903747"),
        make_item("plex_srv-b", "show-b", type="show", title="Breaking Bad",
                   unification_id="tt0903747"),
    ])
    # server A: season 1 has 2 episodes, season 2 has 1 episode
    db_session.add_all([
        make_item("plex_srv-a", "e1", type="episode", title="Pilot",
                   grandparent_rating_key="show-a", parent_index=1, index=1,
                   height=1080, part_size=1_000_000_000),
        make_item("plex_srv-a", "e2", type="episode", title="Cat's in the Bag",
                   grandparent_rating_key="show-a", parent_index=1, index=2,
                   height=1080, part_size=1_100_000_000),
        make_item("plex_srv-a", "e3", type="episode", title="...And the Bag's in the River",
                   grandparent_rating_key="show-a", parent_index=2, index=1,
                   height=720, part_size=900_000_000),
    ])
    # server B: only season 1, 1 episode
    db_session.add_all([
        make_item("plex_srv-b", "f1", type="episode", title="Pilot",
                   grandparent_rating_key="show-b", parent_index=1, index=1,
                   height=2160, part_size=3_000_000_000),
    ])
    await db_session.commit()

    seasons = await svc.list_seasons_with_sources(db_session, "tt0903747")

    assert [s.season for s in seasons] == [1, 2]

    season1 = seasons[0]
    assert season1.season == 1
    by_server = {s["server_id"]: s for s in season1.sources}
    assert by_server["plex_srv-a"]["episode_count"] == 2
    assert by_server["plex_srv-a"]["resolution"] == "1080p"
    assert by_server["plex_srv-a"]["size_bytes"] == 2_100_000_000
    assert by_server["plex_srv-a"]["show_rating_key"] == "show-a"
    assert by_server["plex_srv-b"]["episode_count"] == 1
    assert by_server["plex_srv-b"]["resolution"] == "2160p"
    assert by_server["plex_srv-b"]["show_rating_key"] == "show-b"

    season2 = seasons[1]
    assert season2.season == 2
    assert len(season2.sources) == 1
    assert season2.sources[0]["server_id"] == "plex_srv-a"
    assert season2.sources[0]["episode_count"] == 1
    assert season2.sources[0]["resolution"] == "720p"


async def test_list_seasons_with_sources_unknown_unification_id(db_session):
    seasons = await svc.list_seasons_with_sources(db_session, "does-not-exist")
    assert seasons == []


# --- list_episodes_with_sources ---------------------------------------------


async def test_list_episodes_with_sources_cross_server_rating_keys_differ(db_session):
    db_session.add_all([
        make_server("srv-a", name="Server A"),
        make_server("srv-b", name="Server B"),
        make_item("plex_srv-a", "show-a", type="show", title="The Wire",
                   unification_id="tt0306414"),
        make_item("plex_srv-b", "show-b", type="show", title="The Wire",
                   unification_id="tt0306414"),
        make_item("plex_srv-a", "ep-a-s01e01", type="episode", title="The Target",
                   grandparent_rating_key="show-a", parent_index=1, index=1,
                   height=1080, part_size=800_000_000),
        make_item("plex_srv-b", "ep-b-s01e01", type="episode", title="The Target (Pilot)",
                   grandparent_rating_key="show-b", parent_index=1, index=1,
                   height=720, part_size=500_000_000),
        # Different season — must not leak into season=1 results.
        make_item("plex_srv-a", "ep-a-s02e01", type="episode", title="Ebb Tide",
                   grandparent_rating_key="show-a", parent_index=2, index=1,
                   height=1080, part_size=800_000_000),
    ])
    await db_session.commit()

    episodes = await svc.list_episodes_with_sources(db_session, "tt0306414", 1)

    assert len(episodes) == 1
    ep1 = episodes[0]
    assert ep1.season == 1
    assert ep1.episode == 1
    # the longer/more descriptive title wins
    assert ep1.title == "The Target (Pilot)"

    rating_keys = {s["server_id"]: s["episode_rating_key"] for s in ep1.sources}
    assert rating_keys["plex_srv-a"] == "ep-a-s01e01"
    assert rating_keys["plex_srv-b"] == "ep-b-s01e01"
    assert rating_keys["plex_srv-a"] != rating_keys["plex_srv-b"]

    by_server = {s["server_id"]: s for s in ep1.sources}
    assert by_server["plex_srv-a"]["resolution"] == "1080p"
    assert by_server["plex_srv-a"]["height"] == 1080
    assert by_server["plex_srv-b"]["resolution"] == "720p"


async def test_list_episodes_with_sources_unknown_season_is_empty(db_session):
    db_session.add(make_item("plex_srv-a", "show-a", type="show", title="Some Show",
                              unification_id="tt_show"))
    await db_session.commit()

    episodes = await svc.list_episodes_with_sources(db_session, "tt_show", 99)

    assert episodes == []


# --- list_servers ------------------------------------------------------------


async def test_list_servers_never_exposes_secrets(db_session):
    db_session.add(make_server(
        "srv-a", name="Home Server", owner_title=None, owned=True,
        is_reachable=True, access_token="super-secret-token",
        base_uri="https://192.168.1.5:32400",
    ))
    db_session.add(make_server(
        "srv-b", name="Friend's Server", owner_title="Alice", owned=False,
        is_reachable=False, access_token="another-secret",
        base_uri="https://203.0.113.9:32400", last_sync_error="timeout",
    ))
    await db_session.commit()

    servers = await svc.list_servers(db_session)

    assert len(servers) == 2
    for entry in servers:
        assert "access_token" not in entry
        assert "accessToken" not in entry
        assert "base_uri" not in entry
        assert "baseUri" not in entry

    by_id = {s["server_id"]: s for s in servers}
    assert by_id["plex_srv-a"]["client_identifier"] == "srv-a"
    assert by_id["plex_srv-a"]["name"] == "Home Server"
    assert by_id["plex_srv-a"]["owned"] is True
    assert by_id["plex_srv-a"]["is_reachable"] is True
    assert by_id["plex_srv-b"]["owner_title"] == "Alice"
    assert by_id["plex_srv-b"]["owned"] is False
    assert by_id["plex_srv-b"]["is_reachable"] is False
    assert by_id["plex_srv-b"]["last_sync_error"] == "timeout"
