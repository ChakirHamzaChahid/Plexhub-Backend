# Dimension 3 — DB/Migrations, Flux clés & Performance

> **Audit Clean-Room** · Commit `6cb09fa` · `APP_VERSION = "1.15.0"` · 2026-11-14

## 1. Synthèse DB, migrations & flux critiques

- **Chaîne de migrations (001–032) :** globalement saine et idempotente, à l'exception de la migration 008 (`CR-D03`) qui exécute `CREATE VIRTUAL TABLE ... USING vec0` sans vérifier `_VEC_LOADED["ok"]`.
- **Intégrité `match_locked` / `METADATA_WRITABLE_COLUMNS` :**
  - `sync_worker.py:783-804` (`_LOCK_PROTECTED_SYNC_COLUMNS`), `nfo_import_service.py:517`, `manual_scrape_service.py:851, 918` et `metadata_plan.py:229, 281` préservent correctement les champs verrouillés (note mineure : `enqueue_for_enrichment` insère sans vérifier `match_locked`, filtré ensuite par `_media_is_unlocked()` dans `enrichment_worker`).
- **Passerelle de scraping (`/api/scrape/v1`) :** `scrape_gateway_service.py:287-335` utilise correctement `create_background_task` + `asyncio.shield(task)` et `_FlightOutcome` (note mineure : `_read(key)` avant `_join_or_start` ligne 347 laisse deux requêtes simultanées lire la DB avant coalescing).
- **EPG bulk upsert & pruning :** `epg_sync_service.py:361-415` utilise `on_conflict_do_update` sur `uix_epg_dedup` (migration 028), `write_with_retry`, `ET.iterparse` (`_MAX_BATCH_SIZE = 2000`) et pruning borné (`_PRUNE_STREAMS_PER_TXN = 250`).

Les principaux risques portent sur la **contention SQLite** (`CR-D01`, `CR-D02`, `CR-D04`), l'**indexation des requêtes unifiées** (`CR-P01`), et les **PRAGMAs par connexion** (`CR-P02`).

---

## 2. Tableau récapitulatif des findings (`CR-D*` & `CR-P*`)

| ID | Sévérité | Domaine | Preuves | Description |
|---|---|---|---|---|
| **CR-D01** | **CRITICAL** | DB Retry / Contention | `app/utils/db_retry.py:63-129`, `app/workers/sync_worker.py:1208, 1390, 1416, 1518, 1665, 1737, 1756`, `app/workers/enrichment_worker.py:672, 713`, `app/workers/health_check_worker.py:915, 925`, `app/api/live.py:193`, `app/api/accounts.py:54`, `app/api/categories.py:114`, `app/api/tv_auth.py:632`, `app/services/category_service.py:259, 319, 441` | `commit_with_retry` ne survit pas à un vrai lock SQLite (`PendingRollbackError` après le 1er échec → rollback + re-raise) sur 8 chemins d'écriture actifs. |
| **CR-D02** | **HIGH** | Pool / Isolation | `app/db/database.py:27-58`, `app/workers/sync_worker.py:10, 1253, 1830`, `app/workers/enrichment_worker.py:12, 190, 619, 741, 753`, `app/workers/enrichment_backfill_worker.py:52, 453`, `app/workers/embedding_worker.py:16, 75, 111`, `app/workers/manual_scrape_batch_worker.py:169, 171`, `app/services/plex_generation_service.py:101, 106`, `app/plex_generator/source.py:142, 248` | Les workers de fond utilisent `async_session_factory` (pool API) au lieu de `worker_session_factory`, risquant d'épuiser le pool HTTP (`QueuePool limit reached`). |
| **CR-D03** | **HIGH** | Migrations / Boot | `app/db/migrations.py:41-42, 323-348`, `app/db/database.py:64, 80-97` | La migration 008 crée inconditionnellement la table virtuelle `vec0` sans vérifier `_VEC_LOADED["ok"]`, cassant le démarrage si `sqlite-vec` est absent. |
| **CR-D04** | **HIGH** | Intégrité / Concurrence | `app/services/recommendation_service.py:126-156, 178-207` | `hydrate_misses` lance jusqu'à 20 écritures SQLite concurrentes sans `write_with_retry` ; un lock laisse `failed += 1` et perd l'embedding. |
| **CR-P01** | **HIGH** | Performance / Index | `app/models/database.py:18-21`, `app/services/media_service.py:440-446`, `app/services/metadata_service.py:161, 273` | Prédicat `tuple_(Media.server_id, Media.rating_key).in_(...)` inversé par rapport à la PK `(rating_key, server_id, filter, sort_order)`, empêchant l'usage de l'index PK. |
| **CR-P02** | **MEDIUM** | Performance DB | `app/db/database.py:121-127` | PRAGMAs par connexion (`synchronous=NORMAL`, `cache_size=-64000`, `temp_store=MEMORY`, `mmap_size=256MB`) exécutés une seule fois dans `init_db()` sans listener `connect`. |
| **CR-P03** | **MEDIUM** | Performance / Hot Path | `app/services/media_service.py:177-182, 322-327` | Recherches `Media.title.ilike("%...%")` et `Media.genres.ilike("%...%")` provoquant des full table scans. |
| **CR-P04** | **MEDIUM** | Multi-workers | `app/db/immediate_tx.py:49-60`, `app/services/scrape_gateway_service.py:279-303`, `app/utils/rate_limit.py:244-264`, `app/services/account_outage_service.py:65-72`, `app/services/unified_group_service.py:178-206` | États et locks strictement intra-processus incompatibles avec `--workers > 1`. |
| **CR-P05** | **LOW** | Réseau / Event Loop | `app/services/ollama_service.py:28, 37, 46` | `httpx.AsyncClient` recréé à chaque appel Ollama (churn sockets/FD). |
| **CR-P06** | **LOW** | Requêtes SQL | `app/services/media_service.py:627-639` | `count_movies_missing_external` exécute 3 requêtes `COUNT(*)` séquentielles au lieu d'une agrégation conditionnelle unique. |

