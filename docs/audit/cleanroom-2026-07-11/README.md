# Clean-room Audit — PlexHub Backend — 2026-07-11

**Scope:** full independent 360° diagnostic of the FastAPI backend, deliberately **un-anchored** from any
prior audit (nothing under `docs/audit/**` was read; `CLAUDE.md` §10 "dette" was treated as *unverified* and
every claim re-derived from code at this HEAD).
**HEAD:** `develop` @ `1879f83` (v1.1.5) · **Host:** Windows 11, Python 3.13 · **Method:** 6 parallel
read-only dimension auditors + CI-equivalent `pytest` + in-process ASGI latency probe.
**Result banner:** `pytest` **484 passed / 1 deselected** ✅ · sqlite-vec loads · auth confirmed **fail-closed**
empirically · **56 findings** across 6 dimensions (**1 P0, 17 P1, 26 P2, 12 debt**).

> This report set is a fresh cartography. It is the intended input for the later `ARCHITECTURE.md` / `CLAUDE.md`
> refresh (separate step — not performed here). No code was changed; this run is read-only + reports only.

---

## Scorecard

| Dimension | Score | One-line justification | Report |
|---|:---:|---|---|
| **Security** | **3.5 / 5** | Auth is genuinely fail-closed & constant-time (the §10 "catalogue open" claim is **stale/false** now); one post-auth arbitrary-FS-write P1 + hardening gaps. | [40-security.md](40-security.md) |
| **Architecture** | **3 / 5** | Solid layered bones + a real shared dedup core, undercut by logic-in-routers, triplicated Plex-gen orchestration, and god-files. | [10-architecture.md](10-architecture.md) |
| **Conventions** | **3 / 5** | Strong Pydantic/camelCase discipline + exemplary AI router; gaps: event-loop-blocking writes, a snake_case leak, zero lint tooling. | [20-conventions.md](20-conventions.md) |
| **Data-flows** | **3 / 5** | Fundamentally sound incremental design (locks, savepoints, identity preservation); 5 P1 hazards bite under normal provider churn. | [30-dataflows.md](30-dataflows.md) |
| **Tests / coverage** | **3 / 5** | Broad green suite (484) with deep AI/generator/dedup coverage; but critical gates & core orchestration untested, and 1 masked real bug. | [60-tests.md](60-tests.md) |
| **Perf / scalability** | **2.5 / 5** | Single-item/index/worker paths healthy; the flagship deduped-browse endpoints have an O(catalog)-per-request design flaw. | [50-perf.md](50-perf.md) |
| **Overall** | **≈ 3 / 5** | Healthy, shippable core with **no active data-corruption on the happy path**, but a scale cliff on the primary browse path, several churn-triggered correctness hazards, one exploitable FS-write, and thin safety nets on the exact things that would ship a regression green. | — |

Severity mix: **1 P0 · 17 P1 · 26 P2 · 12 debt** (56 total). Benchmark & method: [00-benchmark.md](00-benchmark.md).

---

## Top-10 priorities (cross-dimension, severity × blast-radius)

