---
description: Workflow Release — tests verts → bump APP_VERSION (develop) → merge develop→main + tag vX.Y.Z → build+push image Docker (GHCR via .github/workflows/docker.yml) → vérif. Délègue à release-manager. Risky → needs-approval.
argument-hint: <version ex. "1.0.1" (optionnel — sinon lit app/main.py APP_VERSION) ; ajoute des notes après la version>
allowed-tools: Read, Write, Edit, Glob, Grep, Bash, Task, Agent
---

> 🟢 **PlexHub Backend — FastAPI/Python 3.13.** Branche `main`. Image Docker publiée sur **GHCR** via `.github/workflows/docker.yml`. Lis `CLAUDE.md` §4/§10. Validation = `pytest -v` + boot `uvicorn app.main:app` + `GET /api/health` 200.

# /release — publier une release (image Docker)

Argument : $ARGUMENTS  *(version optionnelle + notes)*

Délègue à l'agent **`release-manager`** (`Agent` tool) qui exécute le flux. Workflow **orienté process** (ordre, traçabilité, garde-fous priment).

## Phases
1. **Préconditions** — branche `develop` propre et **verte** (`pytest -v` **vert**), serveur boote (`uvicorn app.main:app` + `GET /api/health` 200), `gh` authentifié, `docker.yml` présent. Aucun secret en clair.
1bis. **Synchro Git (bloquante, revue Gemini 2026-09-23)** — `git fetch origin` ; arbre propre (`git status --porcelain` vide) ; `develop` == `origin/develop` et `main` == `origin/main` ; **`git rev-list --count develop..main` = 0** (`main` ne contient aucun commit absent de `develop` : hotfix, commit direct). Sinon **STOP** : proposer de ramener `main` dans `develop` (`git checkout develop && git merge --no-ff main`, `pytest -v` vert, push) **avec l'accord de Chakir**, puis reprendre. Sans ça, promouvoir `develop` vers `main` laisserait ces commits hors de `develop` et les deux branches continueraient de diverger.
2. **Version** — résoudre la version (argument sinon `APP_VERSION` dans `app/main.py`), vérifier qu'elle est **strictement > la dernière publiée** (sinon le tag/l'image n'apportent rien). **Bump `APP_VERSION`** (`app/main.py`) si nécessaire ; commit du bump **sur `develop`**.
2bis. **Smoke de l'image candidate (OBLIGATOIRE, AVANT merge et tag — revue 2026-09-24)** — un tag `v*` déclenche la publication GHCR ; c'est donc maintenant, sur le commit de bump de `develop`, qu'une image défectueuse ne coûte encore rien.
   - `docker build -t plexhub-backend:rc-X.Y.Z .`
   - Données : **copie** de `./data` (jamais le volume de prod), et `.env` de test sans secret de prod.
   - `docker run -d --name plexhub-rc -p 18000:8000 -e APP_PORT=8000 --memory 2g --env-file <.env de test> -v <copie-data>:/app/data plexhub-backend:rc-X.Y.Z`
   - **Critères PASS** : `docker inspect -f "{{.State.Health.Status}}" plexhub-rc` = `healthy` (le `HEALTHCHECK` de l'image a un `start-period` de 60 s) ; `curl -s -o NUL -w "%{http_code}" http://localhost:18000/api/health` = `200` ; `docker logs plexhub-rc` sans `Traceback` ni `ERROR` au boot ; si la release contient une migration, elle s'est appliquée **sur la copie de la base** et un **second boot** passe aussi (idempotence §9).
   - `docker rm -f plexhub-rc`. **FAIL → STOP**, pas de merge ni de tag : correctif sur `develop`, puis reprise.
2ter. **Approbation** — présenter à Chakir : version, notes, résultat du smoke. **Attendre son accord explicite** avant le merge et le tag.
3. **Merge `develop`→`main` & tag** — promouvoir `develop` vers `main` (`git checkout main && git merge --no-ff develop`), vérifier **`git rev-list --count main..develop` = 0** (tout `develop` est dans `main`), puis créer le tag **`vX.Y.Z` sur `main`** (annoté). Ne **jamais** écraser/déplacer un tag existant ni force-push.
4. **Build + push image** — le tag `v*` déclenche **`.github/workflows/docker.yml`** (build + push GHCR). *(Alternative locale : `docker build` + `docker push ghcr.io/...` si CI indisponible.)* Image **idempotente** : ne pas réécrire un tag d'image déjà publié.
5. **Vérifier** — l'image `ghcr.io/...:X.Y.Z` (et `:latest`) est présente — ⚠️ les tags d'image **n'ont pas de `v`** : `docker.yml` utilise `type=semver,pattern={{version}}` ; `docker run` smoke : conteneur démarre, `GET /api/health` 200 (rappel : **2 Go RAM** requis pour le modèle IA/ONNX, §4).

## Garde-fous
- **Risky = approbation humaine** : publier une image/tag est difficilement réversible → **confirme avant le tag/push** si non explicitement autorisé ; **jamais** d'écrasement de tag ou d'image existante. (Cf. `WORKFLOWS.md` « release → needs-approval ».)
- **Secrets** : aucun token/clé (`AI_API_KEY`, `TMDB_API_KEY`, Fernet) dans l'image, les logs ou le repo ; injectés via env/`.env` à l'exécution.
- **Idempotence / retry** : rejouer une étape sans double effet (max 5 essais) ; build qui échoue → diagnostiquer (souvent Dockerfile/RAM/CI), pas de boucle.
- **Pas de tag sans smoke PASS** de l'image candidate (phase 2bis).
- **Traçabilité** : bump commité sur `develop` puis mergé sur `main` (tag) ; rapport final (version, tag, URL image GHCR, résultat du smoke candidat et du smoke post-publication `docker run` + `/api/health`).

## Rollback — release déployée défectueuse
On ne réécrit ni tag git ni tag d'image : on **revient à la version précédente**, puis on **corrige en avant**.
1. **Avant tout déploiement d'une version qui contient une migration** : sauvegarde de la base (fichier SQLite + `-wal`/`-shm`, service arrêté ou `sqlite3 .backup`). Sans elle, pas de retour arrière possible si le schéma a changé.
2. **Revenir (avec l'accord de Chakir)** — redéployer la version précédente : image `ghcr.io/...:<X.Y.Z précédente>` si le déploiement tire GHCR, sinon `git checkout v<précédente>` puis `docker compose up -d --build`. Si la version fautive a migré le schéma de façon non rétro-compatible, restaurer la sauvegarde de l'étape 1. Vérifier `GET /api/health` 200.
3. **Corriger en avant** — `git revert` (ou correctif ciblé) sur `develop`, puis `/release` complet en `X.Y.Z+1` — smoke compris. Ne jamais réutiliser un numéro publié.
4. **Tracer** — `/incident` pour le postmortem.
