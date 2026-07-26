# Audit v1 — Phase 0 : Cartographie (vérifiée `fichier:ligne`)

> **Full-auditor `/audit-full`** — branche `develop`, HEAD **`9da9d46`** (release **v1.7.1**), audité 2026-07-26.
> Runtime observé : serveur smoke local (DB fraîche, migrations 001→022 rejouées, **0 warning duplicate-column**), `GET /api/health` → 200 `{"version":"1.7.1"}`, boot « 102 routes », mode Slave (élection shimée Windows).
> Périmètre de ce fichier : modèle mental prouvé + écarts doc⇆code. Les findings de stabilité/sécurité sont dans `10-stability.md` / `20-security.md`.

## 1. Les 2 commits au-delà du bandeau CLAUDE.md

| Commit | Contenu réel (vérifié `git show --stat`) | Code applicatif touché |
|---|---|---|
| `38aeb5a` fix(dav) prewarm VFS | `CLAUDE.md` (§5.11 + piège 18g mis à jour DANS le commit), `docs/30-ops-plex-webdav.md` (+131), **`scripts/prewarm-dav-cache.sh`** (nouveau, 134 l., shell hôte) | **Aucun** (`app/` intact) |
| `9da9d46` release v1.7.1 | `app/main.py` : `APP_VERSION = "1.7.1"` (1 ligne) | Oui (bump seul) |

Conclusion : aucun risque fonctionnel non documenté dans ces 2 commits ; en revanche le **bandeau** CLAUDE.md (« HEAD `1ac00d3`, v1.7.0 ») et **§4** (« `APP_VERSION = "1.7.0"` ») n'ont pas été recalés au bump → AUDIT-P0-001.

## 2. Modules réels (`app/`, 28 106 LOC hors tests)

