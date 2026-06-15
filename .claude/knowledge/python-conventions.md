# Python / FastAPI Conventions — PlexHub Backend

Comment ce backend est structuré. À suivre sauf surcharge explicite dans `CLAUDE.md`/`docs/`.

## Architecture en couches
1. **`api/` (routers)** — un router par domaine, préfixe `/api` (sauf `admin`). Rôle : parser/valider l'entrée (Pydantic), appeler un service, mapper la sortie + les codes HTTP. **Aucune logique métier ici.**
2. **`services/`** — logique métier réutilisable, sans dépendance à FastAPI. Reçoit une `AsyncSession` ou crée la sienne via `async_session_factory`.
3. **`workers/`** — tâches longues/planifiées (sync, enrichment, validation, embedding). Idempotentes, bornées (limites quotidiennes, batch sizes).
4. **`db/`** — engine + session factory + migrations. **`models/`** — entités SQLAlchemy (`database.py`) + schémas Pydantic (`schemas.py`).
5. **`utils/`** — briques transverses (retry DB, métriques, crypto, request_id, normalisation).

## Async
- Tout est `async def` ; appels réseau via `httpx.AsyncClient` (réutilisé, fermé au shutdown).
- **Jamais** d'appel bloquant dans la boucle : `await asyncio.to_thread(...)` pour le CPU/I/O sync (sqlite `.backup`, init modèle ONNX).
- Tâches d'arrière-plan via `utils/tasks.create_background_task` ; toutes annulées proprement au shutdown.

## SQLAlchemy / SQLite
- Sessions via `async_session_factory()` (context manager) ou dépendance `deps.py`. `commit()` explicite.
- **WAL** activé. Opérations sujettes au lock → wrappées par `utils/db_retry`.
- Migrations dans `db/migrations.py` : fonction `_migration_NNN_*`, **DDL idempotent** (`IF NOT EXISTS`, `ADD COLUMN` gardé par try/except ou check), ajoutée **en fin** de `run_migrations()`. Une migration destructive = `needs-approval`.

## Validation & erreurs
- Frontières publiques : schémas **Pydantic v2** (jamais de dict nu). Réponses typées.
- Erreurs : `HTTPException(status_code, detail)`. Les motifs **503** de l'IA sont contractuels (cf. `CLAUDE.md` §9) — ne pas changer leur `detail`.
- Auth : dépendance `X-API-Key` (`deps.py`). Endpoints IA → 503 si `AI_API_KEY` vide.

## Logging
- Logger `plexhub` (et `plexhub.<sous-module>`). `request_id` injecté par middleware. **Jamais** logguer un secret/token/clé en clair, ni un body susceptible d'en contenir.
- Niveaux : DEBUG (fichier), INFO (console), WARNING pour les libs tierces.

## Tests (pytest)
- `tests/test_*.py`, **pytest-asyncio en mode auto** (`async def test_*` direct, pas de décorateur).
- HTTP externe mocké via **respx** (jamais d'appel réseau réel en test).
- Couvrir : service + validation (unitaire) + endpoint (intégration via `httpx.AsyncClient`/`TestClient`). Tout nouveau comportement = un test ; tout bug corrigé = un test de garde.
- DB de test : SQLite éphémère (cf. `tests/conftest.py`).

## Style
- Noms explicites (ce que fait la chose, pas son type). Fonctions courtes, une responsabilité.
- Commentaires = le **pourquoi**, pas le quoi. Constantes nommées (pas de nombre magique).
- **ruff** comme linter/formateur cible (`ruff check`, `ruff format`) — à câbler par `devops-engineer`.
