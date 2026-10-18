"""B2-06 — simulation et écriture : `POST …/preview` et `PATCH /items/{s}/{r}` (contrat § 4.4–4.6,
§ 5.5–5.7, § 6, § 9 ; ADR 0011 § D2.2–D2.5)."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import select, update

from app.models.database import Media, MetadataEdit
from app.services import unified_group_service
from tests._metadata_support import BASE, SERVER, count_edits, fetch, h, seed, write_headers

URL = f"{BASE}/items/{SERVER}/vod_1.mkv"


async def revision(client, key, rating_key="vod_1.mkv", server=SERVER) -> str:
    return (await client.get(f"{BASE}/items/{server}/{rating_key}", headers=h(key))).json()["revision"]


async def patch(client, keys, fields, *, idem="i-1", allow=None, rev=None, who="rw", rating_key="vod_1.mkv",
                reason=None):
    body: dict = {"fields": fields}
    if allow is not None:
        body["allowRegroup"] = allow
    if reason is not None:
        body["reason"] = reason
    rev = rev or await revision(client, keys["rw"], rating_key)
    return await client.patch(
        f"{BASE}/items/{SERVER}/{rating_key}", json=body, headers=write_headers(keys[who], rev, idem),
    )


async def preview(client, keys, fields, *, allow=None, who="rw", rating_key="vod_1.mkv", rev=None):
    body: dict = {"fields": fields}
    if allow is not None:
        body["allowRegroup"] = allow
    extra = {"If-Match": f'"{rev}"'} if rev else {}
    return await client.post(
        f"{BASE}/items/{SERVER}/{rating_key}/preview", json=body, headers=h(keys[who], **extra),
    )


# ─── auth / headers ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "who,expected",
    [("master", 200), ("rw", 200), ("write", 200), ("read", 403), ("scrape_read", 403), ("scrape", 403),
     ("none", 403), ("revoked", 401), ("expired", 401), ("missing", 401)],
)
async def test_patch_auth_matrix(client, keys, wire, who, expected):
    await seed(wire)
    rev = await revision(client, keys["rw"])
    key = None if who == "missing" else keys[who]
    resp = await client.patch(
        URL, json={"fields": {"summary": "x"}}, headers=write_headers(key, rev) if key else h(None),
    )
    assert resp.status_code == expected, resp.text
    if expected in (401, 403):
        assert resp.json()["detail"]["code"] == {401: "invalid_api_key", 403: "insufficient_scope"}[expected]
        assert (await fetch(wire))[0].summary == "old"


@pytest.mark.parametrize(
    "who,expected",
    [("master", 200), ("read", 200), ("rw", 200), ("scrape_read", 200), ("write", 403), ("scrape", 403),
     ("none", 403)],
)
async def test_preview_runs_under_media_read_only(client, keys, wire, who, expected):
    await seed(wire)
    resp = await preview(client, keys, {"summary": "x"}, who=who)
    assert resp.status_code == expected, resp.text


async def test_missing_if_match_or_idempotency_key_is_428(client, keys, wire):
    await seed(wire)
    resp = await client.patch(URL, json={"fields": {"summary": "x"}}, headers=h(keys["rw"], **{"Idempotency-Key": "k"}))
    assert resp.status_code == 428
    assert resp.json()["detail"] == {
        "code": "precondition_required", "message": "If-Match is required", "header": "If-Match",
    }
    rev = await revision(client, keys["rw"])
    resp = await client.patch(URL, json={"fields": {"summary": "x"}}, headers=h(keys["rw"], **{"If-Match": f'"{rev}"'}))
    assert resp.status_code == 428 and resp.json()["detail"]["header"] == "Idempotency-Key"
    assert (await fetch(wire))[0].summary == "old"


@pytest.mark.parametrize("if_match", ['W/"abc"', 'bad value!', '""', "x" * 129])
async def test_weak_or_malformed_if_match_is_400(client, keys, wire, if_match):
    await seed(wire)
    resp = await client.patch(
        URL, json={"fields": {"summary": "x"}},
        headers=h(keys["rw"], **{"If-Match": if_match, "Idempotency-Key": "k"}),
    )
    assert resp.status_code == 400 and resp.json()["detail"]["code"] == "invalid_parameter"


async def test_malformed_idempotency_key_is_400(client, keys, wire):
    await seed(wire)
    rev = await revision(client, keys["rw"])
    resp = await client.patch(
        URL, json={"fields": {"summary": "x"}},
        headers=h(keys["rw"], **{"If-Match": f'"{rev}"', "Idempotency-Key": "has space"}),
    )
    assert resp.status_code == 400


# ─── every writable field ───────────────────────────────────────────────────


async def test_every_movie_field_is_written_in_the_right_columns(client, keys, wire):
    await seed(wire)
    cast = [{"name": "Keanu Reeves", "role": "Neo", "thumb": "https://i.example/k.jpg"},
            {"name": "Carrie-Anne Moss", "role": "Trinity"}]
    resp = await patch(client, keys, {
        "title": "Matrix Reloaded", "originalTitle": "The Matrix Reloaded", "summary": "New summary",
        "tagline": "Free your mind", "genres": ["Action", "Science-Fiction"], "studios": ["Warner", "Village"],
        "countries": ["US", "AU"], "contentRating": "R", "year": 2003, "premiered": "2003-05-15",
        "durationMs": 8_280_000, "ids": {"imdb": "tt0234215", "tmdb": "604", "tvdb": "81189", "wikidata": "Q1"},
        "ratings": {"imdb": {"value": 7.2, "votes": 600}, "tmdb": {"value": 6.8, "votes": 100}},
        "cast": cast, "youtubeTrailer": "dQw4w9WgXcQ",
    }, allow=True, reason="plexhubTMM")
    assert resp.status_code == 200, resp.text
    (row,) = await fetch(wire)
    assert (row.title, row.title_sortable, row.original_title) == (
        "Matrix Reloaded", "matrix reloaded", "The Matrix Reloaded")
    assert (row.summary, row.tagline, row.genres) == ("New summary", "Free your mind", "Action, Science-Fiction")
    assert (row.studio, row.country, row.content_rating) == ("Warner, Village", "US, AU", "R")
    assert (row.year, row.premiered, row.duration) == (2003, "2003-05-15", 8_280_000)
    assert (row.imdb_id, row.tmdb_id, row.tvdb_id, row.wikidata_id) == ("tt0234215", "604", "81189", "Q1")
    assert (row.imdb_rating, row.imdb_votes, row.tmdb_rating, row.tmdb_votes) == (7.2, 600, 6.8, 100)
    assert row.display_rating == pytest.approx(7.0)               # blend
    assert row.cast == "Keanu Reeves, Carrie-Anne Moss"
    assert [c["name"] for c in json.loads(row.cast_json)] == ["Keanu Reeves", "Carrie-Anne Moss"]
    assert row.youtube_trailer == "dQw4w9WgXcQ"
    assert row.unification_id == "imdb://tt0234215" and row.history_group_key == "imdb://tt0234215"
    assert row.match_locked is True and row.match_source == "plexhubtmm" and row.updated_at > 0
    body = resp.json()
    assert body["kind"] == "patch" and body["dryRun"] is False and body["noop"] is False
    assert body["replayed"] is False and body["editId"]
    assert resp.headers["ETag"] == f'"{body["revisionAfter"]}"'
    assert body["revisionAfter"] == await revision(client, keys["rw"])
    assert body["changedFields"] == sorted(body["changedFields"])
    assert body["after"]["title"] == "Matrix Reloaded" and body["before"]["title"] == "The Matrix"


async def test_a_show_status_and_null_clears(client, keys, wire):
    await seed(wire, rating_key="series_1", type="show", status="Ended", tagline="t", genres="A, B",
               imdb_rating=8.0, imdb_votes=3, cast="X", cast_json='[{"name": "X"}]')
    resp = await patch(client, keys, {
        "status": "Continuing", "tagline": None, "genres": [], "ratings": {"imdb": None}, "cast": [],
    }, rating_key="series_1")
    assert resp.status_code == 200, resp.text
    (row,) = await fetch(wire, "series_1")
    assert (row.status, row.tagline, row.genres) == ("Continuing", None, None)
    assert (row.imdb_rating, row.imdb_votes, row.cast, row.cast_json) == (None, None, None, None)


@pytest.mark.parametrize(
    "fields,code,field",
    [
        ({"title": None}, 400, "title"),
        ({"genres": ["Action, Drame"]}, 400, "genres"),
        ({"status": "Ended"}, 400, "status"),                       # movie: not writable
        ({"year": 1700}, 400, "year"),
        ({"ids": {"imdb": "bad"}}, 400, "ids.imdb"),
        ({"ratings": {"imdb": {"value": 11}}}, 400, "ratings.imdb"),
        ({"cast": [{"name": "A", "thumb": "http://x/a.jpg"}]}, 400, "cast"),
    ],
)
async def test_invalid_values_are_400_naming_the_field_and_write_nothing(client, keys, wire, fields, code, field):
    await seed(wire)
    resp = await patch(client, keys, fields)
    assert resp.status_code == code, resp.text
    assert resp.json()["detail"]["code"] == "invalid_field" and resp.json()["detail"]["field"] == field
    assert (await fetch(wire))[0].summary == "old" and await count_edits(wire) == 0


@pytest.mark.parametrize(
    "fields",
    [{"thumbUrl": "x"}, {"unificationId": "x"}, {"displayRating": 1}, {"matchLocked": True}, {"rating": 3},
     {"ids": {"unknown": "x"}}, {"title": 5}, {"year": "1999"}, {"genres": "Action"},
     {"ratings": {"imdb": {"value": "7"}}}, {"cast": [{"name": "A", "extra": 1}]}],
)
async def test_unknown_keys_and_wrong_types_are_the_native_422(client, keys, wire, fields):
    await seed(wire)
    resp = await patch(client, keys, fields)
    assert resp.status_code == 422, resp.text
    assert isinstance(resp.json()["detail"], list)
    assert await count_edits(wire) == 0


async def test_content_rating_is_refused_on_an_adult_item(client, keys, wire):
    await seed(wire, is_adult=True, content_rating="XXX")
    resp = await patch(client, keys, {"contentRating": "PG"})
    assert resp.status_code == 400 and resp.json()["detail"]["field"] == "contentRating"
    assert (await fetch(wire))[0].content_rating == "XXX"
    ok = await patch(client, keys, {"summary": "fine"}, idem="i-2")
    assert ok.status_code == 200 and (await fetch(wire))[0].content_rating == "XXX"


# ─── no-op / lock / updated_at ──────────────────────────────────────────────


async def test_noop_writes_nothing_and_keeps_the_revision(client, keys, wire):
    await seed(wire, updated_at=111)
    before = await revision(client, keys["rw"])
    resp = await patch(client, keys, {"summary": "old", "genres": ["Action"], "title": " The Matrix "})
    body = resp.json()
    assert resp.status_code == 200 and body["noop"] is True and body["editId"] is None
    assert body["revisionBefore"] == body["revisionAfter"] == before
    (row,) = await fetch(wire)
    assert row.match_locked is False and row.match_source is None and row.updated_at == 111
    assert await count_edits(wire) == 0
    empty = await patch(client, keys, {}, idem="i-2")
    assert empty.json()["noop"] is True and await count_edits(wire) == 0


async def test_locking_rules(client, keys, wire):
    await seed(wire, rating_key="free.mkv")
    await seed(wire, rating_key="manual.mkv", match_locked=True, match_source="manual")
    assert (await patch(client, keys, {"summary": "a"}, rating_key="free.mkv")).status_code == 200
    assert (await patch(client, keys, {"summary": "a"}, rating_key="manual.mkv", idem="i-2")).status_code == 200
    (free,) = await fetch(wire, "free.mkv")
    (manual,) = await fetch(wire, "manual.mkv")
    assert (free.match_locked, free.match_source) == (True, "plexhubtmm")
    assert (manual.match_locked, manual.match_source) == (True, "manual")        # kept


async def test_all_variants_are_written_in_one_transaction_and_locked(client, keys, wire):
    await seed(wire, variants=(("all", "default"), ("cat2", "default"), ("cat3", "default")))
    resp = await patch(client, keys, {"summary": "new", "ids": {"tmdb": "603"}}, allow=True)
    assert resp.status_code == 200
    rows = await fetch(wire)
    assert [r.summary for r in rows] == ["new"] * 3
    assert {(r.match_locked, r.match_source, r.unification_id) for r in rows} == {
        (True, "plexhubtmm", "tmdb://603")}
    assert len({r.updated_at for r in rows}) == 1
    assert resp.json()["variants"]["count"] == 3 and resp.json()["variantsAfter"]["count"] == 3


async def test_divergent_variants_can_be_aligned_and_untouched_fields_stay_divergent(client, keys, wire):
    await seed(wire, variants=(("all", "default"),))
    await seed(wire, variants=(("cat2", "default"),), summary="diverged", genres="Comedie")
    # aligning the summary changes variant cat2 only; genres stay divergent
    resp = await patch(client, keys, {"summary": "old"})
    body = resp.json()
    assert resp.status_code == 200 and body["noop"] is False and body["changedFields"] == ["summary"]
    rows = {r.filter: r for r in await fetch(wire)}
    assert rows["cat2"].summary == "old" and rows["cat2"].genres == "Comedie"
    assert all(r.match_locked for r in rows.values())
    assert body["variants"]["consistent"] is False and body["variantsAfter"]["consistent"] is False


async def test_atomicity_a_failure_before_commit_leaves_every_variant_and_the_journal_untouched(
    client, keys, wire, monkeypatch
):
    await seed(wire, variants=(("all", "default"), ("cat2", "default")))
    from app.services import metadata_service

    def boom(*_a, **_k):
        raise RuntimeError("simulated failure after the UPDATEs")

    monkeypatch.setattr(metadata_service, "new_ulid", boom)
    rev = await revision(client, keys["rw"])
    with pytest.raises(RuntimeError):
        await client.patch(URL, json={"fields": {"summary": "new"}}, headers=write_headers(keys["rw"], rev))
    rows = await fetch(wire)
    assert [r.summary for r in rows] == ["old", "old"] and not any(r.match_locked for r in rows)
    assert await count_edits(wire) == 0


# ─── concurrency / idempotency ──────────────────────────────────────────────


async def test_stale_if_match_is_412_with_the_current_revision(client, keys, wire):
    await seed(wire)
    stale = await revision(client, keys["rw"])
    async with wire() as db:
        await db.execute(update(Media).values(summary="changed by sync"))
        await db.commit()
    current = await revision(client, keys["rw"])
    resp = await patch(client, keys, {"summary": "mine"}, rev=stale)
    assert resp.status_code == 412
    assert resp.json()["detail"] == {
        "code": "revision_mismatch", "message": "The item changed since it was read",
        "currentRevision": current,
    }
    assert (await fetch(wire))[0].summary == "changed by sync" and await count_edits(wire) == 0


async def test_idempotent_replay_returns_the_recorded_response_without_rewriting(client, keys, wire):
    await seed(wire)
    first = await patch(client, keys, {"summary": "new"}, idem="same-key")
    assert first.status_code == 200
    async with wire() as db:                                  # the item moves on afterwards
        await db.execute(update(Media).values(tagline="moved on"))
        await db.commit()
    rev_now = await revision(client, keys["rw"])
    again = await client.patch(
        URL, json={"fields": {"summary": "new"}},
        headers=write_headers(keys["rw"], "any-revision-even-stale", "same-key"),
    )
    assert again.status_code == 200
    body = again.json()
    assert body["replayed"] is True and body["editId"] == first.json()["editId"]
    assert {**body, "replayed": False} == first.json()
    assert await count_edits(wire) == 1
    assert (await fetch(wire))[0].tagline == "moved on" and rev_now != body["revisionAfter"]


async def test_same_idempotency_key_with_another_body_is_409(client, keys, wire):
    await seed(wire)
    assert (await patch(client, keys, {"summary": "one"}, idem="k")).status_code == 200
    resp = await patch(client, keys, {"summary": "two"}, idem="k")
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "idempotency_key_reused"
    assert (await fetch(wire))[0].summary == "one" and await count_edits(wire) == 1


async def test_idempotency_key_is_scoped_per_api_key(client, keys, wire):
    await seed(wire)
    assert (await patch(client, keys, {"summary": "one"}, idem="k")).status_code == 200
    rev = await revision(client, keys["rw"])
    other = await client.patch(
        URL, json={"fields": {"summary": "two"}}, headers=write_headers(keys["master"], rev, "k"),
    )
    assert other.status_code == 200 and other.json()["replayed"] is False
    assert await count_edits(wire) == 2


async def test_a_refused_request_is_not_recorded_for_replay(client, keys, wire):
    await seed(wire)
    assert (await patch(client, keys, {"genres": ["a,b"]}, idem="k")).status_code == 400
    assert (await patch(client, keys, {"summary": "fixed"}, idem="k")).status_code == 200


# ─── regrouping ─────────────────────────────────────────────────────────────


async def test_regroup_needs_allow_regroup(client, keys, wire):
    await seed(wire)                                   # title_the_matrix_1999
    await seed(wire, rating_key="vod_twin.mkv", imdb_id="tt0133093", unification_id="imdb://tt0133093")
    await seed(wire, rating_key="vod_twin2.mkv", imdb_id="tt0133093", unification_id="imdb://tt0133093")
    await seed(wire, rating_key="vod_samegroup.mkv")   # shares title_the_matrix_1999
    resp = await patch(client, keys, {"ids": {"imdb": "tt0133093"}})
    assert resp.status_code == 409
    assert resp.json()["detail"] == {
        "code": "regroup_not_allowed", "message": "The write would change the unification group",
        "unificationIdBefore": "title_the_matrix_1999", "unificationIdAfter": "imdb://tt0133093",
        "membersBefore": 1, "membersAfter": 2,
    }
    assert (await fetch(wire))[0].unification_id == "title_the_matrix_1999" and await count_edits(wire) == 0
    ok = await patch(client, keys, {"ids": {"imdb": "tt0133093"}}, allow=True, idem="i-2")
    body = ok.json()
    assert ok.status_code == 200
    assert body["regroup"] == {
        "changes": True, "unificationIdBefore": "title_the_matrix_1999",
        "unificationIdAfter": "imdb://tt0133093", "membersBefore": 1, "membersAfter": 2,
    }
    assert body["derivedAfter"]["unificationId"] == "imdb://tt0133093"


async def test_no_regroup_reports_equal_member_counts(client, keys, wire):
    await seed(wire)
    body = (await patch(client, keys, {"summary": "x"})).json()
    assert body["regroup"]["changes"] is False
    assert body["regroup"]["membersBefore"] == body["regroup"]["membersAfter"] == 0


async def test_episodes_keep_their_unification_and_history_keys(client, keys, wire):
    """Deux épisodes « Pilot » de séries différentes ne fusionnent pas leurs historiques."""
    for rk, series in (("ep_1.mkv", "series_1"), ("ep_2.mkv", "series_2")):
        await seed(wire, rating_key=rk, type="episode", title="Old", unification_id="",
                   history_group_key=f"{rk}{SERVER}", grandparent_rating_key=series, parent_index=1, index=1)
    for rk in ("ep_1.mkv", "ep_2.mkv"):
        resp = await patch(client, keys, {"title": "Pilot", "summary": "s"}, rating_key=rk, idem=f"i-{rk}")
        assert resp.status_code == 200, resp.text
        assert resp.json()["regroup"]["changes"] is False
    for rk in ("ep_1.mkv", "ep_2.mkv"):
        (row,) = await fetch(wire, rk)
        assert (row.title, row.title_sortable) == ("Pilot", "pilot")
        assert row.unification_id == "" and row.history_group_key == f"{rk}{SERVER}"
        assert row.match_locked is True


async def test_an_episode_refuses_movie_only_fields(client, keys, wire):
    await seed(wire, rating_key="ep_1.mkv", type="episode", title="E", unification_id="",
               history_group_key=f"ep_1.mkv{SERVER}")
    resp = await patch(client, keys, {"genres": ["x"]}, rating_key="ep_1.mkv")
    assert resp.status_code == 400 and resp.json()["detail"]["field"] == "genres"


# ─── preview ≡ patch ────────────────────────────────────────────────────────


def _strip(result: dict) -> dict:
    return {k: v for k, v in result.items() if k not in ("dryRun", "editId", "atMs", "replayed")}


async def test_preview_and_patch_agree_and_preview_writes_nothing(client, keys, wire):
    await seed(wire, variants=(("all", "default"), ("cat2", "default")))
    fields = {"title": "Matrix 2", "year": 2003, "ids": {"tmdb": "604"},
              "ratings": {"tmdb": {"value": 6.5, "votes": 9}}, "genres": ["Action", "SF"]}

    async def snapshot():
        return [(r.filter, {c.name: getattr(r, c.name) for c in Media.__table__.columns}) for r in await fetch(wire)]

    before = await snapshot()
    pre = await preview(client, keys, fields, allow=True)
    assert pre.status_code == 200
    pre_body = pre.json()
    assert pre_body["dryRun"] is True and pre_body["editId"] is None
    assert await snapshot() == before and await count_edits(wire) == 0
    assert (await revision(client, keys["rw"])) == pre_body["revisionBefore"]

    real = await patch(client, keys, fields, allow=True)
    real_body = real.json()
    assert real.status_code == 200 and real_body["dryRun"] is False and real_body["editId"]
    assert _strip(real_body) == _strip(pre_body)
    assert real_body["revisionAfter"] == pre_body["revisionAfter"] == await revision(client, keys["rw"])
    assert pre.headers["ETag"] == f'"{pre_body["revisionAfter"]}"'


async def test_preview_fails_exactly_like_the_write(client, keys, wire):
    await seed(wire)
    bad = await preview(client, keys, {"genres": ["a,b"]})
    assert bad.status_code == 400 and bad.json()["detail"]["field"] == "genres"
    regroup = await preview(client, keys, {"ids": {"imdb": "tt0133093"}})
    assert regroup.status_code == 409 and regroup.json()["detail"]["code"] == "regroup_not_allowed"
    assert (await preview(client, keys, {"ids": {"imdb": "tt0133093"}}, allow=True)).status_code == 200
    stale = await preview(client, keys, {"summary": "x"}, rev="not-the-revision")
    assert stale.status_code == 412 and stale.json()["detail"]["code"] == "revision_mismatch"
    assert (await preview(client, keys, {"summary": "x"})).status_code == 200      # If-Match optional
    missing = await client.post(f"{BASE}/items/{SERVER}/nope/preview", json={"fields": {}}, headers=h(keys["rw"]))
    assert missing.status_code == 404


async def test_preview_of_a_noop_reports_noop(client, keys, wire):
    await seed(wire)
    body = (await preview(client, keys, {"summary": "old"})).json()
    assert body["noop"] is True and body["changedFields"] == []
    assert body["revisionBefore"] == body["revisionAfter"]


# ─── rebuild / limits / deadline ────────────────────────────────────────────


async def test_snapshot_rebuild_is_scheduled_only_after_a_real_write(client, keys, wire, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(unified_group_service, "schedule_rebuild", lambda t, **_k: calls.append(t))
    await seed(wire)
    await preview(client, keys, {"summary": "x"})
    assert (await patch(client, keys, {"summary": "old"})).json()["noop"] is True
    assert calls == []
    assert (await patch(client, keys, {"summary": "new"}, idem="i-2")).status_code == 200
    assert calls == ["movie"]


async def test_body_over_256_kib_is_413_and_a_100_kib_body_is_accepted(client, keys, wire):
    await seed(wire)
    rev = await revision(client, keys["rw"])
    big = await client.patch(
        URL, content=b"x" * 262_145, headers={**write_headers(keys["rw"], rev), "Content-Type": "application/json"},
    )
    assert big.status_code == 413 and big.json()["detail"]["code"] == "invalid_parameter"
    assert await count_edits(wire) == 0
    medium = await client.patch(
        URL, json={"fields": {"summary": "s" * 10_000}, "reason": "r" * 200},
        headers=write_headers(keys["rw"], rev, "i-2"),
    )
    assert medium.status_code == 200


async def test_rate_limit_is_refused_before_any_processing(client, keys, wire, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "METADATA_WRITE_RATE_LIMIT_PER_MINUTE", 1)
    await seed(wire)
    assert (await patch(client, keys, {"summary": "one"}, idem="i-1")).status_code == 200
    resp = await patch(client, keys, {"summary": "two"}, idem="i-2")
    assert resp.status_code == 429
    assert resp.json()["detail"]["code"] == "rate_limited"
    assert int(resp.headers["Retry-After"]) == resp.json()["detail"]["retryAfterSeconds"] >= 1
    assert (await fetch(wire))[0].summary == "one" and await count_edits(wire) == 1
    # reads are counted on the general limiter only
    assert (await client.get(f"{BASE}/fields", headers=h(keys["rw"]))).status_code == 200


async def test_general_rate_limit_counts_reads_and_previews_per_key(client, keys, wire, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "METADATA_RATE_LIMIT_PER_MINUTE", 2)
    await seed(wire)
    assert (await client.get(f"{BASE}/fields", headers=h(keys["read"]))).status_code == 200
    assert (await client.get(f"{BASE}/fields", headers=h(keys["read"]))).status_code == 200
    assert (await client.get(f"{BASE}/fields", headers=h(keys["read"]))).status_code == 429
    assert (await client.get(f"{BASE}/fields", headers=h(keys["rw"]))).status_code == 200   # another key


async def test_a_busy_database_is_a_503_and_writes_nothing(client, keys, wire, monkeypatch):
    from app.db.immediate_tx import DatabaseBusy
    from app.services import metadata_service

    await seed(wire)
    rev = await revision(client, keys["rw"])

    def busy(*_a, **_k):
        raise DatabaseBusy("lock")

    monkeypatch.setattr(metadata_service, "immediate_write_session", busy)
    resp = await client.patch(URL, json={"fields": {"summary": "x"}}, headers=write_headers(keys["rw"], rev))
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "database_busy"
    assert resp.json()["detail"]["retryAfterSeconds"] == int(resp.headers["Retry-After"])
    assert (await fetch(wire))[0].summary == "old"


async def test_journal_row_holds_the_audit_fields_and_never_the_key(client, keys, wire):
    await seed(wire)
    resp = await client.patch(
        URL, json={"fields": {"summary": "new"}, "reason": "plexhubTMM: fix"},
        headers=write_headers(keys["rw"], await revision(client, keys["rw"]), "idem-9", **{"X-Request-ID": "task-7"}),
    )
    edit_id = resp.json()["editId"]
    async with wire() as db:
        row = (await db.execute(select(MetadataEdit).where(MetadataEdit.edit_id == edit_id))).scalar_one()
    assert (row.kind, row.server_id, row.rating_key, row.item_type) == ("patch", SERVER, "vod_1.mkv", "movie")
    assert (row.request_id, row.idempotency_key, row.reason) == ("task-7", "idem-9", "plexhubTMM: fix")
    assert row.key_id and row.key_id != keys["rw"] and len(row.body_sha256) == 64
    assert row.revision_before == resp.json()["revisionBefore"] and row.revision_after == resp.json()["revisionAfter"]
    assert keys["rw"] not in json.dumps([row.before_json, row.after_json, row.response_json])
    master = await client.patch(
        URL, json={"fields": {"summary": "m"}},
        headers=write_headers(keys["master"], resp.json()["revisionAfter"], "idem-10"),
    )
    async with wire() as db:
        mrow = (await db.execute(select(MetadataEdit).where(MetadataEdit.edit_id == master.json()["editId"]))).scalar_one()
    assert mrow.key_id == "master"
