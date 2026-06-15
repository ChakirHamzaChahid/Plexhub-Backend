---
name: brownfield-onboarding
description: À utiliser pour cartographier un backend FastAPI EXISTANT et déjà construit au lieu d'un projet vierge — détecte la stack, reverse-engineer l'architecture telle que bâtie, cartographie modules/flux/schéma/migrations, et classe la dette en safe-à-auto-fix vs risky-needs-approval. Sert l'agent a0-cartographer. Déclenché par /refresh-context et par les audits.
---

# Brownfield onboarding

Les commandes greenfield génèrent vision → PRD → architecture → code. Pour un backend qui existe
déjà, on va dans l'autre sens : **lire le code, photographier l'existant, le noter contre la KB
maison, et planifier l'écart.** Cette skill est la procédure de la moitié « lire et comprendre ».
Toute affirmation s'appuie sur une **preuve `fichier:ligne`** — jamais sur un README qui dérive.

## Quand l'utiliser

- Audits et `/refresh-context` (re-cartographier le code à HEAD) démarrent ici.
- Sert d'outillage à l'agent `a0-cartographer` (propriétaire de la carte du backend).

## Étape 1 — Détecter le backend

Scanne le répertoire cible (pas plus de ~3 niveaux) pour établir la vérité terrain avant tout
raisonnement :

- **Runtime / stack** : `requirements.txt`, `requirements-dev.txt`, `pyproject.toml`,
  `.python-version`, `.github/workflows/*.yml`. Note Python 3.13, FastAPI, SQLAlchemy[asyncio],
  aiosqlite, httpx, Pydantic v2, APScheduler, fastembed/onnxruntime, sqlite-vec, cryptography.
- **Persistance** : `app/db/database.py` (engine async, WAL), `app/db/migrations.py` (chaîne
  001→00N, idempotence `IF NOT EXISTS`), `app/models/database.py` (entités).
- **Entrée / config** : `app/main.py` (lifespan, élection master `fcntl.flock`, scheduler,
  middlewares, routers montés, `APP_VERSION`), `app/config.py` (`Settings`).
- **Signaux** : `CLAUDE.md`, `docs/`, `Dockerfile`/`docker-compose.yml`, `.env.example`, présence de
  fastembed/ONNX (modèle IA), sqlite-vec (recherche vectorielle).
- Enregistre les trouvailles brutes ; ne devine pas là où tu peux lire.

## Étape 2 — Reverse-engineer la carte telle que bâtie

Produis/rafraîchis la carte du backend décrivant **ce qui existe**, pas ce qui est souhaité — chaque
fait ancré sur `fichier:ligne` :

- **`docs/20-architecture.md`** — la stack *réelle*, le découpage en couches (`api/` → `services/` →
  `workers/` → `db/`/`models/` → `utils/` + `plex_generator/`), patterns persistance/async/DI/scheduling
  en usage, déploiement Docker, CI. Marque tout ce qui est inféré comme `(inféré)`.
- **Inventaire des modules** (`app/`) — pour chaque module : sa responsabilité, ses entrées/sorties,
  qui l'appelle. Liste les routers (préfixes), services, workers, le `plex_generator`.
- **Flux bout-en-bout** — sync (xtream→DB), enrichissement (TMDB), validation de flux, génération
  Plex, recommandations IA (`/rank`, embeddings, sqlite-vec), appairage TV (Fernet). Trace
  l'enchaînement réel (worker → service → DB).
- **Schéma & migrations** — liste les tables/entités, l'ordre des migrations 001→00N, vérifie leur
  idempotence, repère la dépendance M008 (sqlite-vec `vec0`).
- **`CLAUDE.md`** à la racine si absent — amorcé depuis l'archi telle que bâtie + la KB maison,
  épinglant la stack réelle, les commandes build/run/test et les noms canoniques trouvés dans le code.

Ne refactore rien à cette étape. Tu prends une photo, tu ne rénoves pas.

## Étape 3 — Classer chaque remédiation en Safe ou Risky

Quand un audit transforme les trouvailles en travail, tague chaque item pour que les corrections
sûres soient automatisables et les changements risqués gatés derrière un plan + approbation humaine :

**Safe (auto-fix, gate de code-review normale) :**
- Lint/format ruff (`ruff check`, `ruff format`), suppression de code mort, `print` → logger `plexhub`
- Ajout de schémas Pydantic manquants sur une frontière (remplacer un dict nu typé)
- Ajout de tests manquants (service + endpoint), wrap d'un appel concurrent par `utils/db_retry`
- Injection de `request_id` manquante, métriques Prometheus manquantes
- Correctness mécanique : `await asyncio.to_thread(...)` sur un appel bloquant oublié, index manquant

**Risky (écrire un ticket + un court plan, approbation requise avant de toucher au code) :**
- Toute **migration de schéma** ou changement de données persistées (DDL destructif = `needs-approval`)
- **Refactors d'architecture** — frontières de couches, déplacement de logique entre services/workers
- Changements du **modèle de concurrence** — async/`to_thread`, élection master `fcntl.flock`, scheduler
- Changement de contrat **API** visible client : codes d'erreur, le **contrat 503 IA** (3 motifs),
  le device-flow tv-auth, la forme d'un endpoint exposé
- Toucher aux **garde-fous maison** : `utils/db_retry`, `SafeRotatingFileHandler`, payload Fernet
- Tout ce qui change un comportement qu'un consommateur d'API remarquerait

Posture par défaut : **corriger le Safe automatiquement ; pour le Risky, proposer et attendre.**
Ne réalise jamais un changement risqué en silence pendant un audit.

## Étape 4 — Passer la main

L'onboarding passe la main à l'audit, qui écrit un backlog de remédiation (chaque item porte sa
sévérité, la règle KB violée, le tag Safe/Risky, et la preuve `fichier:ligne`), puis la boucle de
build normale les ferme — avec la DoD backend (tests verts · boot `uvicorn app.main:app` ·
`/api/health` 200 · migrations idempotentes · OpenAPI à jour).

## Anti-patterns

- Raisonner sur le backend depuis son README seul — lis les fichiers de build et le code ; les README dérivent.
- Réécrire du code pendant l'onboarding/l'audit « tant qu'on y est » — sépare le voir du changer.
- Traiter un refactor risqué comme safe parce qu'il est petit — le risque est le rayon de souffle, pas la taille du diff.
- Affirmer un fait sans preuve `fichier:ligne` — l'autorité de vérité est le code, pas la mémoire.
