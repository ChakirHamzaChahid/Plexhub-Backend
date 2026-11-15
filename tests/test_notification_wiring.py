"""Tests for critical operational notification wiring (T8.2 - Lot 8).

Verifies:
1. Provider outage wiring (app/services/account_outage_service.py):
   - Notification sent when outage strikes reach threshold (outage confirmed).
   - Strikes below threshold do NOT trigger notifications.
   - Recovery notification sent on record_clear ONLY if the account was previously in outage.
   - Healthy account clear produces ZERO notifications (anti-spam).
   - Zero credentials (username, password, stream URLs) in payloads.
2. Download batch completion wiring (app/workers/download_worker.py):
   - Completed batch sends event="download", severity="info".
   - Failed batch sends event="download", severity="error".
   - Partial batch sends event="download", severity="warning".
   - In-progress batch (some jobs queued/running) does NOT send notifications.
   - Standalone job without batch_id does NOT trigger batch notifications.
3. Backup failure wiring (app/scripts/backup_db.py):
   - Missing DB triggers event="backup", severity="error", reason="db_not_found".
   - Corrupt integrity_check in run_backup triggers event="backup", severity="error", reason="integrity_check_failed".
   - Corrupt integrity_check in verify_latest triggers event="backup", severity="error".
   - Successful backup does NOT trigger failure notifications.
"""
from __future__ import annotations

import asyncio

import pytest

from app.config import settings
from app.models.database import DownloadBatch, DownloadJob, XtreamAccount
from app.scripts import backup_db
from app.services import account_outage_service as outage_service
from app.services import notification_service
from app.utils import ssrf
from app.utils.time import now_ms
from app.workers import download_worker


@pytest.fixture(autouse=True)
def _reset_notification_state():
    """Ensure clean cooldown cache and closed HTTP client between tests."""
    notification_service.clear_cooldown_cache()
    ssrf.clear_cache()
    outage_service.invalidate_mask_cache()
    yield
    notification_service.clear_cooldown_cache()
    ssrf.clear_cache()
    outage_service.invalidate_mask_cache()


@pytest.fixture
def notify_config(monkeypatch):
    """Enable notifications pointing to a test webhook URL."""
    monkeypatch.setattr(settings, "NOTIFY_ENABLED", True)
    monkeypatch.setattr(settings, "NOTIFY_WEBHOOK_URL", "https://notify.example.com/webhook")
    monkeypatch.setattr(settings, "NOTIFY_EVENTS", "*")
    monkeypatch.setattr(settings, "NOTIFY_COOLDOWN_SECONDS", 300)

    # Fake DNS to pass SSRF
    def _fake_resolve(host: str) -> bool:
        return host == "notify.example.com"

    monkeypatch.setattr(ssrf, "_resolve_is_public", _fake_resolve)


# ─── 1. Provider Outage Notifications ─────────────────────────────────────────


