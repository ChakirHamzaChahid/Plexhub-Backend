"""B2-09 — état de la dernière sauvegarde, calculé à la requête depuis les fichiers présents
(ADR 0012 § D2.3, contrat B2 § 5.12).

- `perform_backup` enregistre, avant le renommage final, un sidecar `<nom>.verified` (nom, taille,
  `mtime_ns`, résultat d'`integrity_check`) ; `run_backup` tient un fichier d'état de la dernière
  tentative. Ni l'un ni l'autre ne vit dans la base (une restauration ferait régresser l'état).
- `compute_status` ne relit JAMAIS la base ni ne relance `integrity_check` : il liste le dossier.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.scripts import backup_db
from app.services import backup_status_service as status_service

NOW = datetime(2026, 10, 18, 12, 0, 0, tzinfo=timezone.utc)
NOW_MS = int(NOW.timestamp() * 1000)


def make_db(path, rows: int = 3) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        conn.executemany("INSERT INTO t (v) VALUES (?)", [(f"v{i}",) for i in range(rows)])
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def env(tmp_path, monkeypatch):
    backups = tmp_path / "backups"
    backups.mkdir()
    live = tmp_path / "plexhub.db"
    make_db(live)
    monkeypatch.setattr(settings, "BACKUP_DIR", backups)
    monkeypatch.setattr(settings, "DB_PATH", live)
    monkeypatch.setattr(settings, "BACKUP_ENABLED", True)
    monkeypatch.setattr(settings, "BACKUP_HOUR", 4)
    monkeypatch.setattr(settings, "BACKUP_RETENTION_DAYS", 7)
    return backups


def fake_backup(backups, when: datetime, *, size: int = 2048, sidecar: bool = False):
    path = backups / f"plexhub-{when.strftime('%Y%m%d-%H%M%S-%f')}.db"
    path.write_bytes(b"x" * size)
    if sidecar:
        st = path.stat()
        status_service_sidecar(path, size=st.st_size, mtime_ns=st.st_mtime_ns)
    return path


def status_service_sidecar(path, *, size, mtime_ns, integrity="ok", name=None):
    sidecar = backup_db.verified_sidecar_path(path)
    sidecar.write_text(json.dumps({
        "name": name or path.name, "size": size, "mtime_ns": mtime_ns, "integrity": integrity,
        "verified_at": NOW_MS,
    }))


# ─── writing: sidecar and attempt state ─────────────────────────────────────


def test_perform_backup_records_the_verified_sidecar(env):
    target = env / "plexhub-20261018-040000-000001.db"
    backup_db.perform_backup(settings.DB_PATH, target)
    sidecar = backup_db.verified_sidecar_path(target)
    data = json.loads(sidecar.read_text())
    st = target.stat()
    assert data["name"] == target.name and data["size"] == st.st_size
    assert data["mtime_ns"] == st.st_mtime_ns and data["integrity"] == "ok"
    assert [p.name for p in env.iterdir() if p.name.endswith(".tmp")] == []


def test_no_sidecar_when_not_verified_or_not_a_backup_name(env, tmp_path):
    unverified = env / "plexhub-20261018-040000-000002.db"
    backup_db.perform_backup(settings.DB_PATH, unverified, verify=False)
    assert not backup_db.verified_sidecar_path(unverified).exists()
    restore_copy = tmp_path / "plexhub.pre-restore-20261018.db"      # restore_db's own copy
    backup_db.perform_backup(settings.DB_PATH, restore_copy)
    assert not backup_db.verified_sidecar_path(restore_copy).exists()


def test_a_failed_integrity_check_leaves_neither_file_nor_sidecar(env, monkeypatch):
    def bad(*_a, **_k):
        raise backup_db.BackupIntegrityError("corrupt")

    monkeypatch.setattr(backup_db, "check_integrity", bad)
    target = env / "plexhub-20261018-040000-000003.db"
    with pytest.raises(backup_db.BackupIntegrityError):
        backup_db.perform_backup(settings.DB_PATH, target)
    assert list(env.iterdir()) == []


def test_run_backup_records_the_attempt_state(env):
    backup_db.run_backup()
    state = json.loads((env / backup_db.STATE_FILE_NAME).read_text())
    assert state["outcome"] == "ok" and isinstance(state["at_ms"], int)


def test_run_backup_records_a_failed_attempt_then_reraises(env, monkeypatch):
    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(backup_db, "perform_backup", boom)
    with pytest.raises(OSError):
        backup_db.run_backup()
    state = json.loads((env / backup_db.STATE_FILE_NAME).read_text())
    assert state["outcome"] == "failed"


def test_the_state_file_is_not_a_backup_and_never_pruned(env):
    (env / backup_db.STATE_FILE_NAME).write_text("{}")
    old = datetime.now(timezone.utc) - timedelta(days=100)
    fake_backup(env, old, sidecar=True)
    backup_db.prune_old_backups(env, 7)
    assert (env / backup_db.STATE_FILE_NAME).exists()


def test_pruning_a_backup_removes_its_sidecar(env):
    old = fake_backup(env, datetime.now(timezone.utc) - timedelta(days=30), sidecar=True)
    fresh = fake_backup(env, datetime.now(timezone.utc), sidecar=True)
    backup_db.prune_old_backups(env, 7)
    assert not old.exists() and not backup_db.verified_sidecar_path(old).exists()
    assert fresh.exists() and backup_db.verified_sidecar_path(fresh).exists()


def test_orphan_sidecars_are_swept_only_when_old_and_unmatched(env):
    live = fake_backup(env, NOW, sidecar=True)
    orphan_old = env / "plexhub-20200101-000000-000000.db.verified"
    orphan_old.write_text("{}")
    os.utime(orphan_old, (time.time() - 7200, time.time() - 7200))
    orphan_young = env / "plexhub-20200102-000000-000000.db.verified"
    orphan_young.write_text("{}")
    backup_db.sweep_orphan_tmp(env)
    assert not orphan_old.exists() and orphan_young.exists()
    assert backup_db.verified_sidecar_path(live).exists()


# ─── reading: compute_status ────────────────────────────────────────────────


def compute(now_ms=NOW_MS):
    return status_service.compute_status(now_ms)


def test_no_backup_at_all(env):
    status = compute()
    assert status["lastSuccessAt"] is None and status["ageSeconds"] is None
    assert status["integrityCheck"] == "not_run"
    assert status["lastAttemptAt"] is None and status["lastAttemptOutcome"] is None
    assert status["enabled"] is True and status["retentionDays"] == 7
    assert status["serverNowMs"] == NOW_MS


def test_a_missing_directory_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "BACKUP_DIR", tmp_path / "nowhere")
    assert compute()["lastSuccessAt"] is None


def test_a_verified_backup(env):
    when = NOW - timedelta(hours=3)
    fake_backup(env, when, sidecar=True)
    status = compute()
    assert status["lastSuccessAt"] == int(when.timestamp() * 1000)
    assert status["ageSeconds"] == 3 * 3600
    assert status["integrityCheck"] == "ok"


def test_a_backup_made_before_the_sidecars_existed_is_unknown(env):
    fake_backup(env, NOW - timedelta(hours=1), sidecar=False)
    status = compute()
    assert status["lastSuccessAt"] is not None and status["integrityCheck"] == "unknown"


def test_a_replaced_or_edited_file_no_longer_matches_its_sidecar(env):
    path = fake_backup(env, NOW - timedelta(hours=1), sidecar=True)
    path.write_bytes(b"y" * 4096)                                # swapped for another file
    assert compute()["integrityCheck"] == "unknown"
    path2 = fake_backup(env, NOW - timedelta(minutes=30), sidecar=False)
    st = path2.stat()
    status_service_sidecar(path2, size=st.st_size, mtime_ns=st.st_mtime_ns, name="plexhub-other.db")
    assert compute()["integrityCheck"] == "unknown"             # sidecar names another file
    path3 = fake_backup(env, NOW - timedelta(minutes=10), sidecar=False)
    st = path3.stat()
    status_service_sidecar(path3, size=st.st_size, mtime_ns=st.st_mtime_ns, integrity="failed")
    assert compute()["integrityCheck"] == "unknown"             # only an explicit "ok" counts


def test_a_deleted_backup_never_counts_even_with_its_sidecar_left_behind(env):
    path = fake_backup(env, NOW - timedelta(hours=1), sidecar=True)
    path.unlink()
    status = compute()
    assert status["lastSuccessAt"] is None and status["integrityCheck"] == "not_run"


def test_the_newest_present_backup_wins_and_empty_or_foreign_files_are_ignored(env):
    fake_backup(env, NOW - timedelta(hours=30), sidecar=True)
    newest = fake_backup(env, NOW - timedelta(hours=2), sidecar=True)
    fake_backup(env, NOW - timedelta(minutes=1), size=0)                       # empty => ignored
    (env / "plexhub-notadate.db").write_bytes(b"x" * 10)
    (env / "plexhub-20261018-110000-000000.db.tmp").write_bytes(b"x" * 10)     # in-flight copy
    (env / "other.db").write_bytes(b"x" * 10)
    status = compute()
    assert status["lastSuccessAt"] == int((NOW - timedelta(hours=2)).timestamp() * 1000)
    assert newest.exists() and status["integrityCheck"] == "ok"


def test_the_legacy_second_granularity_name_is_understood(env):
    (env / "plexhub-20261018-100000.db").write_bytes(b"x" * 10)
    assert compute()["lastSuccessAt"] == int(datetime(2026, 10, 18, 10, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)


def test_attempt_state_is_reported_and_tolerates_garbage(env):
    (env / backup_db.STATE_FILE_NAME).write_text(json.dumps({"at_ms": NOW_MS - 5000, "outcome": "failed"}))
    status = compute()
    assert status["lastAttemptAt"] == NOW_MS - 5000 and status["lastAttemptOutcome"] == "failed"
    for garbage in ("not json", "[]", json.dumps({"at_ms": "x", "outcome": 5}), json.dumps({"outcome": "weird"})):
        (env / backup_db.STATE_FILE_NAME).write_text(garbage)
        status = compute()
        assert status["lastAttemptAt"] is None and status["lastAttemptOutcome"] is None


def test_schedule_fields(env, monkeypatch):
    status = compute()
    assert status["scheduleHourLocal"] == 4
    assert isinstance(status["scheduleTimezone"], str) and status["scheduleTimezone"]
    monkeypatch.setattr(settings, "BACKUP_ENABLED", False)
    disabled = compute()
    assert disabled["enabled"] is False and disabled["scheduleHourLocal"] is None


def test_the_timezone_falls_back_to_an_offset_when_tzlocal_fails(monkeypatch):
    import tzlocal

    def boom():
        raise RuntimeError("no tz")

    monkeypatch.setattr(tzlocal, "get_localzone_name", boom)
    assert status_service.timezone_label().startswith(("UTC+", "UTC-", "UTC"))


def test_the_response_never_contains_a_path_or_a_backup_file_name(env, tmp_path):
    fake_backup(env, NOW - timedelta(hours=1), sidecar=True)
    text = json.dumps(compute())
    assert str(tmp_path) not in text and "plexhub-" not in text and ".db" not in text
    assert str(env) not in text


def test_compute_status_does_not_run_integrity_check(env, monkeypatch):
    fake_backup(env, NOW - timedelta(hours=1), sidecar=True)

    def forbidden(*_a, **_k):
        raise AssertionError("integrity_check must never run on a request")

    monkeypatch.setattr(backup_db, "check_integrity", forbidden)
    assert compute()["integrityCheck"] == "ok"


# ─── --verify-latest (pre-B2 backup) ────────────────────────────────────────


def test_verify_latest_blesses_the_newest_backup_after_a_real_check(env):
    make_db(env / "plexhub-20261018-030000-000000.db")
    assert backup_db.verify_latest(env) == "plexhub-20261018-030000-000000.db"
    assert backup_db.verified_sidecar_path(env / "plexhub-20261018-030000-000000.db").exists()


def test_verify_latest_refuses_a_corrupt_file(env):
    bad = env / "plexhub-20261018-030000-000001.db"
    bad.write_bytes(b"this is not a sqlite database" * 100)
    with pytest.raises(backup_db.BackupIntegrityError):
        backup_db.verify_latest(env)
    assert not backup_db.verified_sidecar_path(bad).exists()


def test_verify_latest_without_any_backup(env):
    assert backup_db.verify_latest(env) is None


# ─── the route ──────────────────────────────────────────────────────────────


async def test_route_requires_the_read_scope_and_renders_camel_case(api_client, db_factory, monkeypatch, env):
    from app.db import database as db_module
    from app.services import api_key_service
    from tests._metadata_support import BASE, MASTER, h, make_key

    monkeypatch.setattr(db_module, "async_session_factory", db_factory)
    monkeypatch.setattr(api_key_service, "async_session_factory", db_factory)
    monkeypatch.setattr(settings, "AI_API_KEY", MASTER)
    read = await make_key(db_factory, scopes="media.read")
    write_only = await make_key(db_factory, scopes="media.write")
    fake_backup(env, datetime.now(timezone.utc) - timedelta(hours=2), sidecar=True)

    assert (await api_client.get(f"{BASE}/backup/status")).status_code == 401
    assert (await api_client.get(f"{BASE}/backup/status", headers=h(write_only))).status_code == 403
    resp = await api_client.get(f"{BASE}/backup/status", headers=h(read))
    body = resp.json()
    assert resp.status_code == 200 and body["schemaVersion"] == 1
    assert set(body) == {
        "schemaVersion", "enabled", "lastSuccessAt", "ageSeconds", "integrityCheck", "lastAttemptAt",
        "lastAttemptOutcome", "retentionDays", "scheduleHourLocal", "scheduleTimezone", "serverNowMs",
    }
    assert body["integrityCheck"] == "ok" and 7000 <= body["ageSeconds"] <= 7300
    assert str(env) not in resp.text
