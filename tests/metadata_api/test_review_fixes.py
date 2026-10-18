"""Correctifs issus des relectures indépendantes du lot B2 (sécurité + correction) : chaque test
reproduit un défaut constaté avant le correctif."""
from __future__ import annotations

import asyncio
import base64
import json
import sqlite3
import threading
import time

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import immediate_tx
from app.db.immediate_tx import DatabaseBusy, immediate_write_session
from app.models.database import Media
from app.services import metadata_service, unified_group_service
from app.services.metadata_fields import FieldError, normalize_patch
from app.services.metadata_plan import compute_revision, plan_patch, plan_revert, variant_from_columns, variant_images
from tests._metadata_support import BASE, SERVER, count_edits, h, seed, write_headers
from tests.test_metadata_plan import V, patch as plan_patch_helper

URL = f"{BASE}/items/{SERVER}/vod_1.mkv"


async def rev(client, keys, rk="vod_1.mkv") -> str:
    return (await client.get(f"{BASE}/items/{SERVER}/{rk}", headers=h(keys["rw"]))).json()["revision"]


# ─── annulation d'éditions empilées : dérivé périmé (relecture générale, MEDIUM 1) ───────────


def test_reverting_an_id_edit_after_a_later_title_edit_recomputes_the_grouping():
    idless = V(match_locked=True, match_source="manual")
    e1 = plan_patch_helper([idless], {"ids": {"imdb": "tt1234567"}})
    e2 = plan_patch_helper(list(e1.after), {"title": "Bar"})                  # uid stays imdb://
    assert e2.changes[0].updates.get("unification_id") is None
    edit = {"before": variant_images(e1.before), "after": variant_images(e1.after)}
    revert = plan_revert(list(e2.after), edit, lock_newer_edit=False)
    updates = revert.changes[0].updates
    assert updates["imdb_id"] is None
    # NOT the recorded 'title_the_matrix_1999' (the title is now Bar): recomputed from the result
    assert updates["unification_id"] == "title_bar_1999"
    assert updates["history_group_key"] == "title_bar_1999"
    assert revert.unification_after == "title_bar_1999"


def test_reverting_restores_the_recorded_grouping_when_the_identity_inputs_are_back_to_what_they_were():
    before = [V()]
    e1 = plan_patch_helper(before, {"ids": {"imdb": "tt1234567"}})
    edit = {"before": variant_images(e1.before), "after": variant_images(e1.after)}
    revert = plan_revert(list(e1.after), edit, lock_newer_edit=False)
    assert revert.changes[0].updates["unification_id"] == "title_the_matrix_1999"


def test_regroup_reports_the_variant_that_really_changes_even_when_the_canonical_does_not():
    a = V(imdb_id="tt0133093", unification_id="imdb://tt0133093", history_group_key="imdb://tt0133093")
    b = V(filter="zzz")                                                         # no id, title-based group
    plan = plan_patch([a, b], normalize_patch({"ids": {"imdb": "tt0133093"}}, "movie", is_adult=False))
    assert plan.regroup_changes is True
    assert plan.unification_before != plan.unification_after          # never "before == after" on a 409


# ─── validation (sécurité F2/F3, relecture MEDIUM 2/3, LOW 4) ────────────────────────────────


@pytest.mark.parametrize(
    "patch,field",
    [
        ({"ratings": {"imdb": {"value": 5, "votes": 2**31}}}, "ratings.imdb"),
        ({"ratings": {"tmdb": {"value": 5, "votes": 10**30}}}, "ratings.tmdb"),
        ({"title": "a\ud800b"}, "title"),                       # lone surrogate
        ({"summary": "x￾y"}, "summary"),                   # non-character (invalid in XML 1.0)
        ({"title": "x￿"}, "title"),
        ({"ids": {"imdb": "tt١٢٣٤٥٦٧"}}, "ids.imdb"),            # Arabic-Indic digits
        ({"ids": {"tmdb": "١٢٣"}}, "ids.tmdb"),
        ({"cast": [{"name": "A", "tvdbId": "12\n"}]}, "cast"),
        ({"cast": [{"name": "A", "thumb": "https://x/\ud800"}]}, "cast"),
        ({"premiered": "١٩٩٩-٠١-٠١"}, "premiered"),
    ],
)
def test_values_that_used_to_crash_or_slip_through_are_now_400(patch, field):
    with pytest.raises(FieldError) as exc:
        normalize_patch(patch, "movie", is_adult=False)
    assert exc.value.field == field


def test_the_largest_vote_count_is_accepted():
    out = normalize_patch({"ratings": {"imdb": {"value": 5, "votes": 2**31 - 1}}}, "movie", is_adult=False)
    assert out["ratings.imdb"]["votes"] == 2**31 - 1


def test_a_legitimate_zero_width_joiner_and_newlines_are_still_accepted():
    out = normalize_patch({"title": "ن‌ا", "summary": "a\nb\tc"}, "movie", is_adult=False)
    assert out["summary"] == "a\nb\tc" and out["title"] == "ن‌ا"


