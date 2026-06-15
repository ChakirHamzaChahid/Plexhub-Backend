---
name: architecture-builder
description: À utiliser pour produire le doc d'architecture technique et les principes d'ingénierie du backend PlexHub. Utilisé surtout par l'agent CTO / tech-lead. Déclenché sur « conçois l'architecture », « choisis la stack », ou dans le cadre de /feature (phase plan).
---

# Architecture builder

Produit `docs/20-architecture.md` à partir de `docs/00-vision.md` + `docs/10-prd.md`. Charge d'abord
`house-conventions` (packs `stack-defaults.md` + `python-conventions.md` + `api-conventions.md`) :
l'archi est une **instanciation** de ces packs pour le périmètre, pas une réinvention.

## Sections de docs/20-architecture.md

1. **Décision de périmètre** — quels domaines du backend sont touchés (sync, enrichissement,
   validation de flux, génération Plex, IA/recsys, tv-auth, admin), et l'ordre si séquentiel.

2. **Couches FastAPI** — confirme le découpage maison (cf. `python-conventions.md`) :
   - `api/` (routers) : validation Pydantic v2 + délégation, **aucune logique métier**, préfixe `/api`
   - `services/` : logique métier, sans dépendance FastAPI, reçoit/crée une `AsyncSession`
   - `workers/` : tâches planifiées idempotentes et bornées (APScheduler, master seul)
   - `db/` + `models/` : engine async, session factory, migrations idempotentes, entités + schémas
   - `utils/` : briques transverses (db_retry, métriques, crypto, request_id)
   - Pour chaque nouveau domaine, dis **où** vit quoi dans ces couches.

3. **Persistance** — SQLite **WAL** en accès async (`aiosqlite` via SQLAlchemy[asyncio] 2.0),
   sessions via `async_session_factory`/`deps.py`. Schéma : entités SQLAlchemy + migrations maison
   **idempotentes** (`IF NOT EXISTS`, ajout en fin de `run_migrations()`). Concurrence protégée par
   `utils/db_retry`. Recherche vectorielle IA via **sqlite-vec** (`vec0 FLOAT[384]`, M008).
   Tout DDL destructif = `needs-approval`.

4. **Workers & ordonnancement** — APScheduler (AsyncIOScheduler, **master seul** via élection
   `fcntl.flock` POSIX). Pipeline série : sync → enrichissement → validation → génération Plex
   (`max_instances=1`, `coalesce=True`). Crons : health-check, cleanup EPG, backup DB. Toute tâche
   doit être idempotente et bornée (limites quotidiennes, batch sizes). Appels bloquants
   (`sqlite3.backup`, init ONNX) → `asyncio.to_thread`.

5. **Interface API** — esquisse les endpoints REST nécessaires (préfixe `/api`, auth `X-API-Key`),
   les schémas Pydantic v2 d'entrée/sortie, les codes d'erreur (dont le contrat **503 IA** à 3 motifs,
   intangible — cf. `CLAUDE.md` §9). OpenAPI doit refléter le contrat.

6. **Déploiement Docker** — image via `Dockerfile` + `docker-compose.yml`. Conteneur **Linux**
   (l'élection master `fcntl.flock` est POSIX), **2 Go RAM** minimum (modèle IA fastembed + ONNX).
   Volumes pour `DATA_DIR`/`LOG_DIR`. Secrets injectés par env / `.env` (jamais dans l'image).

7. **Layout du dépôt** (état réel) :

   ```
   /
   ├── app/                # code FastAPI (api, services, workers, plex_generator, db, models, utils)
   ├── tests/              # pytest + pytest-asyncio + respx
   ├── docs/               # docs équipe (vision, prd, archi, impl-spec, sprint, board)
   ├── .claude/            # agents, skills, knowledge, workflows
   ├── Dockerfile / docker-compose.yml
   └── requirements*.txt / pyproject.toml
   ```

8. **CI / release** — branche d'intégration **`main`** ; CI `tests.yml` (Python 3.13, `pytest -v`) ;
   image Docker via `docker.yml` ; versioning `APP_VERSION` (`app/main.py`), tag `vX.Y.Z` sur `main`.

9. **Budgets non-fonctionnels** — chiffre-les pour le périmètre :
   - **Latence** : endpoints synchrones < ~200 ms hors I/O externe ; `/rank` chaud rapide (le **cold
     start IA ~30 s** au 1ᵉʳ appel est attendu et documenté).
   - **RAM** : tenir dans l'enveloppe 2 Go du conteneur (modèle IA inclus).
   - **Robustesse** : aucun appel bloquant dans la boucle ; concurrence DB via `db_retry` (WAL) ;
     workers idempotents et bornés.
   - **Observabilité** : `request_id` injecté, métriques Prometheus, **jamais** de secret/token loggé.

10. **Risques** — top 3 avec mitigations (ex. cold start IA, locks SQLite sous charge, dépendance
    POSIX `fcntl.flock` qui interdit le master sous Windows natif).

## Principes d'ingénierie (rulebook court à inclure)

- Chaque PR embarque des tests (`pytest -v` vert).
- Logique métier dans `services/`/`workers/`, jamais dans un router.
- Pas de dict nu en réponse publique : schéma Pydantic v2 typé.
- Aucun appel bloquant dans la boucle d'événements (`asyncio.to_thread`).
- Migrations idempotentes ajoutées en fin de chaîne ; DDL destructif = `needs-approval`.
- Concurrence DB via `utils/db_retry` ; WAL activé.
- Secrets jamais dans le repo ni les logs ; logger `plexhub` avec `request_id`, pas de `print`.
- L'API publique d'un module est documentée à sa racine.

## Barre de qualité

Chaque décision des §3, §4, §5, §9 a un paragraphe de « pourquoi » rattaché au PRD. Si tu ne peux pas
écrire le « pourquoi », reconsidère le choix. La DoD backend (tests verts · boot `uvicorn app.main:app`
· `/api/health` 200 · migrations idempotentes · OpenAPI à jour) est le critère d'acceptation de l'archi.
