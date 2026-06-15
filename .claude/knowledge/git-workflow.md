# Git Workflow, Versioning & CI — PlexHub Backend

## Modèle de branches
- **`main`** — branche d'intégration et de production (la CI `tests.yml`/`docker.yml` se déclenche sur `main`). **Jamais de force-push.**
- Branches courtes (< 1 semaine), un changement logique chacune, depuis `main` :
  - `feature/<slug>`, `fix/<slug>`, `refactor/<slug>`, `chore/<slug>`
  - `audit/<sujet>-<YYYY-MM-DD>` pour les passes d'audit/review
  - `hotfix/<version>` depuis un tag, remergée dans `main`
- **Squash-merge** des features/fixes ; PR + review + checks verts requis ; historique linéaire ; pas de force-push sur `main`.

## Conventions de commit
- **Conventional Commits** : `type(scope): description` — types `feat|fix|refactor|chore|docs|test|perf` ; scope = module (`ai`, `sync`, `plex_generator`, `db`, `tv-auth`…). *(Style observé dans l'historique, ex. `feat(tv-auth): …`.)*
- Référencer l'ID de ticket/issue. Un changement logique par commit. La **gate de merge** appartient au `tech-manager` (seul à `git merge` sur `main`).

## Versioning
- Version applicative dans **`app/main.py`** (`APP_VERSION`). Bump avant une release.
- Tag **`vX.Y.Z`** sur le SHA de `main`. Bump par défaut : minor pour une feature, patch pour un fix-only, major sur instruction.

## CI (GitHub Actions)
- **`tests.yml`** : Python 3.13, `pip install -r requirements-dev.txt`, `pytest -v` (un `--deselect` sur un test base64 flaky pré-existant — à retirer quand corrigé). Build vert + tests verts = **gate de merge dur**.
- **`docker.yml`** : build de l'image. Une release passe par un build d'image (cf. `/release`).
- Recommandé (à câbler) : job **ruff** (`ruff check`) + couverture.

## Release (Docker / tag)
- Tests verts → bump `APP_VERSION` → tag `vX.Y.Z` → build + push image (GHCR via `docker.yml`) → vérif. Cf. `.claude/commands/release.md`. Release = **Risky → `needs-approval`**.

## Secrets — jamais dans le repo
Ne jamais committer : `.env`, clés API (`TMDB_API_KEY`, `AI_API_KEY`), clé Fernet (`TV_AUTH_ENCRYPTION_KEY`), identifiants Xtream. Modèle = **`.env.example`** (sans valeurs). Injection via variables d'env / `.env` gitignored. Ne jamais logguer un secret.
