# Git Workflow, Versioning & CI — PlexHub Backend

## Modèle de branches (RÈGLE EN VIGUEUR)
- **`develop`** — **branche de travail ET d'intégration par défaut**. **On commit directement dessus.** ➜ **PAS de branche par tâche** (`feature/*`, `fix/*`, `refactor/*` = proscrites). Tout le développement courant (features, fixes, refactos) se fait en commits successifs sur `develop`.
- **`main`** — branche **stable / release uniquement**. On n'y développe jamais. Elle est mise à jour **seulement lors d'une release**, par merge de `develop` → `main` + tag `vX.Y.Z`. **Jamais de force-push.**
- **Seule exception au « pas de branche »** : `hotfix/<version>` partant de `main` pour un correctif **urgent de prod**, remergé dans `main` **et** dans `develop`. Ce n'est pas du développement courant.
- La CI (`tests.yml` / `docker.yml`) se déclenche sur **`develop` et `main`**.

> Conséquence pratique : un agent/commande ne crée **jamais** de branche pour livrer une tâche — il commite sur `develop`. La revue se fait **sur `develop`** (avant push ou en post-commit) ; la promotion `develop`→`main` appartient au `tech-manager`, au moment d'une release.

## Conventions de commit
- **Conventional Commits** : `type(scope): description` — types `feat|fix|refactor|chore|docs|test|perf` ; scope = module (`ai`, `sync`, `plex_generator`, `db`, `tv-auth`…).
- Référencer l'ID de ticket/issue. **Un changement logique par commit** (sur `develop`, l'historique reste lisible : commits petits, verts, réversibles un par un).
- Pas de mélange de cleanup non lié dans un commit de feature.

## Versioning & Release
- Version applicative dans **`app/main.py`** (`APP_VERSION`). Bump au moment de la release.
- Release = **merge `develop` → `main`** (quand `develop` est vert et stable) → tag **`vX.Y.Z`** sur `main` → build + push image (GHCR via `docker.yml`). Bump par défaut : minor pour une vague de features, patch pour un lot de fixes, major sur instruction. Release = **Risky → `needs-approval`** (cf. `.claude/commands/release.md`).

## CI (GitHub Actions)
- **`tests.yml`** : Python 3.13, `pip install -r requirements-dev.txt`, `pytest -v` (un `--deselect` sur un test base64 flaky pré-existant). Build vert + tests verts = condition pour promouvoir `develop`→`main`.
- **`docker.yml`** : build de l'image (release).
- Recommandé (à câbler) : job **ruff** (`ruff check`) + couverture.

## Secrets — jamais dans le repo
Ne jamais committer : `.env`, clés API (`TMDB_API_KEY`, `AI_API_KEY`), clé Fernet (`TV_AUTH_ENCRYPTION_KEY`), identifiants Xtream, `OLLAMA_URL` interne. Modèle = **`.env.example`** (sans valeurs). Injection via variables d'env / `.env` gitignored. Ne jamais logguer un secret.
