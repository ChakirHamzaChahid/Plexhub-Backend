"""State of the latest database backup, computed from the files present (lot B2).

ADR 0012 D2.3, contract B2 § 5.12. Everything is read from the backup directory on every
call — the newest present ``plexhub-<stamp>.db`` (its UTC stamp is the backup instant), the
``<name>.verified`` sidecar written by ``backup_db`` before the final rename, and the
last-attempt state file. Never from the database (a restore would make the state regress),
never by re-running ``integrity_check`` (blocking, very long on a multi-GB file), and never
with a path or a file name in the result.

``integrityCheck``:
- ``ok``       the newest file has a sidecar whose name, size and mtime still match it;
- ``unknown``  the file exists but nothing proves it was verified (made before the sidecars
               existed, replaced, edited, or a sidecar that is not an explicit ``ok``);
- ``not_run``  no backup file is present.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import settings
from app.scripts import backup_db

logger = logging.getLogger("plexhub.backup_status")

_FILE_PREFIX = backup_db._FILE_PREFIX
_FILE_SUFFIX = backup_db._FILE_SUFFIX


def timezone_label() -> str:
    """Zone in which ``BACKUP_HOUR`` is expressed: APScheduler's, i.e. the system local zone
    (``TZ``). IANA name when known, else a fixed offset such as ``UTC+02:00``."""
    try:
        import tzlocal

        name = tzlocal.get_localzone_name()
        if name:
            return str(name)
    except Exception:  # noqa: BLE001 - any failure falls back to the offset
        pass
    offset = datetime.now().astimezone().utcoffset()
    if offset is None:
        return "UTC"
    minutes = int(offset.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    hours, rest = divmod(abs(minutes), 60)
    return f"UTC{sign}{hours:02d}:{rest:02d}"


def _newest_backup(backup_dir: Path) -> tuple[Path, int] | None:
    """(file, instant in ms UTC) of the newest present, non-empty backup, or None."""
    if not backup_dir.is_dir():
        return None
    best: tuple[Path, int] | None = None
    for f in backup_dir.iterdir():
        if not f.is_file() or not backup_db.is_backup_name(f.name):
            continue
        try:
            if f.stat().st_size <= 0:
                continue
        except OSError:
            continue
        stamp = backup_db._parse_backup_stamp(f.name[len(_FILE_PREFIX):-len(_FILE_SUFFIX)])
        if stamp is None:
            continue
        at_ms = int(stamp.timestamp() * 1000)
        if best is None or at_ms > best[1]:
            best = (f, at_ms)
    return best


def _is_verified(db_file: Path) -> bool:
    try:
        data = json.loads(backup_db.verified_sidecar_path(db_file).read_text(encoding="utf-8"))
        st = db_file.stat()
    except (OSError, ValueError):
        return False
    return (
        isinstance(data, dict)
        and data.get("integrity") == "ok"
        and data.get("name") == db_file.name
        and data.get("size") == st.st_size
        and data.get("mtime_ns") == st.st_mtime_ns
    )


def _last_attempt(backup_dir: Path) -> tuple[int | None, str | None]:
    try:
        data = json.loads((backup_dir / backup_db.STATE_FILE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, None
    if not isinstance(data, dict):
        return None, None
    at_ms, outcome = data.get("at_ms"), data.get("outcome")
    if isinstance(at_ms, bool) or not isinstance(at_ms, int) or outcome not in ("ok", "failed"):
        return None, None
    return at_ms, outcome


def compute_status(now_ms: int) -> dict[str, Any]:
    backup_dir = Path(settings.BACKUP_DIR)
    newest = _newest_backup(backup_dir)
    if newest is None:
        last_success, integrity, age = None, "not_run", None
    else:
        file, last_success = newest
        integrity = "ok" if _is_verified(file) else "unknown"
        age = max(0, (now_ms - last_success) // 1000)
    attempt_at, attempt_outcome = _last_attempt(backup_dir)
    enabled = bool(settings.BACKUP_ENABLED)
    return {
        "enabled": enabled,
        "lastSuccessAt": last_success,
        "ageSeconds": age,
        "integrityCheck": integrity,
        "lastAttemptAt": attempt_at,
        "lastAttemptOutcome": attempt_outcome,
        "retentionDays": int(settings.BACKUP_RETENTION_DAYS),
        "scheduleHourLocal": int(settings.BACKUP_HOUR) if enabled else None,
        "scheduleTimezone": timezone_label(),
        "serverNowMs": now_ms,
    }
