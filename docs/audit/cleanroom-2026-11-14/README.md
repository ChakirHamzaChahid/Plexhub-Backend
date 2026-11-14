# Audit Clean-Room 360° — PlexHub Backend

> **Date :** 2026-11-14 · **Commit :** `6cb09fa` · **Branche :** `develop` · **Version :** `APP_VERSION = "1.15.0"`
> **Méthode :** Audit *clean-room* (zéro historique consulté, lecture seule sur le code applicatif).

---

## 1. Synthèse exécutive & vérifications runtime

- **Périmètre analysé :** 127 fichiers Python (~46 450 LOC) sous `app/`, ~180 fichiers de tests, configuration CI/CD et Docker.
- **Vérifications exécutées à HEAD (`6cb09fa`) :**
  - `ruff check .` : **OK** (`All checks passed!`)
  - `pytest -q --no-cov` : **3826 passed, 4 skipped**, 214 warnings en 429.58s
  - Smoke check boot (`TestClient(app)` isolé, `BACKGROUND_JOBS_ENABLED=false`) : migrations 001→032 OK, boot **0.27s**, **142 routes**, `GET /api/health` → **200** (35.0ms), `GET /metrics` sans `METRICS_PASSWORD` → **503** (fail-closed), `GET /api/media/movies/unified` sans `X-API-Key` → **401** (fail-closed).

L'architecture est globalement mature sur les ajouts récents (`/api/metadata/v1`, `/api/scrape/v1`, `/dav`, `route_audit.py`, tests real-lock), mais cumule **43 findings** (2 `CRITICAL`, 14 `HIGH`, 14 `MEDIUM`, 13 `LOW`) concentrés sur : la contention SQLite (`commit_with_retry`, pools workers), le blocage de l'event loop sur I/O disque, les fuites d'URLs Xtream avec credentials dans les erreurs/logs, et l'absence de garde SSRF sur `xtream_service`.

---

## 2. Scorecard par dimension

| Dimension | Rapport | Score | Critical | High | Medium | Low | Total |
|---|---|---:|---:|---:|---:|---:|---:|
| **1. Architecture & Conventions** | [10-architecture-conventions.md](10-architecture-conventions.md) | **6.5 / 10** | 1 | 5 | 2 | 0 | **8** |
| **2. Sécurité & Authentification** | [20-securite-auth.md](20-securite-auth.md) | **7.5 / 10** | 0 | 3 | 3 | 5 | **11** |
| **3. DB/Migrations, Flux & Perf** | [30-db-flux-perf.md](30-db-flux-perf.md) | **6.5 / 10** | 1 | 4 | 3 | 2 | **10** |
| **4. Tests, Couverture, CI & Ops** | [40-tests-ci-ops.md](40-tests-ci-ops.md) | **7.5 / 10** | 0 | 2 | 6 | 6 | **14** |
| **Global** | — | **7.0 / 10** | **2** | **14** | **14** | **13** | **43** |

---

## 3. Top-10 priorités d'action