---

## 3. Fiches détaillées & remédiations

### `CR-D01` — `commit_with_retry` incapable de survivre à un lock réel
- **Sévérité :** `CRITICAL`
- **Mécanisme :** En SQLAlchemy 2.x, après un échec de `session.commit()` sur `OperationalError: database is locked`, la session passe en `PendingRollbackError` et ne peut plus être re-commitée (`app/utils/db_retry.py:108-126` rollback et re-lève l'erreur dès le 1er échec).
- **Remédiation :** Migrer les sites d'écriture vers `write_with_retry(work, session_factory=...)` avec session fraîche par tentative, et découper les boucles longues des workers en unités transactionnelles indépendantes.

### `CR-D02` — Contournement de `worker_session_factory` par les workers
- **Sévérité :** `HIGH`
- **Mécanisme :** `app/db/database.py:27-58` isole un `worker_engine` (pool 2+2), mais `sync_worker`, `enrichment_worker`, `enrichment_backfill_worker`, `embedding_worker`, `manual_scrape_batch_worker`, `plex_generation_service` et `plex_generator/source.py` utilisent `async_session_factory` (pool API).
- **Remédiation :** Injecter `worker_session_factory` par défaut dans tous les workers et générateurs de fond.

### `CR-D03` — Crash au démarrage dans la migration 008 si `sqlite-vec` est absent
- **Sévérité :** `HIGH`
- **Mécanisme :** `app/db/migrations.py:41-42, 323-348` exécute `CREATE VIRTUAL TABLE IF NOT EXISTS ai_embeddings USING vec0(...)` sans vérifier `_VEC_LOADED["ok"]`.
- **Remédiation :** Conditionner la création de `ai_embeddings` à `_VEC_LOADED["ok"]` afin de préserver la dégradation gracieuse (`503` sur `/api/ai`).

### `CR-D04` — Écritures concurrentes non protégées dans `recommendation_service.hydrate_misses`
- **Sévérité :** `HIGH`
- **Mécanisme :** Jusqu'à 20 coroutines exécutent des `INSERT`/`DELETE`/`commit` en parallèle sans `write_with_retry` (`app/services/recommendation_service.py:126-156, 178-207`) ; sous contention, `gather(..., return_exceptions=True)` perd silencieusement les vecteurs.
- **Remédiation :** Encapsuler la persistance dans `write_with_retry` ou sérialiser les écritures DB après les fetchs HTTP parallèles.

### `CR-P01` — Prédicat tuple inversé contournant l'index PK sur `Media`
- **Sévérité :** `HIGH`
- **Mécanisme :** La PK de `Media` est `(rating_key, server_id, filter, sort_order)` (`app/models/database.py:18-21`), alors que `media_service.py:443` et `metadata_service.py:161, 273` filtrent sur `tuple_(Media.server_id, Media.rating_key).in_(pk_pairs)`.
- **Remédiation :** Inverser en `tuple_(Media.rating_key, Media.server_id).in_([(rk, sv) for sv, rk in pk_pairs])`.

### `CR-P02` — PRAGMAs par connexion perdus sur les connexions du pool
- **Sévérité :** `MEDIUM`
- **Remédiation :** Attacher un listener `@event.listens_for(..., "connect")` sur `engine.sync_engine` et `worker_engine.sync_engine` pour appliquer `synchronous=NORMAL`, `cache_size=-64000`, `temp_store=MEMORY` et `mmap_size=268435456` à chaque nouvelle connexion.

### `CR-P03` — Full table scans sur `Media.title.ilike('%...%')` / `genres.ilike('%...%')`
- **Sévérité :** `MEDIUM`
- **Remédiation :** Indexer `title` et `genres` via une table virtuelle SQLite `FTS5` (tokenizer `trigram`) et joindre sur `(rating_key, server_id)`.

### `CR-P04` — Hypothèses mono-processus non compatibles `--workers > 1`
- **Sévérité :** `MEDIUM`
- **Remédiation :** Documenter `--workers 1` comme exigence stricte (ou externaliser locks/rate-limits/caches si multi-workers un jour requis).

### `CR-P05` & `CR-P06` — Optimisations réseau & SQL (`LOW`)
- **`CR-P05`** (`app/services/ollama_service.py:28, 37, 46`) : réutiliser un `httpx.AsyncClient` poolé au cycle de vie applicatif.
- **`CR-P06`** (`app/services/media_service.py:627-639`) : fusionner les 3 requêtes `COUNT(*)` de `count_movies_missing_external` en une seule agrégation conditionnelle.
