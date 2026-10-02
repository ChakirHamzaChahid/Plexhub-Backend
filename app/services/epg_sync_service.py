"""Bulk EPG synchronisation from each Xtream account's XMLTV (Lot 10 sub-lot D,
FR-BE-222 / FR-BE-559 / FR-BE-560, ADR 0006).

Until now the EPG was loaded ON DEMAND, one channel at a time
(``GET /api/live/channels/{stream_id}/epg`` -> ``live_service.ingest_short_epg``),
so the TV's "now playing" seed was empty for every channel nobody had opened.
This service fills ``epg_entries`` for the whole account in one pass:

    active, non-outage account
      -> LiveChannel.epg_channel_id -> stream_id map  (visible channels only)
      -> ``xtream_service.download_xmltv``  (streamed to a TEMP FILE, size-capped)
      -> ``iterparse`` of that file, in ``asyncio.to_thread``, batch by batch
      -> keep programmes OVERLAPPING [now - PAST, now + FUTURE]
      -> UPSERT in short batches through ``write_with_retry`` (fresh session
         per attempt) on ``(server_id, stream_id, start_time)``

Design constraints, each learned from this codebase (CLAUDE.md §3/§9):

* SQLite has ONE writer. The pipeline and the API write too, so every write
  transaction here is a single short batch (``EPG_SYNC_BATCH_SIZE`` rows,
  milliseconds), never one account-sized transaction.
* Memory: the container is capped at 2 GB. The XMLTV is NEVER held in memory
  (``get_xmltv`` returned one ``str``; it was removed). It streams to disk, and
  the parser hands over one bounded batch at a time, clearing parsed elements.
* The event loop never parses XML or writes the file: both happen in threads.
* One failing account (network, invalid XML, size cap) never stops the others.
* No secret in a log or a report: the download URL carries the credentials, so
  errors are reported by TYPE (+ HTTP status), never by interpolating httpx text.

Dates: XMLTV times (``YYYYMMDDHHMMSS +HHMM``) are converted to epoch
MILLISECONDS, the unit ``epg_entries.start_time/end_time`` already holds for the
short-EPG path (``start_timestamp * 1000``) and that ``GET /api/live/*`` compares
against ``int(time.time() * 1000)``.

Unlike the short-EPG Xtream payload, XMLTV text is plain XML text: it is NOT
base64-decoded (``live_service._try_base64_decode`` applies to the JSON API only).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterator

from sqlalchemy import delete, func, or_, select, tuple_
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.database import EpgEntry, LiveChannel, XtreamAccount
from app.services import account_outage_service
from app.services.xtream_service import XmltvTooLargeError, xtream_service
from app.utils.db_retry import write_with_retry
from app.utils.server_id import build_server_id
from app.utils.time import now_ms

logger = logging.getLogger("plexhub.epg_sync")

SessionFactory = Callable[[], AsyncSession]

_MAX_TITLE_CHARS = 500
_MAX_DESCRIPTION_CHARS = 4000
# XMLTV declares its DTD with a bare ``<!DOCTYPE tv SYSTEM "xmltv.dtd">`` (no
# internal subset). A document that DECLARES ENTITIES is never a legitimate
# XMLTV and is the vector for entity-expansion attacks: refuse it outright.
_DOCTYPE_SCAN_BYTES = 64 * 1024
# Ended programmes are deleted per CONTIGUOUS RANGE of this many stream_ids per
# transaction (see _prune_ended: locality, not row count, is what bounds the WAL).
_PRUNE_STREAMS_PER_TXN = 250


class EpgSyncError(Exception):
    """A recoverable, account-level failure whose message is safe to log."""


# ─── Reports ────────────────────────────────────────────────────────────────


@dataclass
class AccountEpgReport:
    account_id: str
    # ok | failed | skipped_outage | no_channels
    status: str = "ok"
    bytes_downloaded: int = 0
    programmes_seen: int = 0          # <programme> elements read
    skipped_invalid: int = 0          # missing/invalid channel, times, or stop <= start
    skipped_out_of_window: int = 0
    skipped_unmapped: int = 0         # XMLTV channel id matches no visible LiveChannel
    rows_emitted: int = 0             # rows handed to the writer (after stream_id fan-out)
    inserted: int = 0
    updated: int = 0                  # existing rows whose content actually changed
    unchanged: int = 0                # existing rows already identical (not rewritten)
    pruned: int = 0                   # ended programmes deleted after a successful sync
    truncated: bool = False           # EPG_SYNC_MAX_PROGRAMMES reached
    error: str | None = None
    duration_s: float = 0.0


@dataclass
class EpgSyncReport:
    accounts: list[AccountEpgReport] = field(default_factory=list)
    duration_s: float = 0.0

    @property
    def accounts_ok(self) -> int:
        return sum(1 for a in self.accounts if a.status in ("ok", "no_channels"))

    @property
    def accounts_failed(self) -> int:
        return sum(1 for a in self.accounts if a.status == "failed")

    @property
    def accounts_skipped(self) -> int:
        return sum(1 for a in self.accounts if a.status == "skipped_outage")

    @property
    def inserted(self) -> int:
        return sum(a.inserted for a in self.accounts)

    @property
    def updated(self) -> int:
        return sum(a.updated for a in self.accounts)

    @property
    def unchanged(self) -> int:
        return sum(a.unchanged for a in self.accounts)

    @property
    def pruned(self) -> int:
        return sum(a.pruned for a in self.accounts)


# ─── Pure helpers: time, error text ─────────────────────────────────────────

# XMLTV: "YYYYMMDDhhmmss +HHMM". The spec allows truncation (YYYYMMDDhhmm, or
# date only) and omitting the offset (then UTC, per the spec).
_XMLTV_TIME_RE = re.compile(
    r"^\s*(\d{4})(\d{2})(\d{2})(?:(\d{2})(\d{2})(\d{2})?)?"
    r"\s*(?:(Z)|([+-])(\d{2}):?(\d{2}))?\s*$"
)


def parse_xmltv_time(value: str | None) -> int | None:
    """Convert an XMLTV timestamp to epoch milliseconds, or ``None`` if it is
    absent or not a real date (month 13, hour 25, garbage, offset beyond a day)."""
    if not value:
        return None
    m = _XMLTV_TIME_RE.match(value)
    if not m:
        return None
    year, month, day, hh, mm, ss, zulu, sign, oh, om = m.groups()
    tz = timezone.utc
    if sign:
        off = timedelta(hours=int(oh), minutes=int(om))
        if off >= timedelta(hours=24):
            return None
        tz = timezone(off if sign == "+" else -off)
    try:
        dt = datetime(
            int(year), int(month), int(day),
            int(hh or 0), int(mm or 0), int(ss or 0), tzinfo=tz,
        )
    except ValueError:
        return None
    return int(dt.timestamp() * 1000)


def _channel_key(value: str | None) -> str:
    """Normalised join key between XMLTV ``channel=`` and
    ``LiveChannel.epg_channel_id`` (trimmed, case-folded)."""
    return (value or "").strip().casefold()


def _safe_error(exc: BaseException) -> str:
    """A log/report-safe description. NEVER ``str(exc)`` for httpx errors: an
    ``HTTPStatusError`` message embeds the full request URL, i.e. the Xtream
    username and password."""
    import httpx

    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTPStatusError: HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.HTTPError):
        return type(exc).__name__
    if isinstance(exc, (ET.ParseError, EpgSyncError, XmltvTooLargeError)):
        return f"{type(exc).__name__}: {exc}"
    return type(exc).__name__


# ─── Streaming XMLTV parser (runs in a worker thread) ───────────────────────


@dataclass
class _ParseStats:
    programmes_seen: int = 0
    skipped_invalid: int = 0
    skipped_out_of_window: int = 0
    skipped_unmapped: int = 0
    rows_emitted: int = 0
    truncated: bool = False


def _assert_no_entity_declarations(path: str) -> None:
    with open(path, "rb") as fh:
        head = fh.read(_DOCTYPE_SCAN_BYTES)
    if b"<!ENTITY" in head:
        raise EpgSyncError("XMLTV declares XML entities; refusing to parse")


def _text(elem: ET.Element | None, limit: int) -> str | None:
    if elem is None or elem.text is None:
        return None
    value = elem.text.strip()
    return value[:limit] if value else None


def iter_programme_batches(
    path: str,
    channel_map: dict[str, list[tuple[int, str]]],
    *,
    server_id: str,
    win_start_ms: int,
    win_end_ms: int,
    fetched_at: int,
    batch_size: int,
    max_rows: int,
    stats: _ParseStats,
) -> Iterator[list[dict]]:
    """Yield lists of ``EpgEntry`` row dicts, at most ``batch_size`` long.

    Synchronous and blocking by design: the caller advances it with
    ``asyncio.to_thread(next, it, None)`` so XML parsing never runs on the event
    loop, and only ONE batch of rows ever lives in memory at a time. Parsed
    elements are released as the parse advances (``root.clear()``), so memory is
    flat in the file size. Raises ``ET.ParseError`` on malformed XML (rows
    already yielded stay valid: the writes are idempotent upserts).

    ``channel_map``: normalised XMLTV channel id -> [(stream_id, epg_channel_id)].
    A programme is fanned out to EVERY stream_id sharing that epg_channel_id
    (HD/SD/backup variants of the same channel all exist in the app and the TV
    queries by stream_id).
    """
    _assert_no_entity_declarations(path)
    # We own the file handle (instead of handing ``iterparse`` a path): if the
    # consumer abandons this generator mid-file (``close()`` on error), the
    # ``with`` releases the descriptor deterministically, which is what lets
    # the caller ``os.unlink`` the temp file on Windows too.
    with open(path, "rb") as source:
        yield from _iter_batches_from(
            source, channel_map, server_id, win_start_ms, win_end_ms,
            fetched_at, batch_size, max_rows, stats,
        )


def _iter_batches_from(
    source, channel_map, server_id, win_start_ms, win_end_ms,
    fetched_at, batch_size, max_rows, stats,
) -> Iterator[list[dict]]:
    batch: list[dict] = []
    root: ET.Element | None = None

    for event, elem in ET.iterparse(source, events=("start", "end")):
        if event == "start":
            if root is None:
                root = elem
            continue
        if elem.tag != "programme":
            continue

        stats.programmes_seen += 1
        start_ms = parse_xmltv_time(elem.get("start"))
        stop_ms = parse_xmltv_time(elem.get("stop"))
        xml_channel = _channel_key(elem.get("channel"))

        if not xml_channel or start_ms is None or stop_ms is None or stop_ms <= start_ms:
            stats.skipped_invalid += 1
        elif stop_ms <= win_start_ms or start_ms >= win_end_ms:
            # Keep anything that OVERLAPS the window (a programme already
            # airing at window start must stay: it IS the "now playing").
            stats.skipped_out_of_window += 1
        else:
            targets = channel_map.get(xml_channel)
            if not targets:
                stats.skipped_unmapped += 1
            else:
                title_el = elem.find("title")
                title = _text(title_el, _MAX_TITLE_CHARS) or "Unknown"
                description = _text(elem.find("desc"), _MAX_DESCRIPTION_CHARS)
                lang = title_el.get("lang") if title_el is not None else None
                for stream_id, epg_channel_id in targets:
                    if stats.rows_emitted >= max_rows:
                        stats.truncated = True
                        break
                    batch.append({
                        "server_id": server_id,
                        "epg_channel_id": epg_channel_id,
                        "stream_id": stream_id,
                        "title": title,
                        "description": description,
                        "start_time": start_ms,
                        "end_time": stop_ms,
                        "lang": lang,
                        "fetched_at": fetched_at,
                    })
                    stats.rows_emitted += 1
                    if len(batch) >= batch_size:
                        yield batch
                        batch = []

        # Release memory: drop every completed child of the root.
        if root is not None:
            root.clear()
        if stats.truncated:
            break

    if batch:
        yield batch


# ─── Batch writer ───────────────────────────────────────────────────────────


async def _write_batch(
    rows: list[dict], server_id: str, session_factory: SessionFactory,
) -> tuple[int, int, int]:
    """Upsert one batch. Returns ``(inserted, updated, unchanged)``.

    ``work`` is fully replayable (``write_with_retry``: a FRESH session per
    attempt, ADR 0004 Decision 4). A row whose content is identical to the
    stored one is not rewritten (``DO UPDATE ... WHERE <changed>``), so a
    re-run only costs real WAL writes for programmes that actually changed.
    """
    by_key: dict[tuple[int, int], dict] = {}
    for r in rows:  # XMLTV may repeat a programme: last occurrence wins
        by_key[(r["stream_id"], r["start_time"])] = r
    deduped = list(by_key.values())
    keys = list(by_key)

    async def work(session: AsyncSession) -> tuple[int, int, int]:
        existing = (await session.execute(
            select(func.count()).select_from(EpgEntry).where(
                EpgEntry.server_id == server_id,
                tuple_(EpgEntry.stream_id, EpgEntry.start_time).in_(keys),
            )
        )).scalar() or 0

        stmt = sqlite_insert(EpgEntry).values(deduped)
        excl = stmt.excluded
        changed = or_(
            EpgEntry.title != excl.title,
            EpgEntry.end_time != excl.end_time,
            EpgEntry.epg_channel_id != excl.epg_channel_id,
            func.coalesce(EpgEntry.description, "") != func.coalesce(excl.description, ""),
            func.coalesce(EpgEntry.lang, "") != func.coalesce(excl.lang, ""),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["server_id", "stream_id", "start_time"],
            set_={
                "epg_channel_id": excl.epg_channel_id,
                "title": excl.title,
                "description": excl.description,
                "end_time": excl.end_time,
                "lang": excl.lang,
                "fetched_at": excl.fetched_at,
            },
            where=changed,
        ).returning(EpgEntry.id)
        affected = len((await session.execute(stmt)).all())
        await session.commit()

        inserted = max(0, len(deduped) - existing)
        updated = max(0, affected - inserted)
        unchanged = max(0, existing - updated)
        return inserted, updated, unchanged

    return await write_with_retry(work, session_factory=session_factory, op="epg_sync.batch")


async def _prune_ended(
    session_factory: SessionFactory, server_id: str, cutoff_ms: int,
) -> int:
    """Delete this server's programmes that ended before ``cutoff_ms``.

    Why the sync prunes instead of leaving it all to the nightly
    ``_cleanup_stale_epg``: every programme enters the table ~12 h before it
    airs and would otherwise sit there, ended, until 03:00 -- up to ~2.8x the
    window's rows -- and the nightly job then removes them in ONE transaction
    that holds SQLite's single writer for the whole delete. Pruning here keeps
    ``epg_entries`` at roughly window size. The nightly purge stays as the safety
    net (accounts the sync no longer visits, short-EPG rows, a sync that is off).

    Same semantics as the nightly purge (``end_time`` in the past), only with
    the cutoff at the window start so the last ``EPG_SYNC_PAST_HOURS`` of
    history stay readable.

    HOW it deletes matters more than how fast (measured, ADR 0006 D4): the
    ended rows of a guide are the OLDEST programme of EVERY channel, so they are
    scattered across all the stream-ordered indexes (``epg_entries`` carries 7).
    A delete chunked by AGE ("the next 2 000 stale rows") therefore dirties
    nearly every index page on every chunk, and the WAL (which logs whole pages)
    ballooned to ~250 MB for a 63 000-row table. Chunking by contiguous
    ``stream_id`` RANGE keeps each transaction on a small, adjacent slice of
    those indexes (and on the oldest slice of the time index), so each page is
    logged about once per run. Each transaction still stays short.
    """
    async with session_factory() as session:
        stale_streams = (await session.execute(
            select(EpgEntry.stream_id).where(
                EpgEntry.server_id == server_id,
                EpgEntry.end_time < cutoff_ms,
                EpgEntry.stream_id.is_not(None),
            ).distinct().order_by(EpgEntry.stream_id)
        )).scalars().all()

    total = 0
    for i in range(0, len(stale_streams), _PRUNE_STREAMS_PER_TXN):
        lo = stale_streams[i]
        hi = stale_streams[min(i + _PRUNE_STREAMS_PER_TXN, len(stale_streams)) - 1]

        async def work(session: AsyncSession, lo=lo, hi=hi) -> int:
            res = await session.execute(delete(EpgEntry).where(
                EpgEntry.server_id == server_id,
                EpgEntry.stream_id.between(lo, hi),
                EpgEntry.end_time < cutoff_ms,
            ))
            await session.commit()
            return res.rowcount or 0

        total += await write_with_retry(work, session_factory=session_factory, op="epg_sync.prune")
        await asyncio.sleep(0)
    return total


# ─── Per-account and whole-run orchestration ────────────────────────────────


async def _load_channel_map(
    session_factory: SessionFactory, server_id: str,
) -> dict[str, list[tuple[int, str]]]:
    """XMLTV channel key -> [(stream_id, epg_channel_id)] for the channels the
    live API itself exposes.

    The visibility rule is the one ``GET /api/live/channels`` applies and
    nothing else: ``is_in_allowed_categories == True``. The live routes do NOT
    filter ``is_adult`` server-side (the flag travels in the channel payload and
    the client decides), so neither does the sync: adult channels in an allowed
    category are synced like any other, and a channel hidden by the category
    filter is never fetched for.
    """
    async with session_factory() as session:
        rows = (await session.execute(
            select(LiveChannel.stream_id, LiveChannel.epg_channel_id).where(
                LiveChannel.server_id == server_id,
                LiveChannel.is_in_allowed_categories == True,  # noqa: E712
                LiveChannel.epg_channel_id.is_not(None),
                LiveChannel.epg_channel_id != "",
            )
        )).all()
    channel_map: dict[str, list[tuple[int, str]]] = {}
    for stream_id, epg_channel_id in rows:
        key = _channel_key(epg_channel_id)
        if key:
            channel_map.setdefault(key, []).append((stream_id, epg_channel_id))
    return channel_map


async def _sync_account(
    account: XtreamAccount, session_factory: SessionFactory, *, now: int,
) -> AccountEpgReport:
    started = time.monotonic()
    report = AccountEpgReport(account_id=account.id)
    server_id = build_server_id(account.id)
    tmp_path: str | None = None
    batches: Iterator[list[dict]] | None = None
    try:
        channel_map = await _load_channel_map(session_factory, server_id)
        if not channel_map:
            report.status = "no_channels"
            return report

        past_h = max(0, settings.EPG_SYNC_PAST_HOURS)
        future_h = max(0, settings.EPG_SYNC_FUTURE_HOURS)
        win_start = now - past_h * 3_600_000
        win_end = now + future_h * 3_600_000

        fd, tmp_path = tempfile.mkstemp(prefix="plexhub-xmltv-", suffix=".xml")
        os.close(fd)
        report.bytes_downloaded = await xtream_service.download_xmltv(
            account, tmp_path,
            max_bytes=max(1, settings.EPG_SYNC_MAX_BYTES),
            total_timeout=float(max(1, settings.EPG_SYNC_DOWNLOAD_TIMEOUT_SECONDS)),
        )

        stats = _ParseStats()
        batches = iter_programme_batches(
            tmp_path, channel_map,
            server_id=server_id, win_start_ms=win_start, win_end_ms=win_end,
            fetched_at=now, batch_size=max(1, settings.EPG_SYNC_BATCH_SIZE),
            max_rows=max(1, settings.EPG_SYNC_MAX_PROGRAMMES), stats=stats,
        )
        try:
            while True:
                # Parsing (CPU + file I/O) happens off the event loop.
                batch = await asyncio.to_thread(next, batches, None)
                if batch is None:
                    break
                ins, upd, unch = await _write_batch(batch, server_id, session_factory)
                report.inserted += ins
                report.updated += upd
                report.unchanged += unch
                # Let API requests breathe between batches (0 ms = just yield).
                await asyncio.sleep(max(0, settings.EPG_SYNC_BATCH_PAUSE_MS) / 1000)
        finally:
            report.programmes_seen = stats.programmes_seen
            report.skipped_invalid = stats.skipped_invalid
            report.skipped_out_of_window = stats.skipped_out_of_window
            report.skipped_unmapped = stats.skipped_unmapped
            report.rows_emitted = stats.rows_emitted
            report.truncated = stats.truncated

        # The account synced cleanly: drop what has ended (best-effort -- a
        # failed prune must never fail an otherwise good sync).
        try:
            report.pruned = await _prune_ended(session_factory, server_id, win_start)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "EPG prune failed for account %s: %s", account.id, _safe_error(exc),
            )
    except Exception as exc:  # noqa: BLE001 — one account must never stop the run
        report.status = "failed"
        report.error = _safe_error(exc)
        logger.warning("EPG sync failed for account %s: %s", account.id, report.error)
    finally:
        if batches is not None:
            with contextlib.suppress(Exception):
                batches.close()
        if tmp_path is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
        report.duration_s = round(time.monotonic() - started, 3)
    return report


async def run_epg_sync(session_factory: SessionFactory | None = None) -> EpgSyncReport:
    """Sync the EPG of every active, non-outage Xtream account.

    ``session_factory`` defaults to the dedicated WORKER pool
    (``worker_session_factory``): background writes must never consume the
    connection slots that serve API requests.

    Never raises for an account-level problem; the returned report carries one
    ``AccountEpgReport`` per account (ok / failed / skipped_outage /
    no_channels). Only an infrastructure failure BEFORE any account is
    attempted (e.g. cannot list accounts) propagates, so ``track_job`` does not
    mark a run that did nothing as fresh.
    """
    if session_factory is None:
        from app.db.database import worker_session_factory as session_factory  # type: ignore[assignment]

    started = time.monotonic()
    report = EpgSyncReport()

    async with session_factory() as session:
        accounts = (await session.execute(
            select(XtreamAccount).where(XtreamAccount.is_active == True)  # noqa: E712
            .order_by(XtreamAccount.id)
        )).scalars().all()
        masked = await account_outage_service.masked_server_ids(session)

    now = now_ms()
    for account in accounts:
        if build_server_id(account.id) in masked:
            logger.info(
                "EPG sync: account %s skipped (confirmed provider outage)", account.id,
            )
            report.accounts.append(
                AccountEpgReport(account_id=account.id, status="skipped_outage")
            )
            continue
        report.accounts.append(await _sync_account(account, session_factory, now=now))

    report.duration_s = round(time.monotonic() - started, 3)
    logger.info(
        "EPG sync done in %.1fs: %d account(s) ok, %d failed, %d skipped (outage); "
        "%d inserted, %d updated, %d unchanged, %d pruned",
        report.duration_s, report.accounts_ok, report.accounts_failed,
        report.accounts_skipped, report.inserted, report.updated, report.unchanged,
        report.pruned,
    )
    return report


async def run_epg_sync_job() -> EpgSyncReport:
    """Scheduler entry point (``main.py`` wraps it in ``track_job("epg_sync")``).

    ``run_epg_sync`` never raises for an account-level problem, so a bare
    ``track_job`` would stamp the job "fresh" even if EVERY account failed --
    the exact false reassurance the freshness gauge exists to prevent. Here a
    run in which at least one account was attempted and none succeeded raises,
    so the gauge goes stale and the failure shows in the scheduler log. A
    partial failure (some accounts ok) is a success: the failed accounts are
    already in the report and the log.
    """
    report = await run_epg_sync()
    if report.accounts_failed and not report.accounts_ok:
        raise EpgSyncError(
            f"EPG sync failed for all {report.accounts_failed} attempted account(s)"
        )
    return report