| Module | Fichiers clés (LOC) | Rôle vérifié |
|---|---|---|
| racine | `main.py` (662), `config.py` (274), `cli.py` | App/lifespan/élection master (`main.py:243-520`), montage routers (`main.py:570-657`), `Settings` maison `os.getenv` (pas de pydantic-settings — la dépendance a d'ailleurs **disparu de `requirements.txt`**, cf. §5) |
| `api/` | **21 modules** : accounts, admin, admin_downloads, admin_plex_downloads, admin_unified_downloads, ai (1228→ toujours god-file), api_keys, categories, dav, deps, downloads, enrichment, health, live, media, plex, plex_downloads, stream, sync, tv_auth | 102 routes au boot. `deps.py` (183 l.) = 5 gardes (cf. `20-security.md` §1) |
| `services/` | 27 modules ; les plus gros : `download_service` (1352), `nfo_import_service` (888), `plex_sync_service` (674), `media_service` (691), `tmdb_service` (673), `subtitle_service` (643) | conforme §2 CLAUDE.md (tous les modules listés existent, aucun fantôme) |
| `workers/` | `sync_worker` (**1618** — doc dit 1390, le god-file a grossi), `health_check_worker` (786), `enrichment_worker` (733), `download_worker` (538), `enrichment_backfill_worker` (398), `embedding_worker` (150) | |
| `db/` | `database.py` (131), `migrations.py` (**1051**) | ⚠️ `database.py` a désormais **DEUX engines** : `engine` API (pool 20+30, timeout 60 s, `database.py:10-25`) + **`worker_engine`/`worker_session_factory`** (pool 2+2, `database.py:35-58`) — **non documenté dans CLAUDE.md** ; seul `health_check_worker` consomme le pool worker (`health_check_worker.py:9`) |
| `models/` | `database.py` (678 — ORM), `schemas.py` (645 — Pydantic v2 camelCase) | `XtreamAccount.password` + `PlexServer.access_token` = `EncryptedString` (`models/database.py:140,577`) |
| `plex_generator/` | generator (506), source (250), storage (222), naming, nfo_builder, mapping, models | conforme |
| `dav/` | vfs (140), tree_builder (250), propfind (103), throttle (112), relay (350) | conforme §5.11 |
| `utils/` | db_retry, metrics (5 métriques métier), crypto_fields (158), payload_crypto (68), rating_blend, tasks, request_context, server_id, string_normalizer, ttl_cache, unification, time | |
| `scripts/` | backup_db, validate_id_consistency (704), strip_titles_pollution, dedup_resolved_twins, rename_download_illegal_chars, **backfill_certifications** (294 — absent de §2 CLAUDE.md) | |

## 3. Chaîne de migrations RÉELLE (`app/db/migrations.py:22-50`)

- **22 migrations, ordre exact** : 001→007 séquentielles, puis **008 sur connexion dédiée** (`migrations.py:34-35`, sqlite-vec requis pour la table virtuelle `vec0` — conforme piège 6), puis 009→**022** (`omdb_scrape_cache`, `migrations.py:50`).
- Idempotence : `CREATE … IF NOT EXISTS` + probe **`_column_exists`** via `PRAGMA table_info` (`migrations.py:55-70`) avant chaque `ADD COLUMN` → le boot fraîche est réellement silencieux (**vérifié empiriquement : 0 warning duplicate-column** sur la DB smoke) → **CR-C05 résolu au code**.
- ⚠️ Réserve stabilité (masquage d'échec réel par `except Exception → warning`) : voir AUDIT-P1-002 dans `10-stability.md`.
- `api_keys` reste créée par `Base.metadata.create_all` seul (pas de migration numérotée) — inchangé, conforme doc.

## 4. Montage des routers & gardes (`app/main.py:570-657`) — prouvé

3 conventions cohabitent (CR-A04 toujours vrai — aucun walker d'assertion `/api/*` au boot, le commentaire `main.py:561-564` l'acte comme follow-up non fait) :

| Pattern | Routers | Garde | Preuve |
|---|---|---|---|
| A — garde au mount | accounts, categories, live, media, stream, sync, plex | `_guard=[Depends(verify_backend_secret)]` | `main.py:568,573-579` |
| A' — Basic Auth au mount | admin, admin_downloads, admin_plex_downloads, admin_unified_downloads + `/docs` + `/openapi.json` | `verify_admin_basic_auth` | `main.py:588-621` |
| B — public | health (`main.py:570`), tv_auth (`main.py:582` ; seul `/approve` gardé via alias `verify_pairing_api_key = verify_backend_secret`, `tv_auth.py:40,260`) | — | smoke : `/api/health` 200 sans clé |
| C — self-préfixé + garde module-level | ai (`verify_api_key`, `ai.py:52-55`), api_keys (`api_keys.py:20-23`), downloads (`downloads.py:33-36`), plex_downloads (`plex_downloads.py:46-49`), enrichment (`enrichment.py:33-36`) — tous `verify_master_key` sauf ai | `main.py:643-647` |
| C' — hors `/api` | dav (`verify_dav_basic_auth`, `dav.py:79`) | `main.py:657` ; smoke : `/dav/` → 503 fail-closed |
| Hors-router | `/metrics` (instrumentator, `metrics.py:46-51`) | **AUCUNE** — smoke : 200 sans clé (CR-S02, cf. `20-security.md`) |

## 5. Stack réelle (`requirements.txt` / `requirements-dev.txt` / `pyproject.toml`)

- Pins liés confirmés avec commentaire d'incident : `fastapi>=0.115,<0.116` ⇆ `prometheus-fastapi-instrumentator>=7.0,<8` (`requirements.txt:1-6,16`).
- Autres bornes : fastembed `>=0.7,<1.0`, onnxruntime `<2.0`, sqlite-vec `<0.2`, numpy `<3.0`, psutil `<7.0`, cryptography `>=42,<46`.
- **Écart vs CLAUDE.md §10** : **`pydantic-settings` n'est plus déclarée** (CR-C07 résolu par suppression) — la doc la liste encore comme « déclarée, non utilisée ».
- Dev : pytest, pytest-asyncio, **pytest-cov `>=5,<6`**, respx, **ruff `>=0.6,<0.9` + black + mypy** (`requirements-dev.txt`) — CR-C06/T09 câblés, conforme §3.
- Runtimes : CI Python 3.13, Docker `python:3.12-slim` (inchangé).

## 6. Écarts doc⇆code relevés (findings Phase 0)

### AUDIT-P0-001 — Bandeau + §4 CLAUDE.md non recalés à la release v1.7.1 — **S3 (dette doc)**
- **Preuve** : bandeau « À JOUR AU 2026-07-23, HEAD `1ac00d3`, release v1.7.0 » et §4 « `APP_VERSION = "1.7.0"` » vs HEAD réel `9da9d46`, `app/main.py:37` = `"1.7.1"`. Le commit `9da9d46` touche `main.py` sans mettre à jour la doc dans le même commit (violation de la règle anti-dérive du bandeau lui-même) ; `38aeb5a` avait, lui, correctement recalé §5.11/piège 18g.
- **Impact** : faible (2 commits, contenu doc §5.11 déjà à jour) mais le détecteur de dérive est censé l'empêcher.
- **Effort** : trivial (`/sync-context`).

### AUDIT-P0-002 — §10 « Dette ouverte » massivement périmé : ≥ 10 findings CR-* listés « ouverts » sont RÉSOLUS à HEAD — **S2 (dette doc, risque de re-travail)**
- **Preuves code (chacune vérifiée)** :
  - CR-S01 (`outputDir` arbitraire) → **résolu** : `_resolve_confined_output_dir` confine sous `PLEX_LIBRARY_DIR` (`app/api/plex.py:35-73`).
  - CR-F04 (pipeline boot vs intervalle sans mutex) → **résolu** : `_PIPELINE_LOCK` partagé (`app/main.py:115,299-305,470-475`).
  - CR-F06 (tv-auth snake_case) → **résolu** : `deviceCode` accepté, legacy conservé (`app/api/tv_auth.py:304-323`).
  - CR-F07 (one-shot non atomique) → **résolu** : claim par UPDATE conditionnel `payload_delivered IS FALSE` + rowcount (`app/api/tv_auth.py:352-376`).
  - CR-F03 (budget TMDB sous-compté) → **résolu** : compteur par tentative HTTP réelle + `reset_request_count` (`app/workers/enrichment_worker.py:610-624`).
  - CR-P06 (`ORDER BY random()`) → **résolu** : ancre `rowid` aléatoire + range scan (`app/workers/health_check_worker.py:326-360`).
  - CR-C04 (writers request-path sans retry) → **largement résolu** : `commit_with_retry` posé sur `tv_auth`/`accounts`/`categories`/`live`/`ai` (grep : `tv_auth.py:175,294,367,411`, `accounts.py:42,63,79`, `categories.py:105`, `live.py:185`) — résiduel : le défaut same-session (AUDIT-P1-001) + `get_db` (AUDIT-P1-006).
  - CR-C05 (duplicate column au boot) → **résolu** : `_column_exists` (`migrations.py:55-70`) + smoke 0 warning.
  - CR-C07 (pydantic-settings inutilisée) → **résolu** par suppression (`requirements.txt`).
  - CR-S06 (CORS wildcard intégral) → **partiellement résolu** : méthodes/headers explicites + warning au boot (`main.py:535-550`) ; origins par défaut `*` subsiste (`config.py:86-88`).
  - CR-C10 (attr-bags anonymes) → **résolu** pour l'auto-provision : `XtreamCredentials` typé (`main.py:175-182`, `services/xtream_credentials.py`).
  - Non listé au §10 mais présent : sérialisation cron vs pipeline de la validation via `_VALIDATION_LOCK` (`health_check_worker.py:285-311`).
- **Impact** : un agent/dev qui se fie au §10 re-fixe des non-problèmes ou sur-estime le risque réel. Le DELTA (autre agent) formalise le statut finding par finding ; ici on acte la **dérive de doc**.
- **Effort** : `/sync-context` ciblé §10.

### AUDIT-P0-003 — Incohérences internes §2 CLAUDE.md — **S3 (dette doc)**
- `db/` : « chaîne **001→019** » (`§2 db/`) alors que bandeau/§9/§10 disent 022 — le code fait **022** (`migrations.py:50`).
- `api/` : « 12+ routers, 67+ endpoints » vs **21 modules / 102 routes** réels (compte auto-avoué « non recompté », mais l'écart devient trompeur).
- `sync_worker.py` : « 1390 LOC » vs **1618** réelles.
- `db/database.py` : le **double engine** (`worker_engine` pool dédié, `database.py:35-58`) et les tailles de pool API (20+30, `pool_timeout=10`) ne figurent nulle part — c'est pourtant un choix d'archi anti-starvation important (root-cause fix d'un incident 500 `QueuePool limit reached` documenté en commentaire `database.py:29-34`).
- Références de lignes `main.py` massivement décalées (montage `396-438` → réel `570-657` ; flock `226-227` → réel `275-276`) — attendu pour un cache, mais à recaler au prochain `/refresh-context`.
- `scripts/` : `backfill_certifications.py` (294 l.) absent de §2.

## 7. Récapitulatif

| ID | Sévérité | Titre | Preuve |
|---|---|---|---|
| AUDIT-P0-001 | S3 | Bandeau/§4 CLAUDE.md non recalés au bump v1.7.1 | `CLAUDE.md` bandeau vs `app/main.py:37` |
| AUDIT-P0-002 | S2 (doc) | §10 dette : ≥10 CR-* « ouverts » en réalité résolus à HEAD | cf. liste de preuves ci-dessus |
| AUDIT-P0-003 | S3 | Incohérences §2 (chaîne 019 vs 022, LOC, double engine non documenté) | `migrations.py:50`, `database.py:35-58` |
