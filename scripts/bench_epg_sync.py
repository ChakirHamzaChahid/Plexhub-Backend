"""Synthetic scale benchmark for the bulk EPG sync (Lot 10 sub-lot D, ADR 0006).

    .venv/Scripts/python.exe scripts/bench_epg_sync.py \
        --accounts 2 --channels 5000 --programmes 20 --extra 40

What it does (everything local, NO network, NEVER touches data/plexhub.db):

  1. builds a throw-away file-backed WAL database in a temp dir, with engines
     configured like production (`app/db/database.py`: busy timeout 60 s, one
     API-sized pool and one small worker pool);
  2. seeds ``--accounts`` Xtream accounts x ``--channels`` live channels;
  3. writes one XMLTV file per account holding, for every channel,
     ``--programmes`` programmes INSIDE the sync window plus ``--extra``
     programmes OUTSIDE it (a real XMLTV spans days; only the window is kept);
  4. runs ``run_epg_sync`` TWICE (cold = all inserts, warm = all unchanged), then
     ``--cycles N`` more times with the clock advanced 6 h each (steady state:
     a day of cron ticks, to check the table stays at window size), while
     a probe measures, on the same event loop, the latency of
       - the ``GET /api/live/epg`` handler (``live.get_epg_now``, DB work only),
       - a tiny concurrent write through ``write_with_retry``;
  5. reports durations, ``epg_entries`` row count and on-disk growth, peak
     process RSS (psutil sampler) and, optionally (``--tracemalloc``), the peak
     Python heap on a fresh DB.

LIMITS (read before quoting a number): this is a synthetic dataset on the
developer's machine, not the ~1.9 GB production database nor a real provider
XMLTV, and it is not constrained to the container's 1 CPU / 2 GB. The numbers
show orders of magnitude and the shape of the contention; they do NOT replace
the measurement on a prod-scale copy that must precede EPG_SYNC_ENABLED=true.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import tracemalloc
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))]


def _stats_ms(values: list[float]) -> dict:
    return {
        "n": len(values),
        "p50_ms": round(_pct(values, 0.50) * 1000, 1),
        "p95_ms": round(_pct(values, 0.95) * 1000, 1),
        "max_ms": round(max(values) * 1000, 1) if values else 0.0,
    }


def _fmt(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y%m%d%H%M%S +0000")


def write_xmltv(path: Path, now_ms: int, channels: int, programmes: int, extra: int) -> int:
    """Stream one synthetic XMLTV file to ``path``; returns its size in bytes.

    Programmes sit on a FIXED grid of ``slot``-long slots anchored at the epoch
    (like a real guide: a given slot has the same start/stop/title whenever the
    file is regenerated), so successive ``--cycles`` re-deliver the programmes
    they already stored (-> ``unchanged``) and only the slots that newly enter
    the window are inserts. ``programmes`` is the number of slots per 13 h
    window (a programme overlapping the window edge makes it ``programmes`` or
    ``programmes + 1`` rows per channel); ``extra`` slots BEFORE the window must
    be discarded by the window filter."""
    window_ms = 13 * 3_600_000
    slot = window_ms // max(1, programmes)
    win_start = now_ms - 3_600_000
    win_end = now_ms + 12 * 3_600_000
    first = (win_start // slot) * slot
    last = ((win_end - 1) // slot) * slot

    def prog(c: int, s: int, long_desc: bool) -> str:
        k = s // slot
        if long_desc:
            return (
                f'<programme start="{_fmt(s)}" stop="{_fmt(s + slot)}" channel="ch{c}">'
                f'<title lang="fr">Emission {c}-{k}</title>'
                f'<desc lang="fr">Description synthetique de l\'emission {c}-{k}, '
                "assez longue pour ressembler a un resume de guide des programmes.</desc>"
                "<category>Divertissement</category></programme>\n"
            )
        return (
            f'<programme start="{_fmt(s)}" stop="{_fmt(s + slot)}" channel="ch{c}">'
            f"<title>Ancienne {c}-{k}</title><desc>Hors fenetre.</desc></programme>\n"
        )

    with open(path, "w", encoding="utf-8") as fh:
        fh.write('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE tv SYSTEM "xmltv.dtd">\n<tv>\n')
        for c in range(channels):
            fh.write(f'<channel id="ch{c}"><display-name>Chaine {c}</display-name></channel>\n')
        for c in range(channels):
            parts = [prog(c, first - (j + 1) * slot, False) for j in range(extra)]
            parts += [prog(c, s, True) for s in range(first, last + 1, slot)]
            fh.write("".join(parts))
        fh.write("</tv>\n")
    return path.stat().st_size


class RssSampler:
    """Peak resident set size of this process, sampled in a thread."""

    def __init__(self) -> None:
        import psutil

        self._proc = psutil.Process()
        self.baseline = self._proc.memory_info().rss
        self.peak = self.baseline
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.peak = max(self.peak, self._proc.memory_info().rss)
            time.sleep(0.05)

    def __enter__(self) -> "RssSampler":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join()
        self.peak = max(self.peak, self._proc.memory_info().rss)

    @property
    def peak_delta_mb(self) -> float:
        return round((self.peak - self.baseline) / 1_048_576, 1)


def _make_engine(db_path: str, *, pool_size: int, max_overflow: int):
    from sqlalchemy.ext.asyncio import create_async_engine

    return create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 60},  # busy timeout 60 s, as prod
        pool_size=pool_size, max_overflow=max_overflow, pool_timeout=30,
    )


async def _seed(api_factory, accounts: int, channels: int) -> list[str]:
    from app.models.database import LiveChannel, XtreamAccount

    server_ids = []
    async with api_factory() as s:
        for a in range(accounts):
            aid = f"bench{a}"
            server_ids.append(f"xtream_{aid}")
            s.add(XtreamAccount(
                id=aid, label=aid, base_url=f"http://{aid}.invalid", port=80,
                username="u", password="p", is_active=True, created_at=0,
            ))
        await s.flush()
        for a in range(accounts):
            for c in range(channels):
                s.add(LiveChannel(
                    stream_id=c + 1, server_id=server_ids[a], name=f"Chaine {c}",
                    name_sortable=f"chaine {c}", epg_channel_id=f"ch{c}", category_id="1",
                    added_at=0,
                ))
            await s.flush()
        await s.commit()
    return server_ids


async def _probe(stop: asyncio.Event, api_factory, worker_factory, server_id: str,
                 reads: list[float], writes: list[float], errors: list[str]) -> None:
    from sqlalchemy import update

    from app.api import live
    from app.models.database import XtreamAccount
    from app.utils.db_retry import write_with_retry

    n = 0
    while not stop.is_set():
        n += 1
        t0 = time.perf_counter()
        try:
            async with api_factory() as session:
                await live.get_epg_now(server_id=server_id, limit=500, offset=0, db=session)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"read:{type(exc).__name__}")
        reads.append(time.perf_counter() - t0)

        async def work(session, n=n):
            await session.execute(
                update(XtreamAccount).where(XtreamAccount.id == "bench0").values(label=f"w{n}")
            )
            await session.commit()

        t0 = time.perf_counter()
        try:
            await write_with_retry(work, session_factory=api_factory, op="bench_write")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"write:{type(exc).__name__}")
        writes.append(time.perf_counter() - t0)
        await asyncio.sleep(0.05)


async def _run_once(label, api_factory, worker_factory, server_id) -> dict:
    from app.services import epg_sync_service

    reads: list[float] = []
    writes: list[float] = []
    errors: list[str] = []
    stop = asyncio.Event()
    probe = asyncio.create_task(_probe(stop, api_factory, worker_factory, server_id, reads, writes, errors))
    with RssSampler() as rss:
        t0 = time.perf_counter()
        report = await epg_sync_service.run_epg_sync(worker_factory)
        elapsed = time.perf_counter() - t0
    stop.set()
    await probe
    return {
        "run": label,
        "duration_s": round(elapsed, 2),
        "inserted": report.inserted, "updated": report.updated, "unchanged": report.unchanged,
        "pruned": report.pruned,
        "accounts_ok": report.accounts_ok, "accounts_failed": report.accounts_failed,
        "per_account": [
            {k: getattr(a, k) for k in (
                "account_id", "status", "bytes_downloaded", "programmes_seen",
                "skipped_out_of_window", "rows_emitted", "inserted", "duration_s", "error")}
            for a in report.accounts
        ],
        "peak_rss_delta_mb": rss.peak_delta_mb,
        "concurrent_read_get_epg_now": _stats_ms(reads),
        "concurrent_write": _stats_ms(writes),
        "probe_errors": errors,
    }


def _db_size_mb(db_path: str) -> float:
    total = 0
    for suffix in ("", "-wal", "-shm"):
        p = db_path + suffix
        if os.path.exists(p):
            total += os.path.getsize(p)
    return round(total / 1_048_576, 1)


def _db_parts_mb(db_path: str) -> dict:
    """Main file and WAL separately: the WAL can balloon under sustained writes
    when readers keep overlapping them (checkpoints cannot reset it)."""
    def mb(p: str) -> float:
        return round(os.path.getsize(p) / 1_048_576, 1) if os.path.exists(p) else 0.0

    return {"db_file_mb": mb(db_path), "wal_mb": mb(db_path + "-wal")}


async def run_bench(accounts: int, channels: int, programmes: int, extra: int,
                    workdir: Path, trace_memory: bool = False, cycles: int = 0,
                    prune: bool = True) -> dict:
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.api import live
    from app.config import settings
    from app.models.database import Base, EpgEntry
    from app.services import epg_sync_service
    from app.services.xtream_service import xtream_service

    original_now_ms = epg_sync_service.now_ms
    original_download = xtream_service.download_xmltv
    original_prune = epg_sync_service._prune_ended

    db_path = str(workdir / "bench.db")
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.close()

    api_engine = _make_engine(db_path, pool_size=20, max_overflow=30)
    worker_engine = _make_engine(db_path, pool_size=2, max_overflow=2)
    api_factory = async_sessionmaker(api_engine, class_=AsyncSession, expire_on_commit=False)
    worker_factory = async_sessionmaker(worker_engine, class_=AsyncSession, expire_on_commit=False)

    result: dict = {
        "config": {"accounts": accounts, "channels_per_account": channels,
                   "programmes_in_window_per_channel": programmes,
                   "programmes_outside_window_per_channel": extra,
                   "batch_size": settings.EPG_SYNC_BATCH_SIZE,
                   "window_hours": [-settings.EPG_SYNC_PAST_HOURS, settings.EPG_SYNC_FUTURE_HOURS]},
    }
    try:
        async with api_engine.begin() as conn_:
            await conn_.run_sync(Base.metadata.create_all)
        server_ids = await _seed(api_factory, accounts, channels)

        clock = {"now": int(time.time() * 1000)}
        epg_sync_service.now_ms = lambda: clock["now"]  # one clock for generation and sync
        if not prune:
            async def _no_prune(*_a, **_k):
                return 0

            epg_sync_service._prune_ended = _no_prune
        xml_files = {}

        def _generate() -> list[int]:
            out = []
            for a in range(accounts):
                path = workdir / f"bench{a}.xml"
                out.append(write_xmltv(path, clock["now"], channels, programmes, extra))
                xml_files[f"bench{a}"] = path
            return out

        result["xmltv_bytes_per_account"] = _generate()

        async def _fake_download(acc, dest, *, max_bytes, total_timeout, **_):
            shutil.copyfile(xml_files[acc.id], dest)
            return os.path.getsize(dest)

        xtream_service.download_xmltv = _fake_download  # type: ignore[method-assign]

        # Idle baseline for the probe (empty epg table: the cheapest case).
        base_reads: list[float] = []
        for _ in range(20):
            t0 = time.perf_counter()
            async with api_factory() as s:
                await live.get_epg_now(server_id=server_ids[0], limit=500, offset=0, db=s)
            base_reads.append(time.perf_counter() - t0)
        result["idle_read_before_sync"] = _stats_ms(base_reads)
        result["db_size_mb_before"] = _db_size_mb(db_path)

        if trace_memory:
            tracemalloc.start()
        result["cold_run"] = await _run_once("cold (all inserts)", api_factory, worker_factory, server_ids[0])
        if trace_memory:
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            result["cold_run"]["tracemalloc_peak_python_heap_mb"] = round(peak / 1_048_576, 1)

        async with api_factory() as s:
            result["epg_entries_rows"] = (await s.execute(
                select(func.count()).select_from(EpgEntry))).scalar()
        result["db_size_mb_after_cold"] = _db_size_mb(db_path)
        result.update({f"{k}_after_cold": v for k, v in _db_parts_mb(db_path).items()})

        # GET /api/live/epg without planner statistics, then after ANALYZE
        # (production runs ANALYZE in migration 023 and PRAGMA optimize at the
        # end of every pipeline run, so the second figure is the realistic one).
        no_stats: list[float] = []
        for _ in range(20):
            t0 = time.perf_counter()
            async with api_factory() as s_:
                await live.get_epg_now(server_id=server_ids[0], limit=500, offset=0, db=s_)
            no_stats.append(time.perf_counter() - t0)
        result["idle_read_full_table_no_planner_stats"] = _stats_ms(no_stats)
        from sqlalchemy import text as _text

        async with api_engine.begin() as conn_:
            await conn_.execute(_text("ANALYZE"))

        result["warm_run"] = await _run_once("warm (all unchanged)", api_factory, worker_factory, server_ids[0])
        result["db_size_mb_after_warm"] = _db_size_mb(db_path)

        idle_reads: list[float] = []
        for _ in range(20):
            t0 = time.perf_counter()
            async with api_factory() as s:
                await live.get_epg_now(server_id=server_ids[0], limit=500, offset=0, db=s)
            idle_reads.append(time.perf_counter() - t0)
        result["idle_read_full_table_with_analyze"] = _stats_ms(idle_reads)

        cycle_results = []
        for k in range(1, cycles + 1):
            clock["now"] += 6 * 3_600_000          # one more cron tick
            _generate()
            r = await _run_once(f"cycle {k} (+{6 * k}h)", api_factory, worker_factory, server_ids[0])
            async with api_factory() as s_:
                rows_now = (await s_.execute(select(func.count()).select_from(EpgEntry))).scalar()
            cycle_results.append({
                "run": r["run"], "duration_s": r["duration_s"], "inserted": r["inserted"],
                "unchanged": r["unchanged"], "pruned": r["pruned"], "epg_entries_rows_after": rows_now,
                "db_size_mb": _db_size_mb(db_path),
                **_db_parts_mb(db_path),
                "read_p95_ms": r["concurrent_read_get_epg_now"]["p95_ms"],
                "write_p95_ms": r["concurrent_write"]["p95_ms"],
            })
        if cycle_results:
            result["cycles"] = cycle_results
    finally:
        epg_sync_service.now_ms = original_now_ms
        epg_sync_service._prune_ended = original_prune
        xtream_service.download_xmltv = original_download  # type: ignore[method-assign]
        await api_engine.dispose()
        await worker_engine.dispose()
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--accounts", type=int, default=2)
    ap.add_argument("--channels", type=int, default=5000)
    ap.add_argument("--programmes", type=int, default=20, help="in-window programmes per channel")
    ap.add_argument("--extra", type=int, default=40, help="out-of-window programmes per channel")
    ap.add_argument("--cycles", type=int, default=0,
                    help="extra runs, clock +6 h each (steady-state / pruning check)")
    ap.add_argument("--no-prune", action="store_true",
                    help="disable the post-sync prune, to quantify what it saves")
    ap.add_argument("--tracemalloc", action="store_true", help="also report the peak Python heap")
    ap.add_argument("--keep", action="store_true", help="keep the temp directory")
    args = ap.parse_args()

    workdir = Path(tempfile.mkdtemp(prefix="plexhub-epgbench-"))
    try:
        out = asyncio.run(run_bench(args.accounts, args.channels, args.programmes, args.extra,
                                    workdir, trace_memory=args.tracemalloc,
                                    cycles=args.cycles, prune=not args.no_prune))
        print(json.dumps(out, indent=2))
    finally:
        if not args.keep:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
