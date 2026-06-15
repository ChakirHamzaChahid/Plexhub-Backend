> 🕒 **À JOUR AU : 2026-06-15 (HEAD `1da2ab9`).** Autorité de vérité du backend PlexHub.
> Maintenance (anti-dérive, OBLIGATOIRE) : tout commit qui touche les modules (§2), le schéma SQLite / les migrations, les flux (§5) ou les conventions (§3) **met à jour le bandeau ci-dessus (date + HEAD) + la section concernée dans le même commit**, OU lance **`/sync-context`** avant de clôturer le workflow. Le détecteur SessionStart (`.claude/hooks/session-start.js`) signale la dérive. En cas de doute sur un fait, **vérifier dans le code** (`fichier:ligne`).

# CLAUDE.md — PlexHub Backend

## §1. Résumé
Backend **FastAPI** (Python, async) du stack PlexHub : miroir de bibliothèques **Plex / Xtream-IPTV**, ingestion **NFO**, enrichissement **TMDB**, génération de **bibliothèque Plex** (NFO + arborescence), et **recommandations IA** (embeddings + recherche vectorielle). Consommé par l'app Android `PlexHubTV`. Le backend ne stocke **aucun historique utilisateur** : il met en cache métadonnées TMDB + embeddings, clés sur `tmdb_id`. Tourne sous **Docker/Linux** (le master-worker s'appuie sur `fcntl.flock`, POSIX).

