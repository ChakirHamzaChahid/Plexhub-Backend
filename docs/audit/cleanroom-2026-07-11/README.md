# Clean-room Audit — PlexHub Backend — 2026-07-11

**Scope:** full independent 360° diagnostic of the FastAPI backend, deliberately **un-anchored** from any
prior audit (nothing under `docs/audit/**` was read; `CLAUDE.md` §10 "dette" was treated as *unverified* and
every claim re-derived from code at this HEAD).
**HEAD:** `develop` @ `1879f83` (v1.1.5) · **Host:** Windows 11, Python 3.13 · **Method:** 6 parallel
read-only dimension auditors + CI-equivalent `pytest` + in-process ASGI latency probe.
**Result banner:** `pytest` **484 passed / 1 deselected** ✅ · sqlite-vec loads · auth confirmed **fail-closed**
empirically · **56 findings** across 6 dimensions (**1 P0, 17 P1, 26 P2, 12 debt**).

> This report set is a fresh cartography. It is the intended input for the later `ARCHITECTURE.md` / `CLAUDE.md`
> refresh (separate step — not performed here). The audit itself was read-only; remediation status is tracked below.

---

## Remediation status (`/fix-cleanroom`, updated 2026-07-11)

**Round 1 — RESOLVED** (fix + guard test + code-review & security-review gate + full suite `528 passed`):

| ID | Sev | Fix | Note |
|---|:--:|---|---|
| CR-P01 | P0 | `aggregate_movies`/`aggregate_series`+sort → `asyncio.to_thread` (`media_service.py`) | **Partial**: event-loop stall removed; full-catalog-load SQL-pagination redesign still open (tracked in-code). |
| CR-S01 | P1 | `outputDir` confined to `PLEX_LIBRARY_DIR` via `Path.parents` check (`plex.py`) | Security-reviewer verified no bypass. |
| CR-F02 | P1 | Slot-conflicting rows **relocated** (per-partition `MAX+1`), not deleted (`sync_worker.py`) | Collision-free across syncs (revised after review). |
| CR-T01 | P1 | `_try_base64_decode` rejects `U+FFFD` (`live.py`); CI `--deselect` removed | Real EPG bug; test now runs. |
| CR-T02 | P1 | New `tests/test_auth_guard.py` — 401 net over 6 guarded routers + public `/health` | Regression net for fail-closed auth. |
| CR-F04 | P1 | `_PIPELINE_LOCK` (skip-if-locked) on boot + interval pipeline (`main.py`) | — |
| CR-C01 | P1 | Generator write/prune/`mapping.save` → `asyncio.to_thread` (`generator.py`) | Ordering preserved. |
| CR-C02 | P1 | Typed `CategoryRefreshResponse` camelCase (`categories.py`/`schemas.py`) | ⚠️ **Breaking wire change** — Android must read `vodCount`/`seriesCount`. |
| CR-S06 | P2 | CORS explicit methods/headers + wildcard-origin warning (`main.py`) | `X-API-Key` preserved. |
| CR-P02 | P1 | Migration 015 backfills 16 missing `media` indexes idempotently (chain 001→015) | Non-destructive schema migration. |

**Round 2 — RESOLVED** (fix + tests + security-review gate + full suite `533 passed`, `ruff` green):

| ID | Sev | Fix | Note |
|---|:--:|---|---|
| CR-S03 | P2 | `EncryptedString` `TypeDecorator` on `XtreamAccount.password` (Fernet) + migration 016 to encrypt existing rows | Dedicated `XTREAM_ENCRYPTION_KEY` (domain-separated `AI_API_KEY` fallback); **fail-open** if no key (documented). Security-review APPROVED. Backups now ciphertext. |
| CR-C06 | debt | Wired `ruff`+`black`+`mypy`+`pytest-cov` (`pyproject.toml`, `requirements-dev.txt`) | Conservative ruleset → `ruff check` green with no mass reformat; coverage report-only. |
| CR-T10 | debt | CI (`tests.yml`) now triggers on `develop` + a `ruff`/`black` lint job | — |
| CR-C07 | debt | Removed unused `pydantic-settings` from `requirements.txt` | — |
| CR-C08 | debt | Deleted dead `sanitize_edition_label`/`_EDITION_INVALID_CHARS` (`naming.py`) | — |
| CR-T11 | debt | Removed stray `@pytest.mark.asyncio` marks (`test_embedding_worker.py`) | — |

