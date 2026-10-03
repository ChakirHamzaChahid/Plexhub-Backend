"""Smoke test for `scripts/bench_epg_sync.py` at a tiny scale, so the benchmark
used to size the bulk EPG sync (ADR 0006) cannot silently rot. The real scale
run (>= 5 000 channels x 2 accounts) is a manual command documented in the
script's docstring, deliberately NOT part of the suite."""
from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "bench_epg_sync.py"


def _load():
    spec = importlib.util.spec_from_file_location("bench_epg_sync", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_bench_runs_end_to_end_at_tiny_scale(tmp_path, monkeypatch):
    bench = _load()
    out = await bench.run_bench(
        accounts=2, channels=30, programmes=5, extra=3, workdir=tmp_path, trace_memory=False,
    )

    # 2 accounts x 30 channels x (5 or 6 slots overlapping the window); the 3
    # extra per channel are outside the window and must be discarded.
    rows = out["epg_entries_rows"]
    assert 2 * 30 * 5 <= rows <= 2 * 30 * 6
    cold, warm = out["cold_run"], out["warm_run"]
    assert (cold["inserted"], cold["accounts_failed"]) == (rows, 0)
    assert (warm["inserted"], warm["updated"], warm["unchanged"]) == (0, 0, rows)
    assert cold["probe_errors"] == [] and warm["probe_errors"] == []
    assert all(a["skipped_out_of_window"] == 30 * 3 for a in cold["per_account"])
    assert out["idle_read_full_table_with_analyze"]["n"] == 20
    assert out["idle_read_full_table_no_planner_stats"]["n"] == 20


async def test_bench_cycles_keep_the_table_at_window_size_thanks_to_pruning(tmp_path):
    bench = _load()
    out = await bench.run_bench(
        accounts=1, channels=20, programmes=4, extra=0, workdir=tmp_path, cycles=3, prune=True,
    )
    # 3 more cron ticks (+6h each): programmes shift out of the window, the
    # ended ones are pruned, so the table does not grow with the number of runs.
    rows = [cy["epg_entries_rows_after"] for cy in out["cycles"]]
    assert max(rows) <= 20 * 5 * 2, rows
    assert sum(cy["pruned"] for cy in out["cycles"]) > 0


def test_percentile_helpers():
    bench = _load()
    assert bench._pct([], 0.5) == 0.0
    assert bench._pct([1.0, 2.0, 3.0], 0.5) == 2.0
    assert bench._stats_ms([0.010, 0.020])["max_ms"] == 20.0