## §2. Modules (`app/`)
- **`main.py`** — app FastAPI + `lifespan` : init DB, **élection master-worker** (`fcntl.flock` sur `data/server_start.lock`), planificateur APScheduler (master seul), middlewares (CORS, GZip, RequestId), montage des routers, instrumentation Prometheus. `APP_VERSION` y est défini.
- **`config.py`** — `Settings` (classe maison lisant l'env via `os.getenv` + `python-dotenv`, **pas** `BaseSettings`). Clés : `TMDB_API_KEY`, `AI_API_KEY`, `TV_AUTH_ENCRYPTION_KEY` (Fernet), `DATA_DIR`/`LOG_DIR`, knobs sync/validation/backup, `CORS_ORIGINS`, `TMDB_LANGUAGE` (défaut `fr-FR`), auto-provision Xtream depuis l'env.
- **`api/`** — routers (préfixe `/api`, sauf admin) : `health`, `accounts`, `categories`, `live`, `media`, `stream`, `sync`, `plex`, `tv_auth`, **`ai`** (définit lui-même son préfixe `/api/ai`), `admin` (UI HTML/HTMX, **sans** préfixe). `deps.py` = dépendances FastAPI (auth `X-API-Key`, session DB).
- **`services/`** — logique métier : `xtream_service`, `tmdb_service`, `media_service`, `category_service`, `stream_service`, `nfo_import_service`, **`embedding_service`** (fastembed + sqlite-vec), **`recommendation_service`** (ranking item-to-item / centroïde).
- **`workers/`** — tâches planifiées : `sync_worker` (`run_all_accounts`), `enrichment_worker` (TMDB, limite quotidienne), `health_check_worker` (`run_pipeline_validation`, validation de flux), `embedding_worker` (re-embedding).
- **`plex_generator/`** — génération de bibliothèque Plex : `source` (`DatabaseSource`), `generator` (`PlexLibraryGenerator`), `storage` (`LocalStorage` + pool de threads d'images), `nfo_builder`, `naming`, `mapping`, `models`.
- **`db/`** — `database.py` (engine async SQLAlchemy + `async_session_factory` + `init_db`), **`migrations.py`** (migrations 001→009, **idempotentes** `IF NOT EXISTS`).
- **`models/`** — `database.py` (entités SQLAlchemy : `XtreamAccount`, `EpgEntry`, etc.), `schemas.py` (Pydantic v2).
- **`utils/`** — `db_retry` (retry « database is locked »), `metrics` (instrumentator Prometheus), `payload_crypto` (Fernet), `request_context` (request_id + middleware), `string_normalizer`, `unification`, `ttl_cache`, `tasks` (background tasks), `time`, `server_id`.
- **`scripts/`** — `backup_db` (snapshot sqlite `.backup`), `strip_titles_pollution`. **`cli.py`** = CLI Typer.
- **`templates/admin/`** — templates Jinja2 de l'UI admin.

> Règle d'archi : la logique métier vit dans `services/`/`workers/`, **pas** dans les routers (`api/` = validation + délégation). Les accès DB passent par `async_session_factory` / dépendances `deps.py`.

## §3. Conventions
| Sujet | Règle | Référence |
|---|---|---|
| Langage / runtime | **Python 3.13** (CI) — async/await partout (FastAPI, SQLAlchemy[asyncio], httpx, aiosqlite) | `.github/workflows/tests.yml` |
| Style / lint | **ruff** recommandé (`ruff check` + `ruff format`) — *à câbler* (pas encore dans `requirements-dev.txt`) | — |
| Validation | **Pydantic v2** aux frontières (schémas `models/schemas.py`) ; jamais de dict brut en réponse publique | — |
| Erreurs | `HTTPException` aux frontières ; les 3 motifs **503** de l'IA sont contractuels (cf. §9) | `api/ai.py`, `README.md` |
| Auth | en-tête **`X-API-Key`** ; endpoints IA → 503 si `AI_API_KEY` vide ; tv-auth → 503 si pas de clé de chiffrement | `api/deps.py`, `api/tv_auth.py` |
| Logs | logger `plexhub` (DEBUG fichier / INFO console) ; **`request_id`** injecté ; **jamais** de secret/token en clair (tokens Plex, clés API) | `app/main.py`, `utils/request_context.py` |
| DB | SQLite **WAL**, accès async (`aiosqlite`) ; toute lecture/écriture concurrente protégée par **`utils/db_retry`** | `db/database.py` |
| Migrations | **idempotentes** (`CREATE TABLE/INDEX IF NOT EXISTS`, `ADD COLUMN` gardé) ; ajoutées **en fin de chaîne** dans `run_migrations()` | `db/migrations.py` |
| Async I/O | tout appel bloquant (`sqlite3.backup`, ONNX) passe par `asyncio.to_thread` ou un pool | `app/main.py:296` |
| i18n métadonnées | `TMDB_LANGUAGE=fr-FR` par défaut | `config.py:58` |
| Secrets | **jamais** dans le repo (`.env`, clés API, Fernet) ; injectés via env / `.env` gitignored | `.env.example` |

## §4. Build / Run / Test
- **Lancer** : `uvicorn app.main:app --reload` (dev) ; conteneur via `docker-compose up` (**mémoire 2 Go** requise pour le modèle IA + ONNX).
- **CLI** : `python -m app.cli <commande>` (Typer).
- **Tests** : `pytest -v` (pytest-asyncio en **mode auto**, `testpaths=["tests"]`). CI : `tests.yml` (Python 3.13) avec un `--deselect` sur un test base64 flaky pré-existant.
- **Docker** : `docker.yml` (build image). `Dockerfile` + `docker-compose.yml` à la racine.
- **Versioning** : `APP_VERSION` dans `app/main.py` (actuellement `1.0.0`). Tag `vX.Y.Z` sur `main`.

## §5. Flux clés (bout-en-bout)
- **§5.1 Sync** — `sync_worker.run_all_accounts()` → `xtream_service` (auth + fetch catégories/streams) → upsert DB. Planifié toutes `SYNC_INTERVAL_HOURS` (défaut 6 h).
- **§5.2 Enrichissement** — `enrichment_worker.run()` → `tmdb_service` (métadonnées, `/find` pour imdb→tmdb), borné par `ENRICHMENT_DAILY_LIMIT`.
- **§5.3 Validation de flux** — `health_check_worker.run_pipeline_validation()` : teste les streams (concurrence `STREAM_VALIDATION_CONCURRENCY`), marque cassé après `STREAM_BROKEN_THRESHOLD` échecs, re-check toutes `STREAM_VALIDATION_RECHECK_HOURS`.
- **§5.4 Génération Plex** — `_auto_generate_plex_library()` (si `PLEX_LIBRARY_DIR`) : `DatabaseSource(account)` → `PlexLibraryGenerator` → `LocalStorage` (NFO + arbo + images). Rapport created/updated/deleted/unchanged.
- **Pipeline planifié (master)** : sync → enrichissement → validation → génération Plex, en série, `max_instances=1`, `coalesce=True`. + crons : health-check (2 h), cleanup EPG périmé (3 h), backup DB (`BACKUP_HOUR`).
- **§5.5 Recommandations IA** — `POST /api/ai/rank` (item→item) / `rank-multi` (centroïde) → `recommendation_service` + `embedding_service` (**fastembed** `paraphrase-multilingual-MiniLM-L12-v2`, 384 dim) + **sqlite-vec** (`vec0`), cache `ai_tmdb_cache`. `embed/status`, `embed/rebuild` (202 + jobId), `embed/jobs/{id}`.
- **§5.6 Appairage TV** — `tv_auth` device-flow, payload chiffré **Fernet** (`payload_crypto`), TTL `TV_AUTH_TTL_SECONDS` (défaut 900 s).

## §7. Agents & ownership (`.claude/agents/`)
**Direction/orchestration** : `ceo`, `cpo`, `cto`, `tech-lead`, `tech-manager`. **IC** : `backend-developer`. **Qualité/gate** : `qa-engineer`, `code-reviewer`, `security-reviewer`, `integration-agent`. **Run/ops** : `devops-engineer`, `release-manager`, `perf-benchmarker`, `observability-analyst`. **Audit/contexte** : `cleanroom-auditor`, `cleanroom-fixer`, `a0-cartographer`. **Spécialistes domaine** : `db-migration-specialist` (schéma SQLite/migrations — propriétaire historique), `sync-specialist` (Xtream/sync/enrichment), `ai-recsys-specialist` (embeddings/ranking/sqlite-vec), `plex-generator-specialist` (génération NFO/arbo).

## §7bis. Skills par agent
`house-conventions` (charge les packs `knowledge/`) pour tous ; `parallel-orchestrator` (tech-manager) ; `architecture-builder` (cto/tech-lead) ; `requirements-intake`/`prd-builder` (cpo) ; `sprint-planner` (tech-manager) ; `brownfield-onboarding` (a0-cartographer). Skills marketplace utiles : `engineering:debug`/`systematic-debugging` (incident), `engineering:code-review` (reviewers), `security-audit` (security-reviewer), `production-code-audit` (cleanroom/cartographer), `api-design-principles` (cto/backend-developer).

## §9. Pièges (house law — à respecter impérativement)
1. **Cold start IA ~30 s** au 1ᵉʳ `/rank` : fastembed télécharge ~120 Mo de poids ONNX. Les appels suivants sont rapides. Override via `AI_EMBED_MODEL`.
2. **3 motifs de 503 IA** (contractuels) : `AI service not configured` (`AI_API_KEY` vide) · `AI vector storage unavailable` (sqlite-vec n'a pas chargé) · `AI model unavailable` (fastembed KO).
3. **Épisodes non supportés par l'IA** : ranking au niveau `tv` (série) ou `movie` uniquement. Les imdb→épisode/saison/personne sont comptés en `resolutionFailed` et droppés.
4. **Cap 20 fetches TMDB frais par `/rank`** : le reste → `cacheMissesDropped` ; le client re-appelle une fois le cache chaud.
5. **Le rebuild ne tourne JAMAIS au boot** : uniquement via `POST /api/ai/embed/rebuild`. Idempotent (scan `embedded_at IS NULL`, curseur `tmdb_id`, DELETE-puis-INSERT sur `vec0`).
6. **Migrations idempotentes** : tout DDL en `IF NOT EXISTS` ; nouvelle migration ajoutée **en fin** de `run_migrations()`. M008 (`vec0 FLOAT[384]` + `ai_tmdb_cache`) dépend du chargement de sqlite-vec.
7. **`fcntl.flock` = POSIX** : l'élection master-worker ne fonctionne que sous Linux/Docker (pas Windows natif). Seul le master lance le scheduler.
8. **SQLite « database is locked »** : toute opération concurrente passe par `utils/db_retry`. WAL activé. Ne jamais ouvrir plusieurs writers sans retry.
9. **Rotation de logs Windows** : `SafeRotatingFileHandler` avale les `PermissionError` (un autre process tient le fichier). Ne pas « corriger » en supprimant ce garde-fou.
10. **CORS** : `CORS_ORIGINS` explicite en prod (pas `*` en façade publique). Token Plex / clés jamais loggés ni renvoyés.
11. **Appels bloquants** (`sqlite3.backup`, init ONNX) → `asyncio.to_thread` ; ne jamais bloquer la boucle d'événements.

## §10. État réel (audité 2026-06-15, HEAD `1da2ab9`)
Stack vérifiée : Python 3.13 · FastAPI ≥0.115 · SQLAlchemy[asyncio] 2.0 · aiosqlite · httpx · Pydantic v2 + pydantic-settings · APScheduler · rapidfuzz · Typer · prometheus-fastapi-instrumentator · Jinja2 · **fastembed 0.7** · onnxruntime · **sqlite-vec 0.1** · numpy · psutil · cryptography. Tests : pytest + pytest-asyncio (auto) + respx. **Dette ouverte** : lint (ruff) non câblé ; couverture à confirmer ; un test base64 flaky désélectionné en CI.

## §11. Workflows
Voir `.claude/WORKFLOWS.md` (routeur intention→commande, auto-injecté au SessionStart). Commandes principales : `/feature`, `/refacto`, `/incident`, `/audit-cleanroom`, `/fix-cleanroom`, `/benchmark`→`/fix-bench-perf`, `/refresh-context`, `/sync-context`, `/release`, `/app-status`.