**CR-C09 — WON'T FIX (blocked by pinned stack):** `HTTP_422_UNPROCESSABLE_CONTENT` does not exist in the pinned Starlette 0.46.2 (`fastapi>=0.115,<0.116`, deliberately pinned). The rename was attempted and **reverted** after it caused `AttributeError`/500 on the 422 paths. The deprecation warning is benign and can only be resolved by a stack bump (forbidden by the fastapi/instrumentator pin coupling).

**Vague A — RESOLVED** (`/incident` bugs de flux ; full suite `570 passed`, ruff vert, code-review OK) :

| ID | Sev | Fix | Note |
|---|:--:|---|---|
| CR-F01 | P1 | `differential_cleanup_episodes` (scopé show+serveur) ; prune seulement si `success and rows` | Empty-200 soft-failure ne supprime plus (garde ajoutée en review). |
| CR-F11 | P2 | Fetch épisodes découplé du hash show (toutes séries actives) | ⚠️ +1 appel `get_series_info`/série/sync — charge Xtream accrue (à monitorer). |
| CR-F03 | P1 | `tmdb_service._request` compte chaque tentative HTTP réelle ; budget piloté par appels réels | Persistance quotidienne = résidu documenté. |
| CR-F06 | P2 | `/tv-auth/status` accepte `deviceCode` (+ `device_code` legacy) | — |
| CR-F07 | P2 | Livraison one-shot atomique (`UPDATE … WHERE payload_delivered=false` + `rowcount==1`) | — |
| CR-F08 | P2 | Circuit breaker roulant (min-sample 10, ratio à chaque check) + fix collatéral `expunge_all` (MissingGreenlet) | — |
| CR-P06 | P2 | Sampling health par plage `rowid` (fini `ORDER BY random()`) | — |
| CR-F09 | P2 | Convergence Passe B déterministe (`min(_key_rank)`) | — |
| CR-T05 | P2 | 20 tests health-check (breaker + `_check_one` + sampling) | — |

**Vague B — RESOLVED** (`/incident` perf ; full suite `585 passed`, ruff vert, code-review APPROVED) :

| ID | Sev | Fix | Note |
|---|:--:|---|---|
| CR-P01 *(résidu)* | P0 | Cache TTL (45 s, cap 12) du groupement unifié, invalidé par empreinte `COUNT+MAX(updated_at)` | Le stall event-loop était déjà réglé (offload). Reste résiduel : windowing SQL vrai + writes enrichment/is_broken sans bump `updated_at` (staleness ≤ TTL). |
| CR-F05 | P2 | `get_unified_group` charge un pool de candidats borné + `_converge` → renvoie toutes les versions convergées | Garde homonyme intacte. |
| CR-P03 | P1 | COUNT via `select(func.count()).select_from(...)` (fini le COUNT sur sous-requête `SELECT *`) (media + live) | ILIKE leading-wildcard = résidu (FTS). |
| CR-P05 | P2 | `run_pipeline_validation` streame **par compte** (`yield_per`) | `source.py` : matérialisation by-design (groupement whole-set), documentée. |
| CR-P08 | debt | Over-fetch KNN escaladé 200→2000 (≤2 requêtes) robuste au skew de type | Résidu : skew extrême > 2000. |

**Résiduels perf (dans `api/media.py`, non traités) :** CR-P04 (deep OFFSET → keyset = changement de contrat), CR-P07 (double-passe Pydantic grande page).

**Vague C1 — RESOLVED** (`/refacto` contenu ; full suite `601 passed`, ruff vert, code-review APPROVED) :