| # | ID | Sev | Title | Evidence | Why it's top-ranked |
|:--:|---|:--:|---|---|---|
| 1 | **CR-P01** | **P0** | `/movies\|shows/unified` load the **entire** catalog into memory and aggregate synchronously on the event loop; `limit/offset` sliced *after* the full load | `services/media_service.py:143-147` | These are the **primary Android browse endpoints**. At 10⁴+ rows = hundreds-of-ms→multi-second **event-loop stalls** + O(catalog) memory per in-flight request. Verified against code. |
| 2 | **CR-S01** | P1 | Post-auth **arbitrary filesystem write / path traversal** via client `outputDir` on `POST /api/plex/generate` (→ `Path()` → `LocalStorage`, no confinement) | `api/plex.py:39-56` → `plex_generator/storage.py` | Any active **per-user** key (not just master) can write attacker-named files anywhere writable and **exfiltrate other accounts' Xtream credentials** (embedded in `.strm` URLs). Verified. |
| 3 | **CR-F02** | P1 | Page-offset eviction in `upsert_media_batch` deletes **unchanged, still-listed** rows when a provider reorders a category → rows vanish ≤6h & **lose enrichment** (`tmdb_id`/`unification_id` revert) on re-insert | `workers/sync_worker.py:557-576` | Silent **data/enrichment loss** on ordinary provider churn. Verified against code. |
| 4 | **CR-T02** | P1† | The fail-closed guard on the **entire** JSON catalogue/sync/plex API has **zero rejection tests** (`grep verify_backend_secret tests/` = 0) | `main.py:396-405` | The whole security posture rests on one `dependencies=_guard`; dropping it **ships green**. Cheapest highest-leverage fix. (†borderline P0.) |
| 5 | **CR-T01** | P1 | The permanently-deselected "flaky" base64 test masks a **real live bug**: `_try_base64_decode` mangles common EPG titles (`News/Test/Info/Kids` → garbage) — rejects control chars but not `U+FFFD` | `api/live.py` `_try_base64_decode` (used `:221,224`) | **Reproduced this session.** CI hides a real bug and mislabels it flaky. |
| 6 | **CR-F04** | P1 | Boot pipeline run and interval job share **no mutual exclusion** (`max_instances=1` governs only the interval) | `main.py:328-338` vs `:249-274` | Slow first boot → concurrent enrichment/generation → **double TMDB spend** + race on the generated tree / `.plex_mapping.json`. |
| 7 | **CR-P02** | P1 | Most ORM-declared `media` indexes are created only by `create_all` on a **fresh** table; migrations ensure just 4 → **upgraded DBs silently lack** hot list/sort indexes | `models/database.py:105-124` vs `db/migrations.py`, `db/database.py:92` | Full scans + filesorts on the hot list/sort queries in prod; invisible in the empty-DB floor. |
| 8 | **CR-F03** | P1 | `ENRICHMENT_DAILY_LIMIT` counts **logical** items, not `_request` retries (≤4×), and resets every run | `workers/enrichment_worker.py`, `tmdb_service._request` | Real TMDB spend can exceed the "limit" **2–4×** under rate-limiting → quota/ban risk. |
| 9 | **CR-F01** | P1 | **Episodes are never differential-cleaned** (`differential_cleanup*` = movie/show/live only) | `workers/sync_worker.py`, `category_service.py:348-377` | Delisted/renumbered episodes **orphan forever** → unbounded drift. |
| 10 | **CR-C01** | P1 | `PlexLibraryGenerator.generate()` runs fsync `.strm`/`.nfo` writes + `mapping.save()` + orphan `rglob`/`rmtree` **directly on the event loop** (only images offloaded) | `plex_generator/generator.py` | **Event-loop starvation** at boot and on every scheduled pipeline. Same root as CR-P01: heavy work on the loop. |

**Honorable mentions (P1/high-P2):** `CR-A01`/`A02`/`A03` (logic-in-routers, triplicated orchestration + `sync.py:74` importing a private `app.main` symbol, god-files) · `CR-F05` (`/unified?unification_id=` under-reports converged twins) · `CR-C02` (snake_case `vod_count`/`series_count` leak) · `CR-P03` (leading-wildcard `ILIKE` + `COUNT`-over-`SELECT *` double full scan) · `CR-T03`/`T04` (sync_worker orchestration & startup wiring untested).

---

## Convergent findings (independently corroborated across auditors — higher confidence)

- **Heavy work on the event loop** — `CR-P01` (perf) + `CR-C01` (conventions) hit the same root from two lanes:
  the unified-browse and library-generation paths do Python-side aggregation / fsync I/O on the loop.
- **Schema truth duplicated ORM↔migrations** — `CR-P02` (missing indexes on upgraded DBs) + `CR-C05`
  (duplicate-column warnings on fresh boot) are two symptoms of the same duplication (also seen live in the benchmark).
- **The auth model is right but unguarded by tests** — Security confirmed fail-closed & constant-time
  (eliminating the old auth-bypass P0 class), while Tests found **zero** rejection tests protecting it
  (`CR-T02`) and the `outputDir` FS-write path untested (`CR-S01` + `CR-T07`).