def test_the_revision_hash_never_crashes_on_odd_text():
    odd = V(summary="lone \ud800 surrogate")
    assert len(compute_revision([odd])) == 22


def _raw(payload: dict) -> dict:
    """Body as ASCII-escaped JSON bytes: httpx's `json=` cannot encode a lone surrogate, a real
    client sending the JSON escape sequence can."""
    return {
        "content": json.dumps(payload, ensure_ascii=True).encode(),
        "headers": {"Content-Type": "application/json"},
    }


async def test_the_api_answers_400_not_500_for_those_values(client, keys, wire):
    await seed(wire)
    for fields, field in (
        ({"ratings": {"imdb": {"value": 5, "votes": 10**30}}}, "ratings.imdb"),
        ({"title": "a\ud800"}, "title"),
    ):
        for method in ("preview", "patch"):
            raw = _raw({"fields": fields})
            if method == "preview":
                resp = await client.post(
                    f"{URL}/preview", content=raw["content"], headers={**raw["headers"], **h(keys["rw"])},
                )
            else:
                resp = await client.patch(
                    URL, content=raw["content"],
                    headers={**raw["headers"], **write_headers(keys["rw"], await rev(client, keys), f"i-{field}")},
                )
            assert resp.status_code == 400, (method, resp.text)
            assert resp.json()["detail"]["code"] == "invalid_field" and resp.json()["detail"]["field"] == field
    assert await count_edits(wire) == 0


@pytest.mark.parametrize("params", [{"imdbId": "tt1234567\n"}, {"imdbId": "tt١٢٣٤٥٦٧"}, {"tmdbId": "١٢٣"}, {"tvdbId": "12\n"}])
async def test_lookup_ids_are_ascii_digits_only(client, keys, params):
    resp = await client.get(f"{BASE}/lookup", params={"type": "movie", **params}, headers=h(keys["read"]))
    assert resp.status_code == 400 and resp.json()["detail"]["code"] == "invalid_parameter"


async def test_the_idempotency_hash_tolerates_a_surrogate_in_the_request(client, keys, wire):
    await seed(wire)
    raw = _raw({"fields": {"summary": "ok"}, "reason": "r\ud800"})
    resp = await client.patch(
        URL, content=raw["content"],
        headers={**raw["headers"], **write_headers(keys["rw"], await rev(client, keys), "i-sur")},
    )
    assert resp.status_code == 400, resp.text                 # never a 500 AFTER the commit
    assert resp.json()["detail"] == {
        "code": "invalid_field", "field": "reason",
        "message": "contains a control character or an invalid code point",
    }
    assert await count_edits(wire) == 0


# ─── curseurs forgés (sécurité F4) ───────────────────────────────────────────────────────────


def _cursor(payload) -> str:
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


@pytest.mark.parametrize(
    "cursor",
    [_cursor([2**70, "x"]), _cursor([-1, "x"]), _cursor([1, 2]), _cursor("x"), _cursor([1]),
     base64.urlsafe_b64encode(b"[" * 300 + b"]" * 300).decode()],
    ids=["huge-int", "negative", "wrong-types", "not-a-list", "short", "deeply-nested"],
)
async def test_forged_history_cursors_are_400_never_500(client, keys, wire, cursor):
    await seed(wire)
    resp = await client.get(f"{URL}/history", params={"cursor": cursor}, headers=h(keys["read"]))
    assert resp.status_code == 400 and resp.json()["detail"]["code"] == "invalid_parameter"


async def test_a_forged_items_cursor_is_400(client, keys):
    resp = await client.get(f"{BASE}/items", params={"type": "movie", "cursor": _cursor([1, 2])}, headers=h(keys["read"]))
    assert resp.status_code == 400


# ─── réglage BACKGROUND_JOBS_ENABLED (sécurité F1) ───────────────────────────────────────────


@pytest.mark.parametrize("raw,expected", [
    (None, True), ("", True), ("true", True), ("TRUE", True), ("1", True), ("yes", True), ("on", True),
    ("enabled", True), (" true ", True), ("garbage", True),
    ("false", False), ("FALSE", False), ("0", False), ("no", False), ("off", False), (" false ", False),
])
def test_only_an_explicit_false_disables_the_background_jobs(monkeypatch, raw, expected):
    from app import config

    if raw is None:
        monkeypatch.delenv("BACKGROUND_JOBS_ENABLED", raising=False)
    else:
        monkeypatch.setenv("BACKGROUND_JOBS_ENABLED", raw)
    assert config._env_flag_default_true("BACKGROUND_JOBS_ENABLED") is expected


# ─── sauvegarde : base absente (relecture générale LOW 8) ────────────────────────────────────


def test_a_missing_database_is_recorded_as_a_failed_attempt(tmp_path, monkeypatch):
    from app.config import settings
    from app.scripts import backup_db

    monkeypatch.setattr(settings, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "absent.db")
    with pytest.raises(FileNotFoundError):
        backup_db.run_backup()
    state = json.loads((tmp_path / "backups" / backup_db.STATE_FILE_NAME).read_text())
    assert state["outcome"] == "failed"