| ID | Sev | Fix | Note |
|---|:--:|---|---|
| CR-C03 | P2 | 7 endpoints `sync.py` typés (`JobIdResponse`/`MessageResponse`/`SyncJobResponse`/`SyncJobListResponse`) | ⚠️ `GET /sync/jobs` : `job_id`→`jobId` (endpoint ops). `media.py:353` = résidu. |
| CR-C04 | P2 | `commit_with_retry` sur ~13 writes du chemin requête (tv-auth/live/accounts/categories + service) | Sémantique inchangée (retry lock only). |
| CR-C05 | P2 | Probe `_column_exists` avant `ADD COLUMN` → 0 warning duplicate-column (contre 20) | — |
| CR-C10 *(migration)* | debt | `_migration_008` remis à sa position numérique (ordre d'exécution inchangé) | Dédup `TempAccount`/`_Acc` = reste à faire. |
| CR-A07 | debt | `build_versions` unifié dans `aggregation_service` (media + generator) | — |
| CR-F10 | debt | Filtre `is_broken` déplacé **après** l'agrégation (générateur groupe comme l'API ; seules versions saines publiées) | ⚠️ 1ʳᵉ génération après ce commit : re-pick possible du `best_row` → renommage dossier/NFO one-shot. |

**Vague C2 — RESOLVED** (`/refacto` moyen ; full suite `631 passed`, ruff vert, code-review APPROVED) :

| ID | Sev | Fix | Note |
|---|:--:|---|---|
| CR-A01 | P1 | Logique métier extraite des routers → `account_service`/`live_service`/`category_service` (routers −135 l.) | Frontière transactionnelle gardée au routeur (préserve retry C04). Résidu : `stream.py` server_id, `list_channels`/`get_epg_now` pagination. |
| CR-A02 | P1 | Orchestration Plex-gen unifiée dans `plex_generation_service` (`generate_plex_library`[+`_auto`]) ; `sync.py` n'importe plus `app.main` | Garde-fous « skip si non configuré / pas de comptes actifs » préservés. |
| CR-A04 | P2 | Montage routers étiqueté Pattern A/B/C (auth par-routeur explicite) | Centralisation complète (assertion `app.routes`) = follow-up. |
| CR-A06 | P2 | Symboles privés cross-module rendus publics (`serialize_vec`, `movie_folder`/`series_folder`) | Job stores process-local = limitation archi notée. |
| CR-C10 | debt | `TempAccount`+`_Acc` → dataclass partagée `XtreamCredentials` ; `_migration_008` déjà remis en ordre (C1) | Résolu en entier. |

**Gardé pour un effort dédié (refacto lourd, approuvé « plus tard ») :** CR-A03 (décomposer god-files `sync_worker` 1390 / `ai` 1228 / `nfo_import` 888) · CR-A05 (slim `main.py`).

**Follow-ups noted (not yet done):** CR-P01 full SQL-side pagination redesign · `/api/plex/generate` behind `verify_master_key` (defense-in-depth) · CR-S02/S04/S05/S07/S08 (sécurité) · **CR-A03/A05 (god-files, main.py)** · CR-P04/P07 (api/media.py) · CR-T03–T09 (tests — Vague D en cours).

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
| 7 | **CR-P02** | P1 — **RÉSOLU 2026-07-11** | Most ORM-declared `media` indexes are created only by `create_all` on a **fresh** table; migrations ensure just 4 → **upgraded DBs silently lack** hot list/sort indexes | `models/database.py:105-124` vs `db/migrations.py`, `db/database.py:92` | Full scans + filesorts on the hot list/sort queries in prod; invisible in the empty-DB floor. **Fix:** migration 015 (`db/migrations.py`) backfills all 16 missing indexes idempotently; chain now 001→015; proof in `tests/test_media_indexes_migration.py`. |
| 8 | **CR-F03** | P1 | `ENRICHMENT_DAILY_LIMIT` counts **logical** items, not `_request` retries (≤4×), and resets every run | `workers/enrichment_worker.py`, `tmdb_service._request` | Real TMDB spend can exceed the "limit" **2–4×** under rate-limiting → quota/ban risk. |
| 9 | **CR-F01** | P1 | **Episodes are never differential-cleaned** (`differential_cleanup*` = movie/show/live only) | `workers/sync_worker.py`, `category_service.py:348-377` | Delisted/renumbered episodes **orphan forever** → unbounded drift. |
| 10 | **CR-C01** | P1 | `PlexLibraryGenerator.generate()` runs fsync `.strm`/`.nfo` writes + `mapping.save()` + orphan `rglob`/`rmtree` **directly on the event loop** (only images offloaded) | `plex_generator/generator.py` | **Event-loop starvation** at boot and on every scheduled pipeline. Same root as CR-P01: heavy work on the loop. |