async def test_outage_trip_notifies_only_at_threshold(db_factory, monkeypatch, notify_config):
    """Notification is fired only when strikes reach ACCOUNT_OUTAGE_STRIKES."""
    monkeypatch.setattr(settings, "ACCOUNT_OUTAGE_STRIKES", 3)

    # Seed an account
    async with db_factory() as session:
        acc = XtreamAccount(
            id="acc_test1",
            label="Fournisseur Test",
            base_url="http://provider.test",
            username="secret_user",
            password="secret_password",
            outage_strikes=0,
            is_active=True,
        )
        session.add(acc)
        await session.commit()

    captured_notifications: list[dict] = []

    async def _capture_send(event, title, message, *, severity="info", key=None, details=None):
        captured_notifications.append({
            "event": event,
            "title": title,
            "message": message,
            "severity": severity,
            "key": key,
            "details": details,
        })
        return True

    monkeypatch.setattr(notification_service, "send_notification", _capture_send)

    # Strike 1: strikes < threshold -> no notification
    strikes = await outage_service.record_trip("acc_test1", session_factory=db_factory)
    assert strikes == 1
    # Allow background task to execute if any
    await asyncio.sleep(0.01)
    assert len(captured_notifications) == 0

    # Strike 2: strikes < threshold -> no notification
    strikes = await outage_service.record_trip("acc_test1", session_factory=db_factory)
    assert strikes == 2
    await asyncio.sleep(0.01)
    assert len(captured_notifications) == 0

    # Strike 3: threshold reached -> notification dispatched!
    strikes = await outage_service.record_trip("acc_test1", session_factory=db_factory)
    assert strikes == 3
    await asyncio.sleep(0.01)
    assert len(captured_notifications) == 1

    notif = captured_notifications[0]
    assert notif["event"] == "outage"
    assert notif["severity"] == "error"
    assert "Fournisseur Test" in notif["title"]
    assert notif["details"]["account_id"] == "acc_test1"
    assert notif["details"]["strikes"] == 3
    assert notif["details"]["status"] == "outage"

    # Invariant: NEVER leak credentials
    full_dump = str(notif)
    assert "secret_user" not in full_dump
    assert "secret_password" not in full_dump
    assert "provider.test" not in full_dump


async def test_outage_clear_notifies_only_when_previously_in_outage(db_factory, monkeypatch, notify_config):
    """Recovery notification is sent only if the account was confirmed in outage."""
    monkeypatch.setattr(settings, "ACCOUNT_OUTAGE_STRIKES", 3)

    captured_notifications: list[dict] = []

    async def _capture_send(event, title, message, *, severity="info", key=None, details=None):
        captured_notifications.append({
            "event": event,
            "title": title,
            "message": message,
            "severity": severity,
            "key": key,
            "details": details,
        })
        return True

    monkeypatch.setattr(notification_service, "send_notification", _capture_send)

    # Account 1: Healthy account (strikes=0) -> clearing it sends ZERO notifications (no spam)
    async with db_factory() as session:
        acc_healthy = XtreamAccount(
            id="acc_healthy",
            label="Fournisseur Sain",
            base_url="http://healthy.test",
            username="u1",
            password="p1",
            outage_strikes=0,
            is_active=True,
        )
        session.add(acc_healthy)
        await session.commit()

    await outage_service.record_clear("acc_healthy", session_factory=db_factory)
    await asyncio.sleep(0.01)
    assert len(captured_notifications) == 0

    # Account 2: Flaky account (strikes=1 < threshold) -> clearing does NOT notify
    async with db_factory() as session:
        acc_flaky = XtreamAccount(
            id="acc_flaky",
            label="Fournisseur Flaky",
            base_url="http://flaky.test",
            username="u2",
            password="p2",
            outage_strikes=1,
            is_active=True,
        )
        session.add(acc_flaky)
        await session.commit()

    await outage_service.record_clear("acc_flaky", session_factory=db_factory)
    await asyncio.sleep(0.01)
    assert len(captured_notifications) == 0

    # Account 3: Outage account (strikes=3 >= threshold) -> clearing triggers recovery notification!
    async with db_factory() as session:
        acc_down = XtreamAccount(
            id="acc_down",
            label="Fournisseur Panne",
            base_url="http://down.test",
            username="secret_u3",
            password="secret_p3",
            outage_strikes=3,
            is_active=True,
        )
        session.add(acc_down)
        await session.commit()

    await outage_service.record_clear("acc_down", session_factory=db_factory)
    await asyncio.sleep(0.01)
    assert len(captured_notifications) == 1

    recov = captured_notifications[0]
    assert recov["event"] == "outage"
    assert recov["severity"] == "info"
    assert "Rétablissement" in recov["title"]
    assert "Fournisseur Panne" in recov["title"]
    assert recov["details"]["previous_strikes"] == 3
    assert recov["details"]["status"] == "recovered"

    # Invariant: zero credentials
    full_dump = str(recov)
    assert "secret_u3" not in full_dump
    assert "secret_p3" not in full_dump