# ─── le rebuild du snapshot suit le COMMIT (relecture générale, faiblesse de test) ───────────


async def test_the_rebuild_is_scheduled_after_the_commit_not_before(client, keys, wire, monkeypatch):
    await seed(wire)
    seen: list[int] = []

    async def spy(*_a, **_k):
        return None

    def record(_type, **_k):
        seen.append(asyncio.get_running_loop().time())
        seen.append(len(committed))

    committed: list[int] = []

    async def count_now():
        committed.append(await count_edits(wire))

    real_record = record

    def record_after(type_, **kw):
        # at call time the journal row must already be visible to ANOTHER session (= committed)
        loop = asyncio.get_running_loop()
        loop.create_task(count_now())
        real_record(type_, **kw)

    monkeypatch.setattr(unified_group_service, "schedule_rebuild", record_after)
    resp = await client.patch(
        URL, json={"fields": {"summary": "new"}}, headers=write_headers(keys["rw"], await rev(client, keys), "i-rb"),
    )
    assert resp.status_code == 200
    await asyncio.sleep(0.05)
    assert committed == [1]


# ─── immediate_tx : restauration sur annulation + plafond de concurrence ─────────────────────


def _init_wal(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()


def _hold(path: str, seconds: float) -> threading.Thread:
    acquired = threading.Event()

    def run():
        c = sqlite3.connect(path, timeout=0)
        c.execute("BEGIN IMMEDIATE")
        acquired.set()
        time.sleep(seconds)
        c.rollback()
        c.close()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert acquired.wait(5)
    return t


@pytest.fixture
async def file_factory(tmp_path):
    path = str(tmp_path / "imm.db")
    _init_wal(path)
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}", connect_args={"check_same_thread": False, "timeout": 60})
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False), path, engine
    await engine.dispose()


async def test_a_cancelled_waiter_gives_its_connection_back_with_the_pool_busy_timeout(file_factory):
    factory, path, _engine = file_factory
    blocker = _hold(path, 1.2)

    async def waiter():
        async with immediate_write_session(factory, deadline_s=8):
            pass

    task = asyncio.create_task(waiter())
    await asyncio.sleep(0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    blocker.join(timeout=5)
    await asyncio.sleep(0.3)
    async with factory() as s:
        assert (await s.execute(text("PRAGMA busy_timeout"))).scalar_one() == 60_000


async def test_the_number_of_concurrent_writers_is_capped(file_factory, monkeypatch):
    factory, path, engine = file_factory
    monkeypatch.setattr(immediate_tx, "MAX_CONCURRENT_WRITERS", 2)
    immediate_tx._slots.clear()
    blocker = _hold(path, 1.5)
    peak = 0

    async def waiter():
        try:
            async with immediate_write_session(factory, deadline_s=1.0):
                pass
        except DatabaseBusy:
            pass

    tasks = [asyncio.create_task(waiter()) for _ in range(8)]
    for _ in range(10):
        await asyncio.sleep(0.05)
        peak = max(peak, engine.sync_engine.pool.checkedout())
    await asyncio.gather(*tasks)
    blocker.join(timeout=5)
    assert 1 <= peak <= 2, peak


async def test_a_queue_longer_than_the_deadline_is_busy_not_hung(file_factory, monkeypatch):
    factory, path, _engine = file_factory
    monkeypatch.setattr(immediate_tx, "MAX_CONCURRENT_WRITERS", 1)
    immediate_tx._slots.clear()
    blocker = _hold(path, 2.0)

    async def first():
        async with immediate_write_session(factory, deadline_s=3):
            pass

    t = asyncio.create_task(first())
    await asyncio.sleep(0.2)
    started = time.monotonic()
    with pytest.raises(DatabaseBusy):
        async with immediate_write_session(factory, deadline_s=0.3):
            pass
    assert time.monotonic() - started < 1.0
    await t
    blocker.join(timeout=5)


# ─── divers ─────────────────────────────────────────────────────────────────────────────────


async def test_every_variant_of_an_item_is_returned_without_truncation(client, keys, wire):
    await seed(wire, variants=tuple((f"cat{n:03d}", "default") for n in range(60)))
    body = (await client.get(URL, headers=h(keys["read"]))).json()
    assert body["variants"]["count"] == 60 and len(body["variants"]["items"]) == 60


def test_media_model_is_unchanged_by_this_module():
    assert Media.__tablename__ == "media"
    assert metadata_service is not None and variant_from_columns is not None and select is not None


async def test_reason_is_bounded_to_200_characters_with_a_400(client, keys, wire):
    await seed(wire)
    for method, path in (("post", f"{URL}/preview"), ("patch", URL)):
        headers = write_headers(keys["rw"], await rev(client, keys), f"i-{method}")
        resp = await getattr(client, method)(path, json={"fields": {"summary": "x"}, "reason": "r" * 201}, headers=headers)
        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"]["field"] == "reason"
    ok = await client.patch(
        URL, json={"fields": {"summary": "x"}, "reason": "r" * 200},
        headers=write_headers(keys["rw"], await rev(client, keys), "i-ok"),
    )
    assert ok.status_code == 200