**Honorable mentions (P1/high-P2):** `CR-A01`/`A02`/`A03` (logic-in-routers, triplicated orchestration + `sync.py:74` importing a private `app.main` symbol, god-files) · `CR-F05` (`/unified?unification_id=` under-reports converged twins) · `CR-C02` (snake_case `vod_count`/`series_count` leak) · `CR-P03` (leading-wildcard `ILIKE` + `COUNT`-over-`SELECT *` double full scan) · `CR-T03`/`T04` (sync_worker orchestration & startup wiring untested).

---

## Convergent findings (independently corroborated across auditors — higher confidence)

- **Heavy work on the event loop** — `CR-P01` (perf) + `CR-C01` (conventions) hit the same root from two lanes:
  the unified-browse and library-generation paths do Python-side aggregation / fsync I/O on the loop.
- **Schema truth duplicated ORM↔migrations** — `CR-P02` (missing indexes on upgraded DBs, **RÉSOLU 2026-07-11** via migration 015) + `CR-C05`
  (duplicate-column warnings on fresh boot, **RÉSOLU 2026-07-11** via `_column_exists` probing) are two symptoms of the same
  duplication (also seen live in the benchmark) — both now converge create_all (fresh DB) and the migration chain (upgraded DB)
  on the identical schema without noise.
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
  ~~**CR-P02**~~ (**RÉSOLU 2026-07-11** — migration 015 creates the missing composite indexes; ORM↔migration converge) ·
  **CR-C01** (offload generator fsync/rmtree via `asyncio.to_thread`) · **CR-P03** (search: trigram/FTS or prefix; avoid `COUNT(SELECT *)`).

### Batch 3 — Security hardening & contract hygiene (P2)
- Security: **CR-S02** (auth `/metrics`) · **CR-S03** (encrypt Xtream creds / backups at rest) · **CR-S05** (rate limiting) ·
  **CR-S06** (explicit CORS in prod) · **CR-S07** (CSRF token on `/admin`) · **CR-S08** (SSRF allow-list) · **CR-S04** (separate Fernet key).
- Contracts: **CR-C02** (kill snake_case leak) · **CR-C03** (typed `response_model` on `sync`/`categories`/`media`) ·
  **CR-F05/F06/F07/F08/F09/F11** (unified-by-id twins, tv-auth camelCase, atomic one-shot delivery, breaker tuning, deterministic convergence, episode re-sync).

### Batch 4 — Tooling & debt (foundation for everything above)
- **CR-C06 / CR-T09 / CR-T10** — wire **ruff + black + mypy + pytest-cov** and a coverage gate; run CI on **`develop`** (not just `main`).
- ~~**CR-A01**~~ (**PARTIELLEMENT RÉSOLU 2026-07-12** — `live_service`/`account_service` (nouveaux) + `category_service.refresh_categories_from_provider` extraits de `live.py`/`accounts.py`/`categories.py`, `server_id` parsing dédupliqué via `parse_server_id` ; résiduel : `live.py` listing/pagination inline (faible priorité) + `stream.py:20-32` — hors scope de cette passe ; voir [10-architecture.md](10-architecture.md#cr-a01)) · **CR-A02/A03/A05** — unify the triplicated Plex-gen orchestration, break up the god-files.
- ~~**CR-C05**~~ (**RÉSOLU 2026-07-11** — `_column_exists` probe before every `ADD COLUMN`; 0 duplicate-column
  warnings on fresh boot, proof in `tests/test_migrations_no_duplicate_warning.py`) · ~~**CR-C10** (migration-008
  ordering half)~~ (**RÉSOLU 2026-07-11** — definition moved to its numeric position, execution order unchanged) ·
  **CR-C10 (`TempAccount` half, 2026-07-12)** RÉSOLU — `accounts.py`'s throwaway `TempAccount` replaced by the shared
  `app.services.xtream_credentials.XtreamCredentials` dataclass (see CR-A01 above); `main.py`'s `_Acc` half still open ·
  **CR-C07/C08/C09, CR-P07/P08, CR-F10, CR-T11** — remaining
  dead code, deprecated `HTTP_422`, misc.

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
