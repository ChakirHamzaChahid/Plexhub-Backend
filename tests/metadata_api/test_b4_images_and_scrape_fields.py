"""Lot B4 — `thumbUrl`/`artUrl` en lecture, huit champs de scrape + notes Metacritic/Rotten Tomatoes
en écriture (contrat plexhubTMM 17, ADR 0013)."""
from __future__ import annotations

import json

import pytest

from app.services.metadata_views import safe_image_url
from tests._metadata_support import BASE, SERVER, count_edits, fetch, h, seed, write_headers

URL = f"{BASE}/items/{SERVER}/vod_1.mkv"
P = "https://image.tmdb.org/t/p/w342/a.jpg"
F = "https://image.tmdb.org/t/p/w1280/b.jpg"
POSTER = "https://img.example.org/poster.jpg"
FANART = "https://img.example.org/fanart.jpg"


async def rev(client, keys, rk="vod_1.mkv"):
    return (await client.get(f"{BASE}/items/{SERVER}/{rk}", headers=h(keys["rw"]))).json()["revision"]


async def patch(client, keys, fields, *, idem="i-1", rk="vod_1.mkv", allow=None):
    body = {"fields": fields}
    if allow is not None:
        body["allowRegroup"] = allow
    return await client.patch(
        f"{BASE}/items/{SERVER}/{rk}", json=body,
        headers=write_headers(keys["rw"], await rev(client, keys, rk), idem),
    )


# ─── safe_image_url (pur) ───────────────────────────────────────────────────


@pytest.mark.parametrize("value", [
    "http://image.tmdb.org/a.jpg",
    "https://user:pw@host.example/a.jpg",
    "https://user@host.example/a.jpg",
    "https://host.example/a.jpg?X-Plex-Token=abc",
    "https://host.example/a.jpg?x-plex-token=abc",
    "https://host.example/a.jpg?api_key=abc",
    "https://host.example/a.jpg?ApiKey=abc",
    "https://host.example/a.jpg?token=abc",
    "https://host.example/a.jpg?password=abc",
    "https://host.example/a.jpg?username=abc",
    "/library/metadata/1/thumb/2", "", None, "https://", "https://[bad", "ftp://h/a.jpg",
])
def test_safe_image_url_rejects(value):
    assert safe_image_url(value) is None


def test_safe_image_url_accepts_plain_https():
    assert safe_image_url(P) == P
    assert safe_image_url("https://h.example/a.jpg?w=300") == "https://h.example/a.jpg?w=300"


# ─── lecture ────────────────────────────────────────────────────────────────


async def test_thumb_priority_user_input_then_resolved_never_raw(client, keys, wire):
    await seed(wire, rating_key="a.mkv", poster_url=POSTER, fanart_url=FANART,
               resolved_thumb_url=P, resolved_art_url=F, thumb_url="https://raw.example/t.jpg")
    await seed(wire, rating_key="b.mkv", resolved_thumb_url=P, resolved_art_url=F)
    await seed(wire, rating_key="c.mkv", thumb_url="https://raw.example/t.jpg", art_url="https://raw.example/a.jpg")
    items = {
        i["ratingKey"]: i
        for i in (await client.get(f"{BASE}/items", params={"type": "movie"}, headers=h(keys["read"]))).json()["items"]
    }
    assert (items["a.mkv"]["thumbUrl"], items["a.mkv"]["artUrl"]) == (POSTER, FANART)
    assert (items["b.mkv"]["thumbUrl"], items["b.mkv"]["artUrl"]) == (P, F)
    assert (items["c.mkv"]["thumbUrl"], items["c.mkv"]["artUrl"]) == (None, None)


async def test_unsafe_stored_url_falls_back_to_null_not_to_next_source(client, keys, wire):
    await seed(wire, poster_url="https://h.example/a.jpg?token=x", resolved_thumb_url=P)
    item = (await client.get(f"{BASE}/items", params={"type": "movie"}, headers=h(keys["read"]))).json()["items"][0]
    assert item["thumbUrl"] is None          # the chosen source is filtered, no silent fall-through
    assert "token" not in json.dumps(item)


async def test_item_view_context_has_computed_urls_and_fields_the_stored_value(client, keys, wire):
    await seed(wire, resolved_thumb_url=P, resolved_art_url=F)
    body = (await client.get(URL, headers=h(keys["read"]))).json()
    assert body["context"]["thumbUrl"] == P and body["context"]["artUrl"] == F
    assert body["fields"]["posterUrl"] is None and body["fields"]["fanartUrl"] is None   # no fallback
    assert body["fields"]["posterGallery"] == [] and body["fields"]["availableTrailers"] == []
    assert body["fields"]["directors"] == [] and body["fields"]["spokenLanguages"] == []
    assert body["fields"]["ratings"]["metacritic"] is None and body["fields"]["ratings"]["rottenTomatoes"] is None


