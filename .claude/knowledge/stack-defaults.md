# Stack Defaults — PlexHub Backend (FastAPI / Python)

> Valeurs reflétant l'état **RÉEL audité** (2026-06-15, HEAD `1da2ab9`). L'autorité de vérité est le
> repo lui-même : `CLAUDE.md` + `requirements.txt` + `pyproject.toml`. En cas de doute, **vérifier dans le code**.

## Runtime / plateforme
- **Python 3.13** (CI). Code 100 % **async** (FastAPI, SQLAlchemy[asyncio], httpx, aiosqlite).
- Cible d'exécution : **Docker / Linux** (le master-worker s'appuie sur `fcntl.flock`, POSIX). Conteneur **2 Go RAM** (modèle IA + ONNX).

## Stack réelle (vérifiée — `requirements.txt`)
| Concern | Valeur | Source |
|---|---|---|
| Web framework | **FastAPI ≥0.115** + **uvicorn[standard]** | `requirements.txt` |
| ORM / DB | **SQLAlchemy[asyncio] 2.0** + **aiosqlite** ; SQLite **WAL** (`data/plexhub.db`) | `app/db/database.py` |
| Migrations | maison, **idempotentes** (`IF NOT EXISTS`), 001→009 | `app/db/migrations.py` |
| Validation | **Pydantic v2** + pydantic-settings | `app/models/schemas.py` |
| HTTP client | **httpx** (async) ; mocks tests via **respx** | `app/services/*` |
| Scheduler | **APScheduler** (AsyncIOScheduler, master seul) | `app/main.py` |
| Fuzzy match | **rapidfuzz** | `app/utils/*` |
| CLI | **Typer** | `app/cli.py` |
| Observabilité | **prometheus-fastapi-instrumentator** + prometheus-client ; logging stdlib (request_id) | `app/utils/metrics.py` |
| Templates | **Jinja2** (UI admin HTMX) | `app/templates/admin` |
| IA embeddings | **fastembed 0.7** (`paraphrase-multilingual-MiniLM-L12-v2`, 384 dim) + **onnxruntime** | `app/services/embedding_service.py` |
| Recherche vectorielle | **sqlite-vec 0.1** (table virtuelle `vec0 FLOAT[384]`) | migration M008 |
| Crypto | **cryptography** (Fernet — payload tv-auth) | `app/utils/payload_crypto.py` |
| Divers | numpy, psutil, python-multipart, python-dotenv | `requirements.txt` |
| Tests | **pytest** + **pytest-asyncio** (mode auto) + **respx** | `pyproject.toml`, `requirements-dev.txt` |

## Commandes par défaut
- **Run dev** : `uvicorn app.main:app --reload`
- **Tests** : `pytest -v` (CI : `tests.yml`, Python 3.13)
- **CLI** : `python -m app.cli <cmd>`
- **Docker** : `docker compose up` / image via `docker.yml`
- **Lint** : `ruff check` + `ruff format` — **recommandé, à câbler** (absent de `requirements-dev.txt`)

## Invariants (house law)
- **Couches** : router (`api/`) = validation + délégation ; logique = `services/`/`workers/` ; DB via `async_session_factory`/`deps.py`. Pas de logique métier dans un router.
- **Async strict** : aucun appel bloquant dans la boucle (`asyncio.to_thread` pour sqlite `.backup`, init ONNX).
- **Migrations idempotentes** ajoutées en fin de chaîne ; jamais de DDL destructif sans `needs-approval`.
- **DB locks** : opérations concurrentes via `utils/db_retry` (SQLite WAL).
- **Secrets** : jamais dans le repo ni les logs (`.env` gitignored, `.env.example` comme modèle).
- **i18n** : métadonnées TMDB en `fr-FR` par défaut (`TMDB_LANGUAGE`).
- **Contrat 503 IA** (3 motifs) et **épisodes non rankés** = invariants fonctionnels (cf. `CLAUDE.md` §9).
