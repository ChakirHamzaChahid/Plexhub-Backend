# Dimension 1 — Cartographie, Architecture & Conventions

> **Audit Clean-Room** · Commit `6cb09fa` · `APP_VERSION = "1.15.0"` · 2026-11-14

## 1. Cartographie des modules & volumétrie

L'application compte **127 fichiers Python** et **~46 450 LOC** sous `app/`.

| Module / Sous-répertoire | Fichiers | Total LOC | Rôle & Architecture |
|---|---:|---:|---|
| `app/services/` | 47 | 19 826 | Cœur métier (sync, enrichissement, metadata, scrape, downloads, live, TV auth, trailers, TMDB/OMDb) |
| `app/api/` | 26 | 7 845 | Routers FastAPI (HTTP/HTMX/JSON), gardes d'auth (`deps.py`), rate-limiting, audit des routes |
| `app/workers/` | 8 | 5 473 | Tâches de fond (sync Xtream, validation flux, enrichissement TMDB/OMDb, downloads, batch scraper) |
| `app/scripts/` | 8 | 2 491 | Scripts de maintenance, backfills, vérification d'intégrité, sauvegardes DB |
| `app/models/` | 5 | 2 308 | ORM SQLAlchemy (`database.py`), DTOs Pydantic (`schemas.py`, `metadata_schemas.py`, `scrape_schemas.py`) |
| `app/utils/` | 18 | 2 062 | Primitives partagées (chiffrement Fernet, métriques Prometheus, locks, rate-limiters, SSRF, retry) |
| `app/db/` | 5 | 1 952 | Engine/session SQLite (`database.py`), 32 migrations séquentielles (`migrations.py`), transactions immédiates |
| `app/plex_generator/` | 8 | 1 596 | Génération de bibliothèque Plex/Jellyfin (.strm, NFO XML, mapping, stockage local) |
| `app/` *(racine)* | 4 | 1 585 | `main.py` (orchestration, lifespan, scheduler), `config.py` (settings), `cli.py` (CLI Typer) |
| `app/dav/` | 6 | 1 010 | VFS WebDAV read-only pour rclone/Plex (PROPFIND, HEAD, GET stream relay, throttling) |