1. **`CR-D01` (CRITICAL) — Remplacer `commit_with_retry` sur les 8 chemins d'écriture actifs** (`app/utils/db_retry.py:63-129`, `sync_worker.py`, `enrichment_worker.py`, `health_check_worker.py`, `live.py`, `accounts.py`, `categories.py`, `tv_auth.py`, `category_service.py`) : migrer vers `write_with_retry` avec session fraîche par tentative.
2. **`CR-A03` (CRITICAL) — Supprimer l'I/O disque bloquant dans `download_service.py:536-558`** : déporter `f.write` / `f.flush` / `f.close` via `asyncio.to_thread` (modèle `xtream_service.py:344`).
3. **`CR-S01` + `CR-S02` (HIGH) — Stopper les fuites de credentials Xtream** dans `POST /api/accounts/{id}/test` (`account_service.py:185`, `accounts.py:118`) et dans les logs (`xtream_service.py:74`, `sync_worker.py`, `main.py:223`).
4. **`CR-S03` + `CR-S04` (HIGH/MEDIUM) — Fermer les vecteurs SSRF** : brancher `ssrf.vet_request` sur `xtream_service._get_client()` (`xtream_service.py:28-36`), valider `base_url` dans `accounts.py`, et vérifier l'URL initiale dans `download_service.download_to_disk` (`download_service.py:452-470`).
5. **`CR-D02` (HIGH) — Isoler le pool DB des workers** : faire utiliser `worker_session_factory` (`app/db/database.py:27-58`) à tous les workers et générateurs au lieu de `async_session_factory`.
6. **`CR-P01` (HIGH) — Corriger l'ordre du tuple `(server_id, rating_key)`** en `(rating_key, server_id)` dans `media_service.py:443` et `metadata_service.py:161, 273` pour exploiter l'index PK de `Media`.
7. **`CR-O01` (HIGH) — Alléger `GET /api/health`** (`app/api/health.py:16-28`) : supprimer les 5 requêtes `COUNT(*)`/`MAX` exécutées toutes les 30s par Docker (déplacer les stats vers `/api/admin/*` ou cache TTL).
8. **`CR-D03` (HIGH) — Protéger la migration 008 contre l'absence de `sqlite-vec`** (`app/db/migrations.py:41-42, 323-348`) en conditionnant `CREATE VIRTUAL TABLE ... vec0` à `_VEC_LOADED["ok"]`.
9. **`CR-D04` (HIGH) — Protéger les écritures vectorielles parallèles dans `recommendation_service.hydrate_misses`** (`recommendation_service.py:126-156`) avec `write_with_retry` ou écriture sérialisée.
10. **`CR-P02` + `CR-A04` (MEDIUM/HIGH) — Fiabiliser les PRAGMAs SQLite par connexion** (`@event.listens_for(..., "connect")` dans `database.py`) et déporter `nfo_import_service.py` dans `asyncio.to_thread`.

---

## 4. Roadmap proposée

### Lot P0 — Sécurité & Stabilité SQLite/Event-Loop (immédiat)
- Corriger `CR-S01`, `CR-S02`, `CR-S03`, `CR-S04` (fuites Xtream + SSRF).
- Corriger `CR-A03` (I/O bloquant download) et `CR-D01`, `CR-D02`, `CR-D04` (contention SQLite + isolation pool workers).
- Corriger `CR-O01` (alléger `/api/health`) et `CR-D03` (migration 008 sans `vec0`).

### Lot P1 — Performance, Observabilité & Couverture critique (court terme)
- Corriger `CR-P01` (tuple PK `Media`), `CR-P02` (listener `connect` pour PRAGMAs), `CR-A04` (`nfo_import_service` async).
- Ajouter les métriques Prometheus `/api/metadata/v1` + Ollama (`CR-O04`), aligner `docker-compose.yml` (`CR-O02`, `CR-O03`) et ajouter les tests `cli.py` + `ollama_service.py` (`CR-T02`, `CR-T03`).
- Ajouter `--limit-max-body-size` (`CR-S06`) et passer `crypto_fields.py` en fail-closed (`CR-S05`).

### Lot P2 — Refactoring structurel & Hygiène CI/Repo (moyen terme)
- Extraire les services des routers épais (`CR-A01`), supprimer les effets de bord à l'import dans `config.py` / `database.py` (`CR-C01`, `CR-T04`).
- Découper les god files (`CR-A05`), unifier `naming.py` vs `download_service.py` (`CR-A06`), et indexer les recherches `ILIKE` via FTS5 `trigram` (`CR-P03`).
- Nettoyer `requirements-dev.txt` / `pyproject.toml` (`CR-C02`, `CR-T01`, `CR-T05`) et aligner CI Python 3.12 + multi-arch Docker (`CR-O07`, `CR-O08`).
