# Plan de remédiation — Audit Clean-Room 360° (2026-11-14)

> **Source :** [docs/audit/cleanroom-2026-11-14/README.md](../audit/cleanroom-2026-11-14/README.md) · **Baseline :** `develop` @ `6cb09fa` (`APP_VERSION = "1.15.0"`)
> **Règle d'exécution :** un lot par session via `/lot docs/plans/2026-11-14-audit-cleanroom-remediation-plan.md`, un rédacteur à la fois, découpage par couche (≤ 2 paquets/ticket).

---

## Table des lots

| Lot | Thème | Priorité | Findings couverts | Statut |
|---|---|---|---|---|
| **Lot 1** | Sécurité & I/O critique (Credentials Xtream, SSRF, I/O download) | P0-A | `CR-S01`, `CR-S02`, `CR-S03`, `CR-S04`, `CR-A03` | ✅ Fait |
| **Lot 2** | Contention SQLite & Stabilité Boot/Health | P0-B | `CR-D01`, `CR-D02`, `CR-D03`, `CR-D04`, `CR-O01` | ✅ Fait |
| **Lot 3** | Performance DB & I/O Services | P1-A | `CR-P01`, `CR-P02`, `CR-P05`, `CR-P06`, `CR-A04` | ✅ Fait |
| **Lot 4** | Observabilité, Docker, Tests critiques & Durcissement | P1-B | `CR-O04`, `CR-T02`, `CR-T03`, `CR-O02`, `CR-O03`, `CR-S05`..`S11` | ✅ Fait |
| **Lot 5** | Découpage Routers/Services & Effets de bord | P2-A | `CR-A01`, `CR-A02`, `CR-A06`, `CR-C01`, `CR-T04`, `CR-O05`, `CR-O06`, `CR-P04` | ✅ Fait |
| **Lot 6** | Recherche FTS5, God Files & Hygiène CI/Repo | P2-B | `CR-P03`, `CR-A05`, `CR-C02`, `CR-T01`, `CR-T05`, `CR-T06`, `CR-O07`, `CR-O08` | ✅ Fait |

---

## Lot 1 — Sécurité & I/O critique (P0-A)

- **Déclencheurs :** `security-reviewer` (SSRF, secrets/logs, downloads, `accounts.py`) + `perf-benchmarker` (event-loop I/O bloquant `download_service.py`). **Risky** (secrets, SSRF, downloads).
- **Tickets :**
  - **T1.1 (`CR-S01`, `CR-S02`) — Stopper les fuites de credentials Xtream**
    - *Périmètre :* `app/services/account_service.py`, `app/api/accounts.py`, `app/services/xtream_service.py`, `app/workers/sync_worker.py`, `app/main.py`, tests associés.
    - *Critères :* `POST /api/accounts/{id}/test` ne renvoie jamais l'URL ni `str(httpx.HTTPStatusError)` au client (uniquement statut HTTP ou nom d'exception) ; les logs dans `xtream_service.py:74`, `sync_worker.py` et `main.py:223` n'interpolent jamais `{e}` brut d'une erreur HTTP Xtream. Tests dédiés vérifiant l'absence de `username`/`password` dans les réponses 400 et les logs capturés.
  - **T1.2 (`CR-S03`, `CR-S04`) — Fermer les vecteurs SSRF**
    - *Périmètre :* `app/services/xtream_service.py`, `app/api/accounts.py`, `app/services/download_service.py`, tests SSRF.
    - *Critères :* `xtream_service._get_client()` arme `event_hooks={"request": [ssrf.vet_request]}` ; `accounts.py` valide `base_url` via `assert_public_host` (en respectant une éventuelle option explicite si IP privée homelab autorisée — à trancher au GATE) ; `download_service.download_to_disk` appelle `_assert_public_redirect_host` sur l'URL initiale avant la boucle de requêtes.
  - **T1.3 (`CR-A03`) — Déporter l'I/O disque bloquant dans `download_service`**
    - *Périmètre :* `app/services/download_service.py:536-558`, `tests/test_download_service.py`.
    - *Critères :* `f.write(chunk)`, `f.flush()`, `f.close()` exécutés via `asyncio.to_thread` (modèle `xtream_service.py:344`) sans régression sur l'écriture partielle/resume et les tests existants.

