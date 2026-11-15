# Dimension 4 — Tests, Couverture, CI & Ops

> **Audit Clean-Room** · Commit `6cb09fa` · `APP_VERSION = "1.15.0"` · 2026-11-14

## 1. Outillage, dépendances & qualité statique

- **Pytest (`pyproject.toml:1-30`) :** `asyncio_mode = "auto"`, branch coverage activé, plancher `--cov-fail-under=70`.
- **Ruff (`pyproject.toml:49-109`) :** `target-version = "py312"`, `select = ["E", "F", "W", "I"]`, mais `E501` et `I001` ignorés globalement + 22 `per-file-ignores` (dont 9 modules `app/` avec `F401`).
- **Mypy (`pyproject.toml:124-136`) :** permissif (`check_untyped_defs = false`), `exclude = ["tests/"]`, non exécuté en CI.
- **Dépendances (`requirements.txt`, `requirements-dev.txt`) :** `fastapi>=0.115,<0.116` et `prometheus-fastapi-instrumentator>=7.0,<8` bornés ensemble ; `python-dotenv` sans version ni borne (`requirements.txt:14`) ; `black>=24.0,<25.0` orphelin dans `requirements-dev.txt:9`.

## 2. Couverture de tests (~180 fichiers)

### Zones fortement couvertes
- **Concurrence SQLite & locks réels :** `test_immediate_write_real_lock.py`, `test_db_retry_real_lock.py`, `test_worker_write_retry_real_lock.py`, `tests/metadata_api/test_real_lock.py`.
- **API `/api/metadata/v1` (Lot B2) :** `tests/metadata_api/*` + `test_metadata_{plan,fields,limits,lock_protection}.py`.
- **Passerelle `/api/scrape/v1` (Lot B1) :** 8 fichiers dédiés + `test_scrape_cache_real_lock.py`.
- **WebDAV (`/dav`), Downloads (Xtream + Plex), Sync & EPG :** suites dédiées unitaires + intégration + real-lock.

### Zones peu ou pas couvertes
- `app/cli.py` (162 LOC) : **0 %** de couverture (`generate`, `recompute-unification`).
- `app/services/ollama_service.py` (73 LOC) : **0 %** de tests unitaires/HTTP (`stream_generate`, `is_healthy` jamais testés, toujours monkeypatché).
- `app/main.py` / `app/db/database.py` : `api_client` (`tests/conftest.py:145-160`) utilise `ASGITransport` (contourne `lifespan`) et `test_startup_wiring.py:32` neutralise `init_db()` ; `fcntl.flock` mocké sous Windows (`test_startup_wiring.py:54-67`).

## 3. CI, Docker & Observabilité

- **Docker (`Dockerfile`, `docker-compose.yml`) :** `python:3.12-slim`, utilisateur non-root `plexhub` (UID/GID 1000), limite `2G` RAM / `1.0` CPU (`docker-compose.yml:73-77`) tendue face à FastEmbed/ONNX + décodage posters (`25M` px × 8) + ffmpeg + SQLite WAL (`64 Mo` cache + `256 Mo` mmap).
- **CI (`.github/workflows/tests.yml`, `docker.yml`) :** job `lint` sur Python 3.13 alors que le runtime cible est 3.12 ; `ffmpeg` absent du runner CI ; image Docker mono-architecture (`linux/amd64` uniquement).
- **Observabilité (`app/utils/metrics.py`, `app/api/health.py`) :** zero-init respecté sur les métriques existantes et `/metrics` fail-closed (`503` si `METRICS_PASSWORD` vide), mais aucune métrique Prometheus pour `/api/metadata/v1` ni Ollama, et `/api/health` exécute 5 requêtes SQL toutes les 30s.

---

## 4. Registre des findings (`CR-T*` & `CR-O*`)

| ID | Domaine | Gravité | Preuves | Description & Risque |
|---|---|---|---|---|
| **CR-O01** | Ops / Santé | **HIGH** | `app/api/health.py:16-28`, `app/services/media_service.py:781-792`, `Dockerfile:62` | **Healthcheck lourd (5 requêtes SQL COUNT/MAX toutes les 30s).** Risque de `pool_timeout=10` → HTTP 500 → conteneur marqué `unhealthy` et redémarré pendant une écriture critique. |
| **CR-T02** | Tests | **HIGH** | `app/cli.py:1-162` | **0 % de couverture sur `app/cli.py`.** Commandes `generate` et `recompute-unification` non testées. |
| **CR-O03** | Ops / Docker | **MEDIUM** | `app/main.py:320-330`, `Dockerfile:45-47`, `docker-compose.yml:61-65` | **Basculement silencieux en esclave (`is_master=False`)** si `./data` appartient à `root` sur l'hôte (`OSError` sur `server_start.lock` capturée sans alerte ni arrêt). |
| **CR-O04** | Observabilité | **MEDIUM** | `app/utils/metrics.py:53-294`, `app/api/metadata_v1.py` | **Trou noir Prometheus sur `/api/metadata/v1` et Ollama** (aucun compteur/histogramme d'éditions, verrous, reverts, conflits 409 ou inférence LLM). |
| **CR-T01** | Tests / Qualité | **MEDIUM** | `pyproject.toml:30, 63-109` | **Plancher de couverture à 70 %** et dérogations Ruff (`E501`, `I001` globaux, 22 `per-file-ignores`). |
| **CR-T03** | Tests | **MEDIUM** | `app/services/ollama_service.py:1-73` | **0 % de tests transport HTTP sur `ollama_service.py`** (`stream_generate` et `is_healthy` non testés). |
| **CR-T04** | Tests | **MEDIUM** | `app/db/database.py:4, 10-25`, `tests/conftest.py:145-160`, `tests/test_startup_wiring.py:32` | **`engine` instancié à l'import**, forçant les tests à contourner `lifespan` et `init_db()`. |
| **CR-O02** | Ops / Docker | **MEDIUM** | `docker-compose.yml:68`, `Dockerfile:63` | **Healthcheck `docker-compose.yml` sans `timeout=5`** sur `urllib.request.urlopen(...)`. |
| **CR-O05** | Ops / Runtime | **LOW** | `app/main.py:122`, `app/utils/rate_limit.py`, `app/config.py:384` | **État intra-processus** (`_PIPELINE_LOCK`, rate-limiters, caches) incompatible avec `uvicorn --workers > 1`. |
| **CR-O06** | Perf / Middleware | **LOW** | `app/utils/request_context.py:18`, `app/api/csrf.py:90` | **Double usage de `BaseHTTPMiddleware`** (surcoût asyncio sur `/dav` et streaming, contrairement à `body_limit.py` en ASGI pur). |
| **CR-T05** | CI / Dépendances | **LOW** | `requirements.txt:14`, `requirements-dev.txt:9-10`, `.github/workflows/tests.yml:61` | `python-dotenv` non borné, `black` résiduel, `mypy` absent de la CI. |
| **CR-T06** | CI / Environnement | **LOW** | `.github/workflows/tests.yml:23-26`, `Dockerfile:10` | `ffmpeg` absent des runners CI alors qu'il est installé dans l'image Docker. |
| **CR-O07** | Ops / CI | **LOW** | `.github/workflows/docker.yml:47-55` | Image Docker publiée en `linux/amd64` uniquement (pas de `linux/arm64`). |
| **CR-O08** | Ops / CI | **LOW** | `.github/workflows/tests.yml:20`, `Dockerfile:1` | Job `lint` CI exécuté sur Python 3.13 face au runtime cible 3.12. |