# ─── 2. Download Batch Completion Notifications ───────────────────────────────


async def test_download_batch_completed_notification(db_factory, monkeypatch, notify_config):
    """A batch with all jobs completed triggers severity='info' notification."""
    batch_id = "batch_comp_1"
    async with db_factory() as session:
        batch = DownloadBatch(
            id=batch_id,
            media_type="show",
            title="Breaking Bad",
            server_id="srv_1",
            rating_key="rk_show",
            scope="series_all",
            total_jobs=2,
            created_at=now_ms(),
        )
        j1 = DownloadJob(
            id="job_1",
            batch_id=batch_id,
            state="queued",
            media_type="episode",
            dest_path="Shows/BB/S01E01.mkv",
            server_id="srv_1",
            rating_key="rk_1",
            title="S01E01",
            created_at=now_ms(),
            updated_at=now_ms(),
        )
        j2 = DownloadJob(
            id="job_2",
            batch_id=batch_id,
            state="queued",
            media_type="episode",
            dest_path="Shows/BB/S01E02.mkv",
            server_id="srv_1",
            rating_key="rk_2",
            title="S01E02",
            created_at=now_ms(),
            updated_at=now_ms(),
        )
        session.add_all([batch, j1, j2])
        await session.commit()

    captured_notifications: list[dict] = []

    async def _capture_send(event, title, message, *, severity="info", key=None, details=None):
        captured_notifications.append({
            "event": event,
            "title": title,
            "message": message,
            "severity": severity,
            "key": key,
            "details": details,
        })
        return True

    monkeypatch.setattr(notification_service, "send_notification", _capture_send)

    # Job 1 completes
    async with db_factory() as session:
        job = await session.get(DownloadJob, "job_1")
        job.state = "completed"
        await session.commit()

    await download_worker._check_and_notify_batch_completion(db_factory, batch_id)
    await asyncio.sleep(0.01)
    # Batch still has job_2 in queued -> no notification
    assert len(captured_notifications) == 0

    # Job 2 completes
    async with db_factory() as session:
        job = await session.get(DownloadJob, "job_2")
        job.state = "completed"
        await session.commit()

    await download_worker._check_and_notify_batch_completion(db_factory, batch_id)
    await asyncio.sleep(0.01)
    # Now whole batch is completed!
    assert len(captured_notifications) == 1
    notif = captured_notifications[0]
    assert notif["event"] == "download"
    assert notif["severity"] == "info"
    assert "Breaking Bad" in notif["title"]
    assert notif["details"]["status"] == "completed"
    assert notif["details"]["completed"] == 2
    assert notif["details"]["failed"] == 0


async def test_download_batch_failed_notification(db_factory, monkeypatch, notify_config):
    """A batch where all jobs fail triggers severity='error'."""
    batch_id = "batch_fail_1"
    async with db_factory() as session:
        batch = DownloadBatch(
            id=batch_id,
            media_type="show",
            title="Chernobyl",
            server_id="srv_1",
            rating_key="rk_chernobyl",
            scope="series_all",
            total_jobs=1,
            created_at=now_ms(),
        )
        j1 = DownloadJob(
            id="job_f1",
            batch_id=batch_id,
            state="failed",
            media_type="episode",
            dest_path="Shows/Chernobyl/S01E01.mkv",
            server_id="srv_1",
            rating_key="rk_f1",
            title="S01E01",
            created_at=now_ms(),
            updated_at=now_ms(),
        )
        session.add_all([batch, j1])
        await session.commit()

    captured_notifications: list[dict] = []

    async def _capture_send(event, title, message, *, severity="info", key=None, details=None):
        captured_notifications.append({
            "event": event,
            "title": title,
            "message": message,
            "severity": severity,
            "key": key,
            "details": details,
        })
        return True

    monkeypatch.setattr(notification_service, "send_notification", _capture_send)

    await download_worker._check_and_notify_batch_completion(db_factory, batch_id)
    await asyncio.sleep(0.01)

    assert len(captured_notifications) == 1
    notif = captured_notifications[0]
    assert notif["event"] == "download"
    assert notif["severity"] == "error"
    assert "échoué" in notif["title"]
    assert "Chernobyl" in notif["title"]
    assert notif["details"]["status"] == "failed"
    assert notif["details"]["completed"] == 0
    assert notif["details"]["failed"] == 1