async def test_episode_view_has_no_b4_field(client, keys, wire):
    await seed(wire, rating_key="e.mkv", type="episode", resolved_thumb_url=P)
    body = (await client.get(f"{BASE}/items/{SERVER}/e.mkv", headers=h(keys["read"]))).json()
    assert set(body["fields"]) == {"title", "summary", "premiered", "durationMs", "ratings"}
    assert set(body["fields"]["ratings"]) == {"imdb", "tmdb"}
    assert body["context"]["thumbUrl"] == P
    fields = (await client.get(f"{BASE}/fields", headers=h(keys["read"]))).json()
    assert "posterUrl" not in fields["types"]["episode"]
    for name in ("posterUrl", "fanartUrl", "posterGallery", "fanartGallery", "directors", "writers",
                 "spokenLanguages", "availableTrailers", "ratings.metacritic", "ratings.rottenTomatoes"):
        assert name in fields["types"]["movie"] and name in fields["types"]["show"]
        assert name not in fields["types"]["episode"]


async def test_episode_rejects_b4_field(client, keys, wire):
    await seed(wire, rating_key="e.mkv", type="episode")
    resp = await patch(client, keys, {"posterUrl": POSTER}, rk="e.mkv")
    assert resp.status_code == 400 and resp.json()["detail"]["code"] == "invalid_field"


# ─── écriture ───────────────────────────────────────────────────────────────


async def test_write_every_b4_field_and_read_it_back(client, keys, wire):
    await seed(wire, variants=(("all", "default"), ("cat2", "default")))
    fields = {
        "posterUrl": POSTER, "fanartUrl": FANART,
        "posterGallery": [P, POSTER, P], "fanartGallery": [F],
        "directors": ["Lana Wachowski", "Lilly Wachowski"], "writers": ["Lana Wachowski"],
        "spokenLanguages": ["en", "fr"],
        "availableTrailers": [
            {"name": "Teaser", "url": "https://v.example/t1", "isPrimary": False},
            {"name": "Trailer", "url": "https://v.example/t2", "isPrimary": True},
        ],
        "ratings": {"metacritic": {"value": 7.3, "votes": 40}, "rottenTomatoes": {"value": 8.7}},
    }
    resp = await patch(client, keys, fields, allow=True)
    assert resp.status_code == 200, resp.text
    assert resp.json()["changedFields"] == sorted([
        "posterUrl", "fanartUrl", "posterGallery", "fanartGallery", "directors", "writers",
        "spokenLanguages", "availableTrailers", "ratings.metacritic", "ratings.rottenTomatoes",
    ])
    for row in await fetch(wire):                      # every variant
        assert (row.poster_url, row.fanart_url) == (POSTER, FANART)
        assert json.loads(row.poster_gallery_json) == [P, POSTER]          # dedup, order kept
        assert json.loads(row.fanart_gallery_json) == [F]
        assert (row.directors, row.writers, row.spoken_languages) == (
            "Lana Wachowski, Lilly Wachowski", "Lana Wachowski", "en, fr")
        assert [t["isPrimary"] for t in json.loads(row.available_trailers_json)] == [False, True]
        assert (row.metacritic_rating, row.metacritic_votes, row.rt_rating, row.rt_votes) == (7.3, 40, 8.7, None)
        assert row.match_locked is True
    view = (await client.get(URL, headers=h(keys["read"]))).json()
    assert view["fields"]["posterUrl"] == POSTER and view["fields"]["directors"] == [
        "Lana Wachowski", "Lilly Wachowski"]
    assert view["fields"]["ratings"]["metacritic"] == {"value": 7.3, "votes": 40}
    assert view["context"]["thumbUrl"] == POSTER and view["context"]["artUrl"] == FANART
    assert view["derived"]["displayRating"] == pytest.approx(6.0)     # metacritic/RT never move display_rating


