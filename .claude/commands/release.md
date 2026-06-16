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
2. **Version** — résoudre la version (argument sinon `APP_VERSION` dans `app/main.py`), vérifier qu'elle est **strictement > la dernière publiée** (sinon le tag/l'image n'apportent rien). **Bump `APP_VERSION`** (`app/main.py`) si nécessaire ; commit du bump **sur `develop`**.
3. **Merge `develop`→`main` & tag** — promouvoir `develop` vers `main` (`git checkout main && git merge --no-ff develop`), puis créer le tag **`vX.Y.Z` sur `main`** (annoté). Ne **jamais** écraser/déplacer un tag existant ni force-push.
4. **Build + push image** — le tag `v*` déclenche **`.github/workflows/docker.yml`** (build + push GHCR). *(Alternative locale : `docker build` + `docker push ghcr.io/...` si CI indisponible.)* Image **idempotente** : ne pas réécrire un tag d'image déjà publié.
5. **Vérifier** — l'image `ghcr.io/...:vX.Y.Z` (et `:latest`) est présente ; `docker run` smoke : conteneur démarre, `GET /api/health` 200 (rappel : **2 Go RAM** requis pour le modèle IA/ONNX, §4).

## Garde-fous
- **Risky = approbation humaine** : publier une image/tag est difficilement réversible → **confirme avant le tag/push** si non explicitement autorisé ; **jamais** d'écrasement de tag ou d'image existante. (Cf. `WORKFLOWS.md` « release → needs-approval ».)
- **Secrets** : aucun token/clé (`AI_API_KEY`, `TMDB_API_KEY`, Fernet) dans l'image, les logs ou le repo ; injectés via env/`.env` à l'exécution.
- **Idempotence / retry** : rejouer une étape sans double effet (max 5 essais) ; build qui échoue → diagnostiquer (souvent Dockerfile/RAM/CI), pas de boucle.
- **Traçabilité** : bump commité sur `develop` puis mergé sur `main` (tag) ; rapport final (version, tag, URL image GHCR, résultat smoke `docker run` + `/api/health`).