async def test_download_batch_partial_notification(db_factory, monkeypatch, notify_config):
    """A batch with mixed completed and failed jobs triggers severity='warning'."""
    batch_id = "batch_partial_1"
    async with db_factory() as session:
        batch = DownloadBatch(
            id=batch_id,
            media_type="show",
            title="The Wire",
            server_id="srv_1",
            rating_key="rk_wire",
            scope="series_all",
            total_jobs=2,
            created_at=now_ms(),
        )
        j1 = DownloadJob(
            id="job_p1",
            batch_id=batch_id,
            state="completed",
            media_type="episode",
            dest_path="Shows/The Wire/S01E01.mkv",
            server_id="srv_1",
            rating_key="rk_p1",
            title="S01E01",
            created_at=now_ms(),
            updated_at=now_ms(),
        )
        j2 = DownloadJob(
            id="job_p2",
            batch_id=batch_id,
            state="failed",
            media_type="episode",
            dest_path="Shows/The Wire/S01E02.mkv",
            server_id="srv_1",
            rating_key="rk_p2",
            title="S01E02",
            created_at=now_ms(),
            updated_at=now_ms(),
        )
        session.add_all([batch, j1, j2])
        await session.commit()

    captured_notifications: list[dict] = []

    async def _capture_send(event, title, message, *, severity="info", key=None, details=None):
        captured_notifications.append({
            "event": event,
            "title": title,
            "message": message,
            "severity": severity,
            "key": key,
            "details": details,
        })
        return True

    monkeypatch.setattr(notification_service, "send_notification", _capture_send)

    await download_worker._check_and_notify_batch_completion(db_factory, batch_id)
    await asyncio.sleep(0.01)

    assert len(captured_notifications) == 1
    notif = captured_notifications[0]
    assert notif["event"] == "download"
    assert notif["severity"] == "warning"
    assert "partiel" in notif["title"]
    assert "The Wire" in notif["title"]
    assert notif["details"]["status"] == "partial"
    assert notif["details"]["completed"] == 1
    assert notif["details"]["failed"] == 1


# ─── 3. Backup Failure Notifications ──────────────────────────────────────────


def test_backup_failure_notifies_on_missing_db(tmp_path, monkeypatch, notify_config):
    """Missing DB triggers event='backup', severity='error' in run_backup()."""
    missing_db = tmp_path / "non_existent.db"
    backup_dir = tmp_path / "backups"
    monkeypatch.setattr(settings, "DB_PATH", missing_db)
    monkeypatch.setattr(settings, "BACKUP_DIR", backup_dir)

    captured_notifications: list[dict] = []

    async def _capture_send(event, title, message, *, severity="info", key=None, details=None):
        captured_notifications.append({
            "event": event,
            "title": title,
            "message": message,
            "severity": severity,
            "key": key,
            "details": details,
        })
        return True

    monkeypatch.setattr(notification_service, "send_notification", _capture_send)

    with pytest.raises(FileNotFoundError):
        backup_db.run_backup()

    assert len(captured_notifications) == 1
    notif = captured_notifications[0]
    assert notif["event"] == "backup"
    assert notif["severity"] == "error"
    assert notif["details"]["reason"] == "db_not_found"