@pytest.mark.parametrize("fields", [
    {"posterUrl": "http://h.example/a.jpg"},
    {"posterUrl": "https://h.example/a b.jpg"},
    {"posterUrl": "https://h.example/" + "a" * 2048},
    {"fanartUrl": "/relative/path.jpg"},
    {"posterGallery": ["https://h.example/%d.jpg" % i for i in range(31)]},
    {"fanartGallery": ["http://h.example/a.jpg"]},
    {"directors": ["A, B"]},
    {"directors": ["x"] * 21},
    {"writers": [""]},
    {"spokenLanguages": ["y" * 65]},
    {"availableTrailers": [{"name": "t", "url": "http://h/t", "isPrimary": False}]},
    {"availableTrailers": [{"name": "", "url": "https://h/t", "isPrimary": False}]},
    {"availableTrailers": [{"name": "a", "url": "https://h/a", "isPrimary": True},
                           {"name": "b", "url": "https://h/b", "isPrimary": True}]},
    {"availableTrailers": [{"name": "t", "url": "https://h/t", "isPrimary": False}] * 21},
    {"ratings": {"metacritic": {"value": 11}}},
    {"ratings": {"rottenTomatoes": {"value": -1}}},
    {"ratings": {"rottenTomatoes": {"value": 5, "votes": -1}}},
])
async def test_invalid_values_are_400_invalid_field(client, keys, wire, fields):
    await seed(wire)
    resp = await patch(client, keys, fields)
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"]["code"] == "invalid_field"
    assert await count_edits(wire) == 0


async def test_unknown_trailer_key_is_422(client, keys, wire):
    await seed(wire)
    resp = await patch(client, keys, {"availableTrailers": [
        {"name": "t", "url": "https://h/t", "isPrimary": False, "extra": 1}]})
    assert resp.status_code == 422


async def test_null_and_empty_list_clear_and_noop_is_recognised(client, keys, wire):
    await seed(wire, poster_url=POSTER, poster_gallery_json=json.dumps([P]), directors="A",
               metacritic_rating=5.0, metacritic_votes=3)
    noop = await patch(client, keys, {"posterUrl": f" {POSTER}"[1:], "posterGallery": [P], "directors": ["A"]})
    assert noop.json()["noop"] is True
    resp = await patch(client, keys, {
        "posterUrl": None, "posterGallery": [], "directors": [], "ratings": {"metacritic": None},
    }, idem="i-2")
    assert resp.status_code == 200 and resp.json()["noop"] is False
    (row,) = await fetch(wire)
    assert (row.poster_url, row.poster_gallery_json, row.directors) == (None, None, None)
    assert (row.metacritic_rating, row.metacritic_votes) == (None, None)


async def test_invalid_stored_json_reads_as_empty(client, keys, wire):
    await seed(wire, poster_gallery_json="{not json", available_trailers_json='"str"')
    body = (await client.get(URL, headers=h(keys["read"]))).json()
    assert body["fields"]["posterGallery"] == [] and body["fields"]["availableTrailers"] == []


async def test_revert_restores_images_of_each_variant(client, keys, wire):
    await seed(wire, variants=(("all", "default"), ("cat2", "default")), poster_url=POSTER)
    await seed(wire, rating_key="x.mkv")  # unrelated item untouched
    resp = await patch(client, keys, {"posterUrl": "https://img.example.org/new.jpg",
                                      "fanartGallery": [F], "ratings": {"rottenTomatoes": {"value": 9}}})
    edit_id = resp.json()["editId"]
    back = await client.post(
        f"{BASE}/edits/{edit_id}/revert", json={},
        headers=write_headers(keys["rw"], await rev(client, keys), "i-rv"),
    )
    assert back.status_code == 200, back.text
    for row in await fetch(wire):
        assert (row.poster_url, row.fanart_gallery_json, row.rt_rating) == (POSTER, None, None)


async def test_revision_changes_with_a_b4_field_but_not_with_resolved_urls(client, keys, wire):
    await seed(wire)
    r0 = await rev(client, keys)
    from sqlalchemy import update

    from app.models.database import Media
    async with wire() as db:
        await db.execute(update(Media).values(resolved_thumb_url=P))
        await db.commit()
    assert await rev(client, keys) == r0
    await patch(client, keys, {"posterUrl": POSTER})
    assert await rev(client, keys) != r0


async def test_preview_does_not_write(client, keys, wire):
    await seed(wire)
    r = await client.post(f"{URL}/preview", json={"fields": {"posterUrl": POSTER}}, headers=h(keys["read"]))
    assert r.status_code == 200 and r.json()["changedFields"] == ["posterUrl"]
    assert (await fetch(wire))[0].poster_url is None


async def test_list_response_size_is_bounded(client, keys, wire):
    for n in range(20):
        await seed(wire, rating_key=f"m{n}.mkv", resolved_thumb_url=P, resolved_art_url=F)
    resp = await client.get(f"{BASE}/items", params={"type": "movie", "limit": 20}, headers=h(keys["read"]))
    assert len(resp.content) / 20 < 700            # bytes per row: two short URLs stay cheap