---

## Lot 2 — Contention SQLite & Stabilité Boot/Health (P0-B)

- **Déclencheurs :** `perf-benchmarker` (SQLite locks, pools, workers, `/api/health`). **Risky** (écritures concurrentes `write_with_retry`, workers, migrations).
- **Tickets :**
  - **T2.1 (`CR-D01` API/services) — Remplacer `commit_with_retry` côté API & services**
    - *Périmètre :* `app/api/live.py`, `app/api/accounts.py`, `app/api/categories.py`, `app/api/tv_auth.py`, `app/services/category_service.py`, tests associés.
    - *Critères :* Migration vers `write_with_retry(..., session_factory=...)` avec session fraîche par tentative ; test real-lock prouvant la survie à une contention `BEGIN IMMEDIATE`.
  - **T2.2 (`CR-D01` workers) — Remplacer `commit_with_retry` côté workers**
    - *Périmètre :* `app/workers/sync_worker.py`, `app/workers/enrichment_worker.py`, `app/workers/health_check_worker.py`, tests workers/real-lock.
    - *Critères :* Lots découpés en unités transactionnelles indépendantes sous `write_with_retry` (plus d'ORM détaché sur session invalidée).
  - **T2.3 (`CR-D02`) — Isoler les workers sur `worker_session_factory`**
    - *Périmètre :* `app/workers/{sync,enrichment,enrichment_backfill,embedding,manual_scrape_batch}_worker.py`, `app/services/plex_generation_service.py`, `app/plex_generator/source.py`.
    - *Critères :* Tous les jobs de fond utilisent `worker_session_factory` par défaut ; aucun `async_session_factory` résiduel dans les workers.
  - **T2.4 (`CR-D03`, `CR-D04`) — Migration 008 résiliente & `hydrate_misses` protégé**
    - *Périmètre :* `app/db/migrations.py`, `app/services/recommendation_service.py`, tests associés.
    - *Critères :* `_migration_008_ai_embeddings` vérifie `_VEC_LOADED["ok"]` avant `CREATE VIRTUAL TABLE ... USING vec0` ; `hydrate_misses` persiste via `write_with_retry` ou sérialisation post-fetch.
  - **T2.5 (`CR-O01`) — Alléger `GET /api/health`**
    - *Périmètre :* `app/api/health.py`, `app/services/media_service.py` (si cache/lightweight check), `tests/`.
    - *Critères :* `/api/health` n'exécute plus 5 `COUNT(*)`/`MAX` bloquants sur `Media`/`XtreamAccount` toutes les 30s (réponse O(1) ou cache TTL non bloquant avec repli gracieux).

---

## Lot 3 — Performance DB & I/O Services (P1-A)

- **Déclencheurs :** `perf-benchmarker` (index PK `Media`, PRAGMAs SQLite, event loop `nfo_import_service`).
- **Tickets :**
  - **T3.1 (`CR-P01`, `CR-P06`) — Corriger l'ordre du tuple PK `Media` & fusionner `COUNT(*)`**
    - *Périmètre :* `app/services/media_service.py`, `app/services/metadata_service.py`, tests unifiés/metadata.
    - *Critères :* `tuple_(Media.rating_key, Media.server_id)` utilisé partout pour exploiter l'index PK ; `EXPLAIN QUERY PLAN` vérifié en test ; `count_movies_missing_external` en une seule agrégation conditionnelle.
  - **T3.2 (`CR-P02`, `CR-P05`) — Listener `connect` PRAGMAs & pool client Ollama**
    - *Périmètre :* `app/db/database.py`, `app/services/ollama_service.py`, tests DB/Ollama.
    - *Critères :* Listener `connect` sur `engine.sync_engine` et `worker_engine.sync_engine` appliquant `synchronous=NORMAL`, `cache_size=-64000`, `temp_store=MEMORY`, `mmap_size=268435456` ; `ollama_service` réutilise un `httpx.AsyncClient` poolé.
  - **T3.3 (`CR-A04`) — Async I/O dans `nfo_import_service`**
    - *Périmètre :* `app/services/nfo_import_service.py`, tests NFO import.
    - *Critères :* `nfo_path.exists()` et `ET.parse(nfo_path)` exécutés via `asyncio.to_thread`.

---

## Lot 4 — Observabilité, Docker, Tests critiques & Durcissement (P1-B)

- **Déclencheurs :** `security-reviewer` (`crypto_fields`, `tv_auth`, `body_limit`, `rate_limit`, `CORS`, `route_audit`). **Risky** (secrets Fernet, auth, Docker).
- **Tickets :**
  - **T4.1 (`CR-O04`) — Métriques Prometheus Lot B2 & Ollama** (`app/utils/metrics.py`, `app/services/metadata_service.py`, `app/services/ollama_service.py`).
  - **T4.2 (`CR-T02`, `CR-T03`) — Tests `app/cli.py` & `app/services/ollama_service.py`** (0 % → couverture complète via `CliRunner` + `respx`).
  - **T4.3 (`CR-O02`, `CR-O03`, `CR-S06`) — Docker healthcheck, lock permissions & body limit** (`docker-compose.yml`, `Dockerfile`, `app/main.py`, `app/api/body_limit.py`).
  - **T4.4 (`CR-S05`, `CR-S07..S11`) — Durcissement sécurité complémentaire** (`crypto_fields.py` fail-closed, rate-limit `tv_auth`, `resolve_client_ip`, `CORS_ORIGINS`, backoff Basic Auth, extension `route_audit.py`).

---

## Lot 5 — Découpage Routers/Services & Effets de bord (P2-A)

- **Déclencheurs :** `security-reviewer` (`tv_auth`, montage routers) + `perf-benchmarker` (middlewares ASGI). **Risky** (refacto large).
- **Tickets :**
  - **T5.1 (`CR-A01`) — Extraction services hors routers** (`api/ai.py`, `api/tv_auth.py`, `api/live.py`, `api/stream.py`, `api/accounts.py`).
  - **T5.2 (`CR-C01`, `CR-T04`) — Suppression effets de bord à l'import** (`config.py` `ensure_dirs()`, `database.py` lazy/configurable engine + test d'intégration `lifespan`/`init_db`).
  - **T5.3 (`CR-A02`, `CR-A06`) — Uniformisation montage routers & helpers `naming.py`** (`main.py`, `plex_generator/naming.py`, `download_service.py`, `download_nfo.py`).
  - **T5.4 (`CR-O06`, `CR-O05`, `CR-P04`) — Middlewares ASGI purs & garde-fou `--workers 1`** (`request_context.py`, `csrf.py`).

---

## Lot 6 — Recherche FTS5, God Files & Hygiène CI/Repo (P2-B)

- **Déclencheurs :** `perf-benchmarker` (migration FTS5 + `media_service`). **Risky** (migration SQLite).
- **Tickets :**
  - **T6.1 (`CR-P03`) — Indexation FTS5 `trigram` sur `Media` (`title`, `genres`)** (migration 033 + requêtes `media_service.py`).
  - **T6.2 (`CR-A05`) — Découpage ciblé des god files** (`sync_worker.py`, `manual_scrape_service.py`, extraction coroutines `main.py`).
  - **T6.3 (`CR-C02`, `CR-T01`, `CR-T05`, `CR-T06`, `CR-O07`, `CR-O08`) — Hygiène packaging, coverage & CI** (`requirements.txt`, `requirements-dev.txt`, `pyproject.toml`, `.github/workflows/{tests,docker}.yml`).