- **`/plex/generate` is a hotspot in two lanes** — `CR-S01` (arbitrary write) + `CR-P05` (materializes whole
  catalog) + `CR-T07` (untested).

---

## Roadmap

### Batch 1 — Ship-blockers (P0 + the cheap high-leverage guards)
- **CR-P01** — paginate/aggregate the `/unified` endpoints **in SQL** (or cache the grouped result + invalidate on sync); stop loading the full catalog per request. *Biggest single win.*
- **CR-T02** — add auth-**rejection** tests (401 without/with wrong `X-API-Key`) across the guarded routers — a few cheap tests that permanently protect the whole fail-closed model.
- **CR-S01** — confine `outputDir` to an allow-listed base (reject absolute/`..`/outside `PLEX_LIBRARY_DIR`); consider requiring master key for `/plex/generate`.

### Batch 2 — P1 correctness & scale
- **CR-F02** (fix reorder-eviction: key on identity, not page slot) · **CR-F01** (episode differential cleanup) ·
  **CR-F03** (count real HTTP calls incl. retries; persist daily counter) · **CR-F04** (single mutex across boot + interval pipeline) ·
  **CR-T01** (fix `_try_base64_decode` to reject `U+FFFD`, then un-deselect the test) ·
  **CR-P02** (migration to create the missing composite indexes; make ORM↔migration the single source) ·
  **CR-C01** (offload generator fsync/rmtree via `asyncio.to_thread`) · **CR-P03** (search: trigram/FTS or prefix; avoid `COUNT(SELECT *)`).

### Batch 3 — Security hardening & contract hygiene (P2)
- Security: **CR-S02** (auth `/metrics`) · **CR-S03** (encrypt Xtream creds / backups at rest) · **CR-S05** (rate limiting) ·
  **CR-S06** (explicit CORS in prod) · **CR-S07** (CSRF token on `/admin`) · **CR-S08** (SSRF allow-list) · **CR-S04** (separate Fernet key).
- Contracts: **CR-C02** (kill snake_case leak) · **CR-C03** (typed `response_model` on `sync`/`categories`/`media`) ·
  **CR-F05/F06/F07/F08/F09/F11** (unified-by-id twins, tv-auth camelCase, atomic one-shot delivery, breaker tuning, deterministic convergence, episode re-sync).

### Batch 4 — Tooling & debt (foundation for everything above)
- **CR-C06 / CR-T09 / CR-T10** — wire **ruff + black + mypy + pytest-cov** and a coverage gate; run CI on **`develop`** (not just `main`).
- **CR-A01/A02/A03/A05** — extract router business logic into services, unify the triplicated Plex-gen orchestration, break up the god-files.
- **CR-C05/C07/C08/C09/C10, CR-P07/P08, CR-F10, CR-T11** — schema-dedup, dead code, deprecated `HTTP_422`, misc.

---

## Files in this audit
| File | Contents |
|---|---|
| [00-benchmark.md](00-benchmark.md) | `pytest` result, ASGI latency floor, fail-closed proof, `fcntl`/Windows-boot + migration-warning observations |
| [10-architecture.md](10-architecture.md) | `CR-A01…A07` (7) — layering, orchestration duplication, god-files |
| [20-conventions.md](20-conventions.md) | `CR-C01…C10` (10) — event-loop writes, snake_case leak, lint gaps |
| [30-dataflows.md](30-dataflows.md) | `CR-F01…F11` (11) — sync/enrich/validate/dedup/tv-pairing correctness |
| [40-security.md](40-security.md) | `CR-S01…S09` (9) — FS-write, secrets-at-rest, CORS/CSRF/SSRF, rate-limit |
| [50-perf.md](50-perf.md) | `CR-P01…P08` (8) — unified-endpoint scale cliff, missing indexes, scans |
| [60-tests.md](60-tests.md) | `CR-T01…T11` (11) — masked bug, untested gates & orchestration, no coverage gate |

*Next step (separate): feed this cartography into a `/refresh-context` or dedicated pass to update
`ARCHITECTURE.md` / `CLAUDE.md`. Findings can be remediated via `/fix-cleanroom` (P0→debt order).*