def test_backup_failure_notifies_on_integrity_error(tmp_path, monkeypatch, notify_config):
    """Integrity check failure in run_backup triggers notification with corrupt title."""
    real_db = tmp_path / "corrupt.db"
    real_db.write_text("corrupted content not a sqlite db")
    backup_dir = tmp_path / "backups"
    monkeypatch.setattr(settings, "DB_PATH", real_db)
    monkeypatch.setattr(settings, "BACKUP_DIR", backup_dir)

    captured_notifications: list[dict] = []

    async def _capture_send(event, title, message, *, severity="info", key=None, details=None):
        captured_notifications.append({
            "event": event,
            "title": title,
            "message": message,
            "severity": severity,
            "key": key,
            "details": details,
        })
        return True

    monkeypatch.setattr(notification_service, "send_notification", _capture_send)

    # Mock perform_backup to simulate integrity check failure
    def _fake_perform_backup(src, dst):
        raise backup_db.BackupIntegrityError("PRAGMA integrity_check failed: row count mismatch")

    monkeypatch.setattr(backup_db, "perform_backup", _fake_perform_backup)

    with pytest.raises(backup_db.BackupIntegrityError):
        backup_db.run_backup()

    assert len(captured_notifications) == 1
    notif = captured_notifications[0]
    assert notif["event"] == "backup"
    assert notif["severity"] == "error"
    assert "Sauvegarde corrompue" in notif["title"]
    assert notif["details"]["reason"] == "integrity_check_failed"


def test_backup_verify_latest_notifies_on_corruption(tmp_path, monkeypatch, notify_config):
    """verify_latest() encountering a corrupt backup file triggers notification."""
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    corrupt_backup = backup_dir / "plexhub-20261115-200000-000000.db"
    corrupt_backup.write_text("fake db content")

    captured_notifications: list[dict] = []

    async def _capture_send(event, title, message, *, severity="info", key=None, details=None):
        captured_notifications.append({
            "event": event,
            "title": title,
            "message": message,
            "severity": severity,
            "key": key,
            "details": details,
        })
        return True

    monkeypatch.setattr(notification_service, "send_notification", _capture_send)

    def _fake_check_integrity(path, log=None):
        raise backup_db.BackupIntegrityError("corrupted database file")

    monkeypatch.setattr(backup_db, "check_integrity", _fake_check_integrity)

    with pytest.raises(backup_db.BackupIntegrityError):
        backup_db.verify_latest(backup_dir)

    assert len(captured_notifications) == 1
    notif = captured_notifications[0]
    assert notif["event"] == "backup"
    assert notif["severity"] == "error"
    assert notif["details"]["reason"] == "integrity_check_failed"


def test_backup_success_does_not_notify(tmp_path, monkeypatch, notify_config):
    """Successful backup does not dispatch any failure notification."""
    import sqlite3
    real_db = tmp_path / "valid.db"
    conn = sqlite3.connect(str(real_db))
    conn.execute("CREATE TABLE t (id INT)")
    conn.commit()
    conn.close()

    backup_dir = tmp_path / "backups"
    monkeypatch.setattr(settings, "DB_PATH", real_db)
    monkeypatch.setattr(settings, "BACKUP_DIR", backup_dir)

    captured_notifications: list[dict] = []

    async def _capture_send(event, title, message, *, severity="info", key=None, details=None):
        captured_notifications.append({"event": event})
        return True

    monkeypatch.setattr(notification_service, "send_notification", _capture_send)

    target = backup_db.run_backup()
    assert target.exists()
    assert len(captured_notifications) == 0


async def test_download_standalone_job_no_batch_notification(db_factory, monkeypatch, notify_config):
    """A standalone job without batch_id does not dispatch any batch notifications."""
    captured_notifications: list[dict] = []

    async def _capture_send(event, title, message, *, severity="info", key=None, details=None):
        captured_notifications.append({"event": event})
        return True

    monkeypatch.setattr(notification_service, "send_notification", _capture_send)

    # Calling with batch_id=None
    await download_worker._check_and_notify_batch_completion(db_factory, None)
    await asyncio.sleep(0.01)
    assert len(captured_notifications) == 0
