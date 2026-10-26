"""B2-05 — routes de lecture de `/api/metadata/v1` : lookup, items, item, episodes, fields
(contrat § 5.2–5.4, § 5.13, § 6.1 ; ADR 0011 § D2.7)."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import update

from app.models.database import Media
from tests._metadata_support import BASE, SERVER, h, seed

pytestmark = pytest.mark.usefixtures("wire")


# ─── Auth (matrice des clés) ────────────────────────────────────────────────

READ_ROUTES = [
    f"{BASE}/lookup?type=movie&tmdbId=603",
    f"{BASE}/items?type=movie",
    f"{BASE}/items/{SERVER}/vod_1.mkv",
    f"{BASE}/items/{SERVER}/series_1/episodes",
    f"{BASE}/items/{SERVER}/vod_1.mkv/history",
    f"{BASE}/edits/NOPE",
    f"{BASE}/fields",
]
ALLOWED = {"master": True, "read": True, "rw": True, "scrape_read": True}


@pytest.mark.parametrize("route", READ_ROUTES)
@pytest.mark.parametrize(
    "who", ["master", "read", "rw", "scrape_read", "write", "scrape", "none", "revoked", "expired", "missing"]
)
async def test_read_routes_auth_matrix(client, keys, route, who):
    key = None if who == "missing" else keys[who]
    resp = await client.get(route, headers=h(key))
    if ALLOWED.get(who):
        assert resp.status_code not in (401, 403), (route, who, resp.text)
    elif who in ("revoked", "expired", "missing"):
        assert resp.status_code == 401 and resp.json()["detail"]["code"] == "invalid_api_key"
    else:
        assert resp.status_code == 403 and resp.json()["detail"]["code"] == "insufficient_scope"


async def test_every_response_carries_a_request_id_header(client, keys):
    resp = await client.get(f"{BASE}/fields", headers=h(keys["read"], **{"X-Request-ID": "task-42"}))
    assert resp.headers["X-Request-ID"] == "task-42"
    assert (await client.get(f"{BASE}/fields", headers=h(keys["read"]))).headers["X-Request-ID"]


# ─── lookup ─────────────────────────────────────────────────────────────────


async def test_lookup_requires_exactly_one_identifier(client, keys):
    for params in (
        {"type": "movie"},
        {"type": "movie", "imdbId": "tt0133093", "tmdbId": "603"},
        {"type": "movie", "imdbId": "x", "unificationId": "u"},
    ):
        resp = await client.get(f"{BASE}/lookup", params=params, headers=h(keys["read"]))
        assert resp.status_code == 400, params
        assert resp.json()["detail"]["code"] == "invalid_parameter"


@pytest.mark.parametrize(
    "params",
    [{"imdbId": "0133093"}, {"imdbId": "tt12"}, {"tmdbId": "abc"}, {"tmdbId": "1234567890"}, {"tvdbId": "x"}],
)
async def test_lookup_rejects_malformed_ids(client, keys, params):
    resp = await client.get(f"{BASE}/lookup", params={"type": "movie", **params}, headers=h(keys["read"]))
    assert resp.status_code == 400 and resp.json()["detail"]["code"] == "invalid_parameter"


async def test_lookup_type_is_required_and_restricted(client, keys):
    assert (await client.get(f"{BASE}/lookup", params={"tmdbId": "1"}, headers=h(keys["read"]))).status_code == 422
    resp = await client.get(f"{BASE}/lookup", params={"type": "episode", "tmdbId": "1"}, headers=h(keys["read"]))
    assert resp.status_code == 422


async def test_lookup_returns_every_match_sorted_hidden_and_adult_included(client, keys, wire):
    await seed(wire, rating_key="vod_9.mkv", server_id="xtream_b", tmdb_id="603")
    await seed(wire, rating_key="vod_2.mkv", server_id="xtream_a", tmdb_id="603", is_adult=True)
    await seed(wire, rating_key="vod_1.mkv", server_id="xtream_a", tmdb_id="603",
               is_in_allowed_categories=False)
    await seed(wire, rating_key="vod_3.mkv", server_id="xtream_a", tmdb_id="604")          # other id
    await seed(wire, rating_key="series_1", server_id="xtream_a", type="show", tmdb_id="603")  # other type
    resp = await client.get(f"{BASE}/lookup", params={"type": "movie", "tmdbId": "603"}, headers=h(keys["read"]))
    body = resp.json()
    assert resp.status_code == 200 and body["schemaVersion"] == 1 and body["truncated"] is False
    assert [(i["serverId"], i["ratingKey"]) for i in body["items"]] == [
        ("xtream_a", "vod_1.mkv"), ("xtream_a", "vod_2.mkv"), ("xtream_b", "vod_9.mkv"),
    ]
    by_key = {i["ratingKey"]: i for i in body["items"]}
    assert by_key["vod_2.mkv"]["isAdult"] is True
    first = body["items"][0]
    assert set(first) == {
        "serverId", "ratingKey", "type", "title", "year", "ids", "unificationId", "revision",
        "lock", "isAdult", "isHidden", "season", "episode", "showRatingKey", "thumbUrl", "artUrl",
    }
    assert first["isHidden"] is True
    assert by_key["vod_2.mkv"]["isHidden"] is False
    assert first["ids"] == {"imdb": None, "tmdb": "603", "tvdb": None, "wikidata": None}
    assert first["lock"] == {"locked": False, "source": None}


async def test_lookup_no_match_is_200_with_an_empty_list(client, keys):
    resp = await client.get(f"{BASE}/lookup", params={"type": "show", "imdbId": "tt0133093"}, headers=h(keys["read"]))
    assert resp.status_code == 200 and resp.json()["items"] == []


async def test_lookup_by_unification_id_and_tvdb(client, keys, wire):
    await seed(wire, tvdb_id="81189", unification_id="imdb://tt0133093")
    for params in ({"unificationId": "imdb://tt0133093"}, {"tvdbId": "81189"}):
        resp = await client.get(f"{BASE}/lookup", params={"type": "movie", **params}, headers=h(keys["read"]))
        assert [i["ratingKey"] for i in resp.json()["items"]] == ["vod_1.mkv"]


async def test_lookup_is_bounded_to_50_with_truncated(client, keys, wire):
    for n in range(55):
        await seed(wire, rating_key=f"vod_{n:03d}.mkv", tmdb_id="7")
    resp = await client.get(f"{BASE}/lookup", params={"type": "movie", "tmdbId": "7"}, headers=h(keys["read"]))
    body = resp.json()
    assert len(body["items"]) == 50 and body["truncated"] is True


# ─── items ──────────────────────────────────────────────────────────────────


async def test_items_lists_those_without_ids_with_stable_pagination(client, keys, wire):
    for n in range(5):
        await seed(wire, rating_key=f"vod_{n}.mkv")                        # no id at all
    await seed(wire, rating_key="vod_has_imdb.mkv", imdb_id="tt0133093")
    await seed(wire, rating_key="vod_has_tmdb.mkv", tmdb_id="603")
    await seed(wire, rating_key="vod_empty.mkv", imdb_id="", tmdb_id="")   # blank == no id
    seen: list[str] = []
    cursor = None
    while True:
        params = {"type": "movie", "missingIds": "any", "limit": 3}
        if cursor:
            params["cursor"] = cursor
        body = (await client.get(f"{BASE}/items", params=params, headers=h(keys["read"]))).json()
        seen += [i["ratingKey"] for i in body["items"]]
        cursor = body["nextCursor"]
        if cursor is None:
            break
    assert seen == ["vod_0.mkv", "vod_1.mkv", "vod_2.mkv", "vod_3.mkv", "vod_4.mkv", "vod_empty.mkv"]


async def test_items_missing_imdb_and_tmdb_and_server_filter(client, keys, wire):
    await seed(wire, rating_key="a.mkv", tmdb_id="1")
    await seed(wire, rating_key="b.mkv", imdb_id="tt0133093")
    await seed(wire, rating_key="c.mkv", server_id="xtream_other")
    async def keys_for(**params):
        body = (await client.get(f"{BASE}/items", params={"type": "movie", **params}, headers=h(keys["read"]))).json()
        return [i["ratingKey"] for i in body["items"]]
    assert await keys_for(missingIds="imdb") == ["a.mkv", "c.mkv"]
    assert await keys_for(missingIds="tmdb") == ["b.mkv", "c.mkv"]
    assert await keys_for(missingIds="any") == ["c.mkv"]
    assert await keys_for() == ["a.mkv", "b.mkv", "c.mkv"]
    assert await keys_for(serverId="xtream_other") == ["c.mkv"]


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 501}, {"missingIds": "bogus"}, {"type": "episode"}])
async def test_items_parameter_bounds_are_422(client, keys, params):
    resp = await client.get(f"{BASE}/items", params={"type": "movie", **params}, headers=h(keys["read"]))
    assert resp.status_code == 422


async def test_items_invalid_cursor_is_400(client, keys):
    resp = await client.get(f"{BASE}/items", params={"type": "movie", "cursor": "%%%"}, headers=h(keys["read"]))
    assert resp.status_code == 400 and resp.json()["detail"]["code"] == "invalid_parameter"


# ─── items : visibilité (lot B3, résout l'écart X22) ─────────────────────────


async def _listed(client, keys, **params):
    resp = await client.get(f"{BASE}/items", params={"type": "movie", **params}, headers=h(keys["read"]))
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _seed_visibility(wire):
    await seed(wire, rating_key="a_visible.mkv")
    await seed(wire, rating_key="b_hidden.mkv", is_in_allowed_categories=False)
    await seed(wire, rating_key="c_visible.mkv")


async def test_items_without_visibility_is_unchanged_and_lists_hidden(client, keys, wire):
    await _seed_visibility(wire)
    default = await _listed(client, keys)
    assert [i["ratingKey"] for i in default["items"]] == ["a_visible.mkv", "b_hidden.mkv", "c_visible.mkv"]
    assert default == await _listed(client, keys, visibility="all")


async def test_items_visibility_visible_excludes_hidden_and_keeps_visible(client, keys, wire):
    await _seed_visibility(wire)
    body = await _listed(client, keys, visibility="visible")
    assert [i["ratingKey"] for i in body["items"]] == ["a_visible.mkv", "c_visible.mkv"]
    assert all(i["isHidden"] is False for i in body["items"])


async def test_items_summary_exposes_is_hidden(client, keys, wire):
    await _seed_visibility(wire)
    body = await _listed(client, keys)
    assert {i["ratingKey"]: i["isHidden"] for i in body["items"]} == {
        "a_visible.mkv": False, "b_hidden.mkv": True, "c_visible.mkv": False,
    }
    view = (await client.get(f"{BASE}/items/{SERVER}/b_hidden.mkv", headers=h(keys["read"]))).json()
    assert view["context"]["isHidden"] is True


async def test_items_visibility_pagination_is_stable(client, keys, wire):
    for n in range(7):
        await seed(wire, rating_key=f"vod_{n}.mkv", is_in_allowed_categories=(n % 2 == 0))
    seen: list[str] = []
    cursor = None
    while True:
        params = {"visibility": "visible", "limit": 2}
        if cursor:
            params["cursor"] = cursor
        body = await _listed(client, keys, **params)
        seen += [i["ratingKey"] for i in body["items"]]
        cursor = body["nextCursor"]
        if cursor is None:
            break
    assert seen == ["vod_0.mkv", "vod_2.mkv", "vod_4.mkv", "vod_6.mkv"]


async def test_items_visibility_combines_with_missing_ids_and_server_id(client, keys, wire):
    await seed(wire, rating_key="a.mkv")
    await seed(wire, rating_key="b.mkv", is_in_allowed_categories=False)
    await seed(wire, rating_key="c.mkv", imdb_id="tt0133093")
    await seed(wire, rating_key="d.mkv", server_id="xtream_other")
    got = await _listed(client, keys, visibility="visible", missingIds="imdb")
    assert [i["ratingKey"] for i in got["items"]] == ["a.mkv", "d.mkv"]
    got = await _listed(client, keys, visibility="visible", missingIds="imdb", serverId="xtream_other")
    assert [i["ratingKey"] for i in got["items"]] == ["d.mkv"]


async def test_items_visibility_invalid_value_is_422(client, keys):
    resp = await client.get(f"{BASE}/items", params={"type": "movie", "visibility": "bogus"}, headers=h(keys["read"]))
    assert resp.status_code == 422


async def test_items_visibility_mixed_variants_follow_the_canonical_variant(client, keys, wire):
    # canonical = smallest (filter, sort_order): ("all", "a")
    await seed(wire, rating_key="canon_visible.mkv", variants=(("all", "a"),))
    await seed(wire, rating_key="canon_visible.mkv", variants=(("all", "b"),), is_in_allowed_categories=False)
    await seed(wire, rating_key="canon_hidden.mkv", variants=(("all", "a"),), is_in_allowed_categories=False)
    await seed(wire, rating_key="canon_hidden.mkv", variants=(("all", "b"),))
    everything = await _listed(client, keys)
    assert {i["ratingKey"]: i["isHidden"] for i in everything["items"]} == {
        "canon_hidden.mkv": True, "canon_visible.mkv": False,
    }
    visible = await _listed(client, keys, visibility="visible")
    assert [i["ratingKey"] for i in visible["items"]] == ["canon_visible.mkv"]


# ─── item view ──────────────────────────────────────────────────────────────


async def test_item_view_has_every_field_present_and_null(client, keys, wire):
    await seed(wire)
    resp = await client.get(f"{BASE}/items/{SERVER}/vod_1.mkv", headers=h(keys["read"]))
    body = resp.json()
    assert resp.status_code == 200
    assert resp.headers["ETag"] == f'"{body["revision"]}"'
    assert body["schemaVersion"] == 1 and body["type"] == "movie"
    assert set(body["fields"]) == {
        "title", "originalTitle", "summary", "tagline", "genres", "studios", "countries",
        "contentRating", "year", "premiered", "durationMs", "ids", "ratings",
        "cast", "youtubeTrailer", "posterUrl", "fanartUrl", "posterGallery", "fanartGallery",
        "directors", "writers", "spokenLanguages", "availableTrailers",
    }
    f = body["fields"]
    assert f["title"] == "The Matrix" and f["genres"] == ["Action"] and f["studios"] == []
    assert f["tagline"] is None and f["ids"] == {"imdb": None, "tmdb": None, "tvdb": None, "wikidata": None}
    assert f["ratings"] == {
        "imdb": None, "tmdb": None, "metacritic": None, "rottenTomatoes": None,
    } and f["cast"] == []
    assert body["derived"] == {
        "unificationId": "title_the_matrix_1999", "historyGroupKey": "title_the_matrix_1999",
        "titleSortable": "the matrix", "displayRating": 6.0,
    }
    assert body["lock"] == {"locked": False, "source": None}
    assert body["variants"]["count"] == 1 and body["variants"]["consistent"] is True
    assert body["context"]["durationImplausible"] is False


async def test_a_show_exposes_status_and_an_episode_only_its_fields(client, keys, wire):
    await seed(wire, rating_key="series_1", type="show", status="Ended")
    await seed(wire, rating_key="ep_1.mkv", type="episode", parent_index=2, index=5,
               grandparent_rating_key="series_1", grandparent_title="Show", title="Pilot", duration=120_000)
    show = (await client.get(f"{BASE}/items/{SERVER}/series_1", headers=h(keys["read"]))).json()
    assert show["fields"]["status"] == "Ended"
    ep = (await client.get(f"{BASE}/items/{SERVER}/ep_1.mkv", headers=h(keys["read"]))).json()
    assert set(ep["fields"]) == {"title", "summary", "premiered", "durationMs", "ratings"}
    assert ep["context"]["season"] == 2 and ep["context"]["episode"] == 5
    assert ep["context"]["showRatingKey"] == "series_1" and ep["context"]["showTitle"] == "Show"
    assert ep["context"]["durationImplausible"] is True       # 2 min < 5 min
    assert ep["derived"]["unificationId"] == "title_the_matrix_1999"


async def test_variants_are_all_exposed_canonical_first_and_consistency_is_computed(client, keys, wire):
    await seed(wire, variants=(("cat9", "default"), ("all", "default")))
    body = (await client.get(f"{BASE}/items/{SERVER}/vod_1.mkv", headers=h(keys["read"]))).json()
    assert [v["filter"] for v in body["variants"]["items"]] == ["all", "cat9"]
    assert body["variants"]["count"] == 2 and body["variants"]["consistent"] is True
    assert set(body["variants"]["items"][0]) == {"filter", "sortOrder", "fields", "lock"}
    async with wire() as db:
        await db.execute(update(Media).where(Media.filter == "cat9").values(summary="diverged"))
        await db.commit()
    body = (await client.get(f"{BASE}/items/{SERVER}/vod_1.mkv", headers=h(keys["read"]))).json()
    assert body["variants"]["consistent"] is False
    assert body["fields"]["summary"] == "old"            # canonical variant ('all')


async def test_unknown_item_is_a_404_object(client, keys):
    resp = await client.get(f"{BASE}/items/{SERVER}/nope", headers=h(keys["read"]))
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "item_not_found"


async def test_unknown_route_keeps_the_framework_404(client, keys):
    resp = await client.get(f"{BASE}/nope", headers=h(keys["read"]))
    assert resp.status_code == 404 and resp.json() == {"detail": "Not Found"}


# ─── revision: changes whichever writer touches the item ────────────────────


@pytest.mark.parametrize(
    "change,should_change",
    [
        (dict(summary="by sync"), True), (dict(genres="Drame"), True), (dict(title="Other"), True),
        (dict(imdb_id="tt0133093"), True), (dict(duration=5_000), True), (dict(content_rating="PG"), True),
        (dict(youtube_trailer="dQw4w9WgXcQ"), True), (dict(display_rating=7.5), True),
        (dict(unification_id="imdb://tt1"), True), (dict(match_locked=True, match_source="manual"), True),
        (dict(thumb_url="http://x/y.jpg"), False), (dict(is_broken=True), False),
        (dict(view_count=3), False), (dict(updated_at=999), False), (dict(content_hash="h"), False),
    ],
)
async def test_revision_follows_any_writer(client, keys, wire, change, should_change):
    await seed(wire)
    url = f"{BASE}/items/{SERVER}/vod_1.mkv"
    before = (await client.get(url, headers=h(keys["read"]))).json()["revision"]
    async with wire() as db:
        await db.execute(update(Media).values(**change))
        await db.commit()
    after = (await client.get(url, headers=h(keys["read"]))).json()["revision"]
    assert (after != before) is should_change


async def test_revision_covers_the_non_canonical_variants(client, keys, wire):
    await seed(wire, variants=(("all", "default"), ("zzz", "default")))
    url = f"{BASE}/items/{SERVER}/vod_1.mkv"
    before = (await client.get(url, headers=h(keys["read"]))).json()["revision"]
    async with wire() as db:
        await db.execute(update(Media).where(Media.filter == "zzz").values(summary="elsewhere"))
        await db.commit()
    assert (await client.get(url, headers=h(keys["read"]))).json()["revision"] != before


# ─── episodes ───────────────────────────────────────────────────────────────


async def _seed_show(wire):
    await seed(wire, rating_key="series_1", type="show")
    for rk, s, e in (("ep_b", 1, 2), ("ep_a", 1, 1), ("ep_c", 2, 1), ("ep_dup", 1, 2)):
        await seed(wire, rating_key=rk, type="episode", parent_index=s, index=e,
                   grandparent_rating_key="series_1", title=rk, unification_id="")
    await seed(wire, rating_key="ep_other", type="episode", parent_index=1, index=1,
               grandparent_rating_key="series_2")
    await seed(wire, rating_key="ep_c", server_id="xtream_other", type="episode", parent_index=9,
               index=9, grandparent_rating_key="series_1")


async def test_episodes_are_sorted_and_duplicates_are_all_returned(client, keys, wire):
    await _seed_show(wire)
    body = (await client.get(f"{BASE}/items/{SERVER}/series_1/episodes", headers=h(keys["read"]))).json()
    assert [(i["season"], i["episode"], i["ratingKey"]) for i in body["items"]] == [
        (1, 1, "ep_a"), (1, 2, "ep_b"), (1, 2, "ep_dup"), (2, 1, "ep_c"),
    ]
    assert body["truncated"] is False
    assert all(i["showRatingKey"] == "series_1" and i["type"] == "episode" for i in body["items"])


async def test_episodes_filter_by_season_and_episode(client, keys, wire):
    await _seed_show(wire)
    url = f"{BASE}/items/{SERVER}/series_1/episodes"
    body = (await client.get(url, params={"season": 1}, headers=h(keys["read"]))).json()
    assert [i["ratingKey"] for i in body["items"]] == ["ep_a", "ep_b", "ep_dup"]
    body = (await client.get(url, params={"season": 1, "episode": 2}, headers=h(keys["read"]))).json()
    assert [i["ratingKey"] for i in body["items"]] == ["ep_b", "ep_dup"]
    resp = await client.get(url, params={"episode": 1}, headers=h(keys["read"]))
    assert resp.status_code == 400 and resp.json()["detail"]["code"] == "invalid_parameter"
    assert (await client.get(url, params={"season": 70000}, headers=h(keys["read"]))).status_code == 422
    assert (await client.get(url, params={"season": 1, "episode": 0}, headers=h(keys["read"]))).status_code == 422


async def test_episodes_of_a_non_show_is_400_and_unknown_is_404(client, keys, wire):
    await seed(wire)
    resp = await client.get(f"{BASE}/items/{SERVER}/vod_1.mkv/episodes", headers=h(keys["read"]))
    assert resp.status_code == 400 and resp.json()["detail"]["code"] == "invalid_parameter"
    resp = await client.get(f"{BASE}/items/{SERVER}/series_404/episodes", headers=h(keys["read"]))
    assert resp.status_code == 404


# ─── fields ─────────────────────────────────────────────────────────────────


async def test_fields_route_renders_the_contract_table(client, keys):
    body = (await client.get(f"{BASE}/fields", headers=h(keys["read"]))).json()
    assert body["schemaVersion"] == 1
    assert body["types"]["episode"] == [
        "durationMs", "premiered", "ratings.imdb", "ratings.tmdb", "summary", "title",
    ]
    assert "status" in body["types"]["show"] and "status" not in body["types"]["movie"]
    for names in body["types"].values():
        assert names == sorted(names)
        assert "ids" not in names and "ratings" not in names  # dotted sub-fields only
    assert body["limits"]["title"] == {"min": 1, "max": 300}
    json.dumps(body)