*(Note : pas de répertoire dédié `app/ai/` ni `app/middleware/`. L'IA est distribuée entre `app/api/ai.py`, `app/workers/embedding_worker.py` et `app/services/embedding_service.py` / `recommendation_service.py` / `ollama_service.py`. Les middlewares sont hébergés dans `app/api/body_limit.py`, `app/api/csrf.py` et `app/utils/request_context.py`.)*

### Fichiers > 400 LOC (42 fichiers)

Top 15 par taille :
1. `app/workers/sync_worker.py` — **1 839** LOC
2. `app/db/migrations.py` — **1 582** LOC
3. `app/services/manual_scrape_service.py` — **1 534** LOC
4. `app/services/download_service.py` — **1 372** LOC
5. `app/services/tmdb_service.py` — **1 360** LOC
6. `app/api/ai.py` — **1 246** LOC
7. `app/workers/health_check_worker.py` — **955** LOC
8. `app/api/admin.py` — **945** LOC
9. `app/services/nfo_import_service.py` — **903** LOC
10. `app/models/database.py` — **826** LOC
11. `app/services/scrape_gateway_service.py` — **817** LOC
12. `app/services/media_service.py` — **801** LOC
13. `app/main.py` — **797** LOC
14. `app/workers/enrichment_worker.py` — **759** LOC
15. `app/scripts/validate_id_consistency.py` — **716** LOC

*(27 autres fichiers entre 417 et 697 LOC : `tv_auth.py`, `omdb_service.py`, `plex_sync_service.py`, `download_worker.py`, `schemas.py`, `scrape_mappers.py`, `epg_sync_service.py`, `subtitle_service.py`, `manual_scrape_batch_worker.py`, `config.py`, `category_service.py`, `poster_match_service.py`, `metadata_service.py`, `trailer_service.py`, `strip_titles_pollution.py`, `metadata_v1.py`, `media.py`, `generator.py`, `metadata_fields.py`, `plex_catalog_service.py`, `recommendation_service.py`, `admin_downloads.py`, `admin_plex_downloads.py`, `plex_download_service.py`, `scrape_gateway_cache.py`, `enrichment_backfill_worker.py`, `metadata_schemas.py`.)*

---

## 2. Analyse d'architecture & découpage

### 2.1 Discipline des routers : « Thin » vs « Thick »
- **Bons élèves (validation + délégation) :** `app/api/metadata_v1.py` (556 LOC), `app/api/scrape_v1.py` (264 LOC), `app/api/plex.py` (116 LOC).
- **Ruptures majeures :**
  - `app/api/ai.py` (1 246 LOC) : 250+ lignes de schémas inline (`ai.py:66-310`), SQL brut (`ai.py:1135, 1176`), ORM et `commit()` directs (`ai.py:1182-1193`).
  - `app/api/tv_auth.py` (697 LOC) : orchestration pairing, Jinja2 (`tv_auth.py:73`), crypto Fernet, boucles de collision et DB inline (`tv_auth.py:292-320`).
  - `app/api/live.py` (143 LOC), `app/api/stream.py` (36 LOC), `app/api/admin.py` (945 LOC) : requêtes ORM/SQL directes.
  - `app/api/accounts.py` (`accounts.py:65, 90`) : `db: AsyncSession = Depends(get_db)` injecté mais inutilisé (remplacé par `write_with_retry`).

### 2.2 Hétérogénéité du montage des routers (`app/main.py:684-786`)
Quatre conventions coexistent :
1. **Pattern A (garde au mount)** : `accounts`, `categories`, `live`, `media`, `stream`, `sync`, `plex` + routers `/admin*`.
2. **Pattern B (mount nu public)** : `health.router`, `tv_auth.router`.
3. **Pattern C (mount nu avec garde module-level)** : `ai`, `api_keys`, `downloads`, `plex_downloads`, `enrichment`, `dav`.
4. **Pattern D (mount nu avec garde par route)** : `scrape_v1`, `metadata_v1`.

Ce manque d'uniformité a rendu nécessaire le garde-fou dynamique `app/api/route_audit.py` (202 LOC, appelé à `main.py:791`).

### 2.3 Appels bloquants dans le code asynchrone
- `app/services/download_service.py:536-558` : `f.write(chunk)`, `f.flush()`, `f.close()` synchrones dans la boucle `async for chunk in resp.aiter_bytes(...)` — alors que `app/services/xtream_service.py:344` utilise `asyncio.to_thread`.
- `app/services/nfo_import_service.py:388, 616-624, 686-694` : `nfo_path.exists()` et `ET.parse(nfo_path)` synchrones dans des boucles async.
- `app/config.py:512-583, 625` : `mkdir`, `read_text`, `write_text` exécutés à l'import via `settings = Settings()`.

### 2.4 Duplication de logique (`services/` vs `plex_generator/`)
- Sanitisation chemins : `app/plex_generator/naming.py:8-19` vs `app/services/download_service.py:191-219` (NFC, NTFS, noms réservés Windows gérés d'un côté seulement).
- Arborescence Plex : `app/plex_generator/naming.py:40-90` vs `app/services/download_service.py:222-260`.
- Helpers NFO : `app/services/download_nfo.py:27-47` vs `app/plex_generator/source.py`.

---

## 3. Registre des findings

### `CR-A03` — I/O disque bloquant synchrone sur l'event loop dans `download_service`
- **Sévérité :** `CRITICAL`
- **Preuves :** `app/services/download_service.py:536-558` (`f.write(chunk)`, `f.flush()`, `f.close()` synchrones dans `async for chunk in resp.aiter_bytes(...)`).
- **Impact :** Freeze de l'event loop asyncio lors des téléchargements vidéo multi-Go, latence et timeouts sur les requêtes concurrentes.
- **Recommandation :** Déléguer l'écriture disque via `asyncio.to_thread` (à l'image de `app/services/xtream_service.py:344`) ou un worker dédié.

### `CR-A01` — Logique métier et requêtes SQL directes dans les routers
- **Sévérité :** `HIGH`
- **Preuves :** `app/api/ai.py:1135-1141, 1175-1193`, `app/api/tv_auth.py:289-320`, `app/api/live.py:39, 58, 117`, `app/api/stream.py:25-28`, `app/api/admin.py`, `app/api/accounts.py:65, 90`.
- **Impact :** Couplage HTTP/SQL, logique métier dupliquée, testabilité unitaire dégradée.
- **Recommandation :** Extraire des services dédiés (`ai_blurb_service`, `tv_auth_service`, `live_service`) sur le modèle de `metadata_v1.py` / `scrape_v1.py`.

### `CR-A02` — 4 conventions hétérogènes de montage des routers
- **Sévérité :** `HIGH`
- **Preuves :** `app/main.py:684-786`, `app/api/route_audit.py:1-46`.
- **Impact :** Risque d'erreur d'exposition lors de l'ajout de nouveaux routers, compensé uniquement au runtime par `route_audit.py`.
- **Recommandation :** Standardiser sur 2 patterns maximum (ex. garde explicite par route ou par router) et documenter la règle unique.

### `CR-A04` — I/O disque et parsing XML synchrones sur l'event loop dans `nfo_import_service`
- **Sévérité :** `HIGH`
- **Preuves :** `app/services/nfo_import_service.py:388, 616-624, 686-694`.
- **Impact :** Blocage de l'event loop lors du scan et parsing de milliers de fichiers NFO.
- **Recommandation :** Encapsuler `exists()` + `ET.parse()` dans `asyncio.to_thread` par lot.

### `CR-A05` — Concentration excessive de responsabilités (« God Files »)
- **Sévérité :** `HIGH`
- **Preuves :** `app/workers/sync_worker.py` (1 839 LOC), `app/db/migrations.py` (1 582 LOC), `app/services/manual_scrape_service.py` (1 534 LOC), `app/services/download_service.py` (1 372 LOC), `app/api/ai.py` (1 246 LOC), `app/main.py` (797 LOC).
- **Impact :** Maintenabilité réduite, conflits de merge fréquents, complexité de test.
- **Recommandation :** Découper par sous-domaines (ex. `sync/` par type média, `migrations/` par plage ou Alembic, `main.py` sans coroutines métier).

### `CR-C01` — Effets de bord filesystem bloquants à l'import dans `config.py`
- **Sévérité :** `HIGH`
- **Preuves :** `app/config.py:512-583, 625` (`mkdir`, `read_text`, `write_text` dans `Settings.__init__`).
- **Impact :** Tout import de `app.config` (tests, scripts, CLI) mute le disque et échoue si le FS est read-only.
- **Recommandation :** Déplacer la création de répertoires et fichiers dans une méthode `ensure_dirs()` appelée explicitement au `lifespan` / CLI.

### `CR-A06` — Duplication et divergence entre `services/` et `plex_generator/`
- **Sévérité :** `MEDIUM`
- **Preuves :** `app/plex_generator/naming.py:8-19, 40-90` vs `app/services/download_service.py:191-260` ; `app/services/download_nfo.py:27-47` vs `app/plex_generator/source.py`.
- **Impact :** Risque de désalignement des chemins `.strm` vs téléchargements physiques (notamment caractères spéciaux / NTFS).
- **Recommandation :** Unifier les fonctions de sanitisation et de calcul d'arborescence dans un module partagé.

### `CR-C02` — Divergence dépendances dev, Ruff/Black et versioning
- **Sévérité :** `MEDIUM`
- **Preuves :** `requirements-dev.txt:9` (`black>=24.0,<25.0`) vs `pyproject.toml:111-122` (Black retiré) ; absence de `[project].version` dans `pyproject.toml` face à `APP_VERSION = "1.15.0"` (`app/main.py:41`).
- **Impact :** Outil obsolète dans `requirements-dev.txt` et absence de source de vérité package unique.
- **Recommandation :** Supprimer `black` de `requirements-dev.txt` et déclarer `[project]` dans `pyproject.toml`.
