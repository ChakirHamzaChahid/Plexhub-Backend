"""B2-07 — historique, édition, annulation et verrou : `history`, `edits/{id}`,
`edits/{id}/revert[/preview]`, `items/{s}/{r}/lock[/preview]` (contrat § 5.8–5.11, § 6.7, § 9 ;
ADR 0011 § D2.5–D2.6)."""
from __future__ import annotations

import pytest
from sqlalchemy import delete, update

from app.models.database import Media, MetadataEdit
from tests._metadata_support import BASE, SERVER, count_edits, fetch, h, seed, write_headers

URL = f"{BASE}/items/{SERVER}/vod_1.mkv"


async def rev(client, keys, rk="vod_1.mkv") -> str:
    return (await client.get(f"{BASE}/items/{SERVER}/{rk}", headers=h(keys["rw"]))).json()["revision"]


async def do_patch(client, keys, fields, *, idem, allow=False, rk="vod_1.mkv"):
    resp = await client.patch(
        f"{BASE}/items/{SERVER}/{rk}", json={"fields": fields, "allowRegroup": allow},
        headers=write_headers(keys["rw"], await rev(client, keys, rk), idem),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def do_lock(client, keys, locked, *, idem, rk="vod_1.mkv"):
    resp = await client.post(
        f"{BASE}/items/{SERVER}/{rk}/lock", json={"locked": locked},
        headers=write_headers(keys["rw"], await rev(client, keys, rk), idem),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def revert(client, keys, edit_id, *, idem, body=None, who="rw", revision=None, path="revert"):
    revision = revision or await rev(client, keys)
    return await client.post(
        f"{BASE}/edits/{edit_id}/{path}", json=body or {},
        headers=write_headers(keys[who], revision, idem),
    )


# ─── history / edit ─────────────────────────────────────────────────────────


async def test_history_is_newest_first_paginated_and_complete(client, keys, wire):
    await seed(wire)
    ids = [(await do_patch(client, keys, {"summary": f"v{n}"}, idem=f"i-{n}"))["editId"] for n in range(5)]
    seen, cursor = [], None
    while True:
        params = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        body = (await client.get(f"{URL}/history", params=params, headers=h(keys["read"]))).json()
        seen += [e["editId"] for e in body["edits"]]
        cursor = body["nextCursor"]
        if cursor is None:
            break
    assert seen == list(reversed(ids))
    first = (await client.get(f"{URL}/history", headers=h(keys["read"]))).json()["edits"][0]
    assert set(first) == {
        "editId", "kind", "atMs", "keyId", "requestId", "idempotencyKey", "reason", "item",
        "revisionBefore", "revisionAfter", "changedFields", "before", "after", "beforeVariants",
        "lockBefore", "lockAfter", "derivedBefore", "derivedAfter", "unificationIdBefore",
        "unificationIdAfter", "reverts", "revertedBy",
    }
    assert first["kind"] == "patch" and first["changedFields"] == ["summary"]
    assert first["before"] == {"summary": "v3"} and first["after"] == {"summary": "v4"}
    assert first["lockBefore"] == {"locked": True, "source": "plexhubtmm"}
    assert first["reverts"] is None and first["revertedBy"] is None
    assert [v["filter"] for v in first["beforeVariants"]] == ["all"]


async def test_history_params_and_unknown_item(client, keys, wire):
    await seed(wire)
    assert (await client.get(f"{URL}/history", params={"limit": 0}, headers=h(keys["read"]))).status_code == 422
    assert (await client.get(f"{URL}/history", params={"limit": 101}, headers=h(keys["read"]))).status_code == 422
    bad = await client.get(f"{URL}/history", params={"cursor": "%%"}, headers=h(keys["read"]))
    assert bad.status_code == 400
    empty = await client.get(f"{URL}/history", headers=h(keys["read"]))
    assert empty.status_code == 200 and empty.json()["edits"] == []          # known item, no edit yet
    unknown = await client.get(f"{BASE}/items/{SERVER}/ghost/history", headers=h(keys["read"]))
    assert unknown.status_code == 404 and unknown.json()["detail"]["code"] == "item_not_found"


async def test_history_stays_readable_after_the_sync_deleted_the_item(client, keys, wire):
    await seed(wire)
    await do_patch(client, keys, {"summary": "kept"}, idem="i-1")
    async with wire() as db:
        await db.execute(delete(Media))
        await db.commit()
    body = (await client.get(f"{URL}/history", headers=h(keys["read"]))).json()
    assert len(body["edits"]) == 1


async def test_get_edit_and_unknown_edit(client, keys, wire):
    await seed(wire)
    edit_id = (await do_patch(client, keys, {"summary": "x"}, idem="i-1"))["editId"]
    resp = await client.get(f"{BASE}/edits/{edit_id}", headers=h(keys["read"]))
    assert resp.status_code == 200 and resp.json()["edit"]["editId"] == edit_id
    assert resp.json()["schemaVersion"] == 1
    missing = await client.get(f"{BASE}/edits/NOPE", headers=h(keys["read"]))
    assert missing.status_code == 404 and missing.json()["detail"]["code"] == "edit_not_found"


async def test_edit_images_only_hold_modified_fields_with_partial_ids(client, keys, wire):
    await seed(wire)
    body = await do_patch(client, keys, {"ids": {"imdb": "tt0133093"}, "genres": ["Drame"]}, idem="i-1", allow=True)
    edit = (await client.get(f"{BASE}/edits/{body['editId']}", headers=h(keys["read"]))).json()["edit"]
    assert edit["changedFields"] == ["genres", "ids.imdb"]
    assert edit["before"] == {"genres": ["Action"], "ids": {"imdb": None}}
    assert edit["after"] == {"genres": ["Drame"], "ids": {"imdb": "tt0133093"}}
    assert edit["unificationIdBefore"] == "title_the_matrix_1999" and edit["unificationIdAfter"] == "imdb://tt0133093"
    assert edit["derivedBefore"]["unificationId"] == "title_the_matrix_1999"
    assert edit["beforeVariants"][0]["derived"]["historyGroupKey"] == "title_the_matrix_1999"


# ─── lock ───────────────────────────────────────────────────────────────────


async def test_lock_and_unlock_are_journaled_and_noop_when_same_state(client, keys, wire):
    await seed(wire, variants=(("all", "default"), ("cat2", "default")))
    locked = await do_lock(client, keys, True, idem="i-1")
    assert locked["kind"] == "lock" and locked["changedFields"] == [] and locked["editId"]
    assert locked["lockBefore"] == {"locked": False, "source": None}
    assert locked["lockAfter"] == {"locked": True, "source": "plexhubtmm"}
    assert [(r.match_locked, r.match_source) for r in await fetch(wire)] == [(True, "plexhubtmm")] * 2
    noop = await do_lock(client, keys, True, idem="i-2")
    assert noop["noop"] is True and noop["editId"] is None
    unlocked = await do_lock(client, keys, False, idem="i-3")
    assert unlocked["lockAfter"] == {"locked": False, "source": None}
    assert [(r.match_locked, r.match_source) for r in await fetch(wire)] == [(False, None)] * 2
    assert await count_edits(wire) == 2


async def test_unlocking_a_manual_row_clears_its_source_like_the_admin(client, keys, wire):
    await seed(wire, match_locked=True, match_source="manual")
    await do_lock(client, keys, False, idem="i-1")
    (row,) = await fetch(wire)
    assert (row.match_locked, row.match_source) == (False, None)


async def test_lock_preview_runs_under_read_and_writes_nothing(client, keys, wire):
    await seed(wire)
    resp = await client.post(f"{URL}/lock/preview", json={"locked": True}, headers=h(keys["read"]))
    body = resp.json()
    assert resp.status_code == 200 and body["dryRun"] is True and body["editId"] is None
    assert body["lockAfter"]["locked"] is True and (await fetch(wire))[0].match_locked is False
    forbidden = await client.post(f"{URL}/lock", json={"locked": True},
                                  headers=write_headers(keys["read"], await rev(client, keys)))
    assert forbidden.status_code == 403
    assert (await client.post(f"{URL}/lock", json={}, headers=write_headers(keys["rw"], "r"))).status_code == 422


# ─── revert ─────────────────────────────────────────────────────────────────


async def test_revert_restores_fields_derived_and_the_free_state_of_the_lock(client, keys, wire):
    await seed(wire)
    original = await fetch(wire)
    edit = await do_patch(client, keys, {"summary": "new", "ids": {"imdb": "tt0133093"}}, idem="i-1", allow=True)
    resp = await revert(client, keys, edit["editId"], idem="i-2", body={"allowRegroup": True, "reason": "oops"})
    body = resp.json()
    assert resp.status_code == 200, resp.text
    assert body["kind"] == "revert" and body["editId"] and body["noop"] is False
    (row,) = await fetch(wire)
    assert (row.summary, row.imdb_id) == ("old", None)
    assert (row.unification_id, row.history_group_key) == ("title_the_matrix_1999", "title_the_matrix_1999")
    assert (row.match_locked, row.match_source) == (False, None)                   # unlocked as before
    assert row.display_rating == original[0].display_rating
    assert body["revisionAfter"] == edit["revisionBefore"] == await rev(client, keys)
    assert body["changedFields"] == ["ids.imdb", "summary"]
    assert body["regroup"]["changes"] is True
    stored = (await client.get(f"{BASE}/edits/{body['editId']}", headers=h(keys["read"]))).json()["edit"]
    assert stored["reverts"] == edit["editId"] and stored["reason"] == "oops"
    assert (await client.get(f"{BASE}/edits/{edit['editId']}", headers=h(keys["read"]))).json()["edit"][
        "revertedBy"] == body["editId"]


async def test_revert_restores_divergent_variants_exactly(client, keys, wire):
    await seed(wire)
    await seed(wire, variants=(("cat2", "default"),), summary="B", genres="Comedie")
    edit = await do_patch(client, keys, {"summary": "Z", "genres": ["Drame"]}, idem="i-1")
    assert (await revert(client, keys, edit["editId"], idem="i-2")).status_code == 200
    rows = {r.filter: r for r in await fetch(wire)}
    assert (rows["all"].summary, rows["all"].genres) == ("old", "Action")
    assert (rows["cat2"].summary, rows["cat2"].genres) == ("B", "Comedie")
    assert not any(r.match_locked for r in rows.values())


async def test_revert_keeps_a_manual_lock_it_did_not_set(client, keys, wire):
    await seed(wire, match_locked=True, match_source="manual")
    edit = await do_patch(client, keys, {"summary": "new"}, idem="i-1")
    assert (await revert(client, keys, edit["editId"], idem="i-2")).status_code == 200
    (row,) = await fetch(wire)
    assert (row.summary, row.match_locked, row.match_source) == ("old", True, "manual")


async def test_revert_preview_equals_revert_and_writes_nothing(client, keys, wire):
    await seed(wire)
    edit = await do_patch(client, keys, {"summary": "new"}, idem="i-1")
    before_rows = [(r.summary, r.match_locked) for r in await fetch(wire)]
    pre = await client.post(f"{BASE}/edits/{edit['editId']}/revert/preview", json={}, headers=h(keys["read"]))
    assert pre.status_code == 200 and pre.json()["dryRun"] is True and pre.json()["kind"] == "revert"
    assert [(r.summary, r.match_locked) for r in await fetch(wire)] == before_rows
    assert await count_edits(wire) == 1
    real = await revert(client, keys, edit["editId"], idem="i-2")
    strip = lambda d: {k: v for k, v in d.items() if k not in ("dryRun", "editId", "atMs", "replayed")}  # noqa: E731
    assert strip(real.json()) == strip(pre.json())


async def test_revert_conflicts_when_a_field_changed_since(client, keys, wire):
    await seed(wire)
    edit = await do_patch(client, keys, {"summary": "mine", "genres": ["Drame"]}, idem="i-1")
    async with wire() as db:
        await db.execute(update(Media).values(summary="changed by sync"))
        await db.commit()
    resp = await revert(client, keys, edit["editId"], idem="i-2")
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "revert_conflict" and resp.json()["detail"]["fields"] == ["summary"]
    assert (await fetch(wire))[0].genres == "Drame" and await count_edits(wire) == 1       # nothing partial
    pre = await client.post(f"{BASE}/edits/{edit['editId']}/revert/preview", json={}, headers=h(keys["read"]))
    assert pre.status_code == 409 and pre.json()["detail"]["fields"] == ["summary"]


async def test_revert_lock_rule_newer_edit_and_changed_lock(client, keys, wire):
    await seed(wire)
    e1 = await do_patch(client, keys, {"summary": "one"}, idem="i-1")
    await do_patch(client, keys, {"tagline": "two"}, idem="i-2")                 # newer, live edit
    resp = await revert(client, keys, e1["editId"], idem="i-3")
    assert resp.status_code == 409 and resp.json()["detail"]["fields"] == ["lock"]
    # lock changed by someone else
    await seed(wire, rating_key="b.mkv")
    e = await do_patch(client, keys, {"summary": "x"}, idem="i-4", rk="b.mkv")
    async with wire() as db:
        await db.execute(update(Media).where(Media.rating_key == "b.mkv").values(match_locked=False, match_source=None))
        await db.commit()
    resp = await client.post(
        f"{BASE}/edits/{e['editId']}/revert", json={},
        headers=write_headers(keys["rw"], await rev(client, keys, "b.mkv"), "i-5"),
    )
    assert resp.status_code == 409 and resp.json()["detail"]["fields"] == ["lock"]


async def test_double_revert_is_a_conflict_without_fields(client, keys, wire):
    await seed(wire)
    edit = await do_patch(client, keys, {"summary": "new"}, idem="i-1")
    assert (await revert(client, keys, edit["editId"], idem="i-2")).status_code == 200
    resp = await revert(client, keys, edit["editId"], idem="i-3")
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "revert_conflict" and resp.json()["detail"]["fields"] == []


async def test_a_revert_can_be_reverted_to_reestablish_the_edit(client, keys, wire):
    await seed(wire)
    edit = await do_patch(client, keys, {"summary": "new"}, idem="i-1")
    r1 = (await revert(client, keys, edit["editId"], idem="i-2")).json()
    r2 = await revert(client, keys, r1["editId"], idem="i-3")
    assert r2.status_code == 200, r2.text
    (row,) = await fetch(wire)
    assert (row.summary, row.match_locked, row.match_source) == ("new", True, "plexhubtmm")
    assert r2.json()["revisionAfter"] == edit["revisionAfter"]


async def test_stacked_edits_unwind_through_the_compensation_rule(client, keys, wire):
    await seed(wire)
    e1 = await do_patch(client, keys, {"summary": "one"}, idem="i-1")
    e2 = await do_patch(client, keys, {"tagline": "two"}, idem="i-2")
    assert (await revert(client, keys, e2["editId"], idem="i-3")).status_code == 200          # LIFO
    resp = await revert(client, keys, e1["editId"], idem="i-4")                   # E2 and its revert cancel out
    assert resp.status_code == 200, resp.text
    (row,) = await fetch(wire)
    assert (row.summary, row.tagline, row.match_locked) == ("old", None, False)


async def test_revert_regroup_follows_the_same_guard(client, keys, wire):
    await seed(wire)
    edit = await do_patch(client, keys, {"ids": {"tmdb": "603"}}, idem="i-1", allow=True)
    resp = await revert(client, keys, edit["editId"], idem="i-2")                  # would regroup back
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "regroup_not_allowed"
    assert (await revert(client, keys, edit["editId"], idem="i-3", body={"allowRegroup": True})).status_code == 200


async def test_revert_of_a_lock_edit_restores_the_state(client, keys, wire):
    await seed(wire)
    lock = await do_lock(client, keys, True, idem="i-1")
    assert (await revert(client, keys, lock["editId"], idem="i-2")).status_code == 200
    assert (await fetch(wire))[0].match_locked is False


async def test_revert_requires_write_scope_headers_and_a_current_revision(client, keys, wire):
    await seed(wire)
    edit = await do_patch(client, keys, {"summary": "new"}, idem="i-1")
    url = f"{BASE}/edits/{edit['editId']}/revert"
    assert (await revert(client, keys, edit["editId"], idem="i-2", who="read")).status_code == 403
    assert (await client.post(url, json={}, headers=h(keys["rw"], **{"Idempotency-Key": "k"}))).status_code == 428
    assert (await client.post(url, json={}, headers=h(keys["rw"], **{"If-Match": '"x"'}))).status_code == 428
    stale = await revert(client, keys, edit["editId"], idem="i-3", revision="stale-revision")
    assert stale.status_code == 412 and stale.json()["detail"]["currentRevision"] == await rev(client, keys)
    unknown = await revert(client, keys, "NOPE", idem="i-4", revision="r")
    assert unknown.status_code == 404 and unknown.json()["detail"]["code"] == "edit_not_found"
    assert (await fetch(wire))[0].summary == "new"


async def test_revert_replay_is_idempotent(client, keys, wire):
    await seed(wire)
    edit = await do_patch(client, keys, {"summary": "new"}, idem="i-1")
    first = await revert(client, keys, edit["editId"], idem="same")
    again = await revert(client, keys, edit["editId"], idem="same", revision="whatever")
    assert again.status_code == 200 and again.json()["replayed"] is True
    assert again.json()["editId"] == first.json()["editId"] and await count_edits(wire) == 2


async def test_journal_is_append_only_through_the_api(client, keys, wire):
    await seed(wire)
    edit = await do_patch(client, keys, {"summary": "new"}, idem="i-1")
    for method in ("put", "delete"):
        resp = await getattr(client, method)(f"{BASE}/edits/{edit['editId']}", headers=h(keys["rw"]))
        assert resp.status_code == 405
    async with wire() as db:
        from sqlalchemy import select

        row = (await db.execute(select(MetadataEdit))).scalar_one()
        assert row.reverted_by is None


@pytest.mark.parametrize("path", ["revert", "revert/preview"])
async def test_revert_body_is_optional(client, keys, wire, path):
    await seed(wire)
    edit = await do_patch(client, keys, {"summary": "new"}, idem="i-1")
    headers = write_headers(keys["rw"], await rev(client, keys), "i-2")
    resp = await client.post(f"{BASE}/edits/{edit['editId']}/{path}", headers=headers)
    assert resp.status_code == 200, resp.text
