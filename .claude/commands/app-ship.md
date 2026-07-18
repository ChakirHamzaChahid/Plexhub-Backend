---
description: Prépare une release — gates préalables (board propre, sécurité, QA, cohérence transverse) puis délègue le pipeline à /release. Ne pousse jamais sans confirmation.
argument-hint: [version cible, ex. 1.5.0, optionnel]
allowed-tools: Read, Write, Edit, Glob, Grep, Bash, Task, Agent
---

> 🟢 **PlexHub Backend — FastAPI/Python 3.13.** Release = merge `develop`→`main` + tag `vX.Y.Z` + image Docker GHCR (pipeline détaillé = **`/release`** / `release-manager`). Release = action **Risky → needs-approval**, jamais en auto.

# /app-ship — Couper une release

Version (optionnel, sinon `release-manager` la choisit depuis `APP_VERSION` dans `app/main.py`) : $ARGUMENTS

## Étapes

1. **Sanity check du board.** Lis `docs/31-board.md`. S'il reste du `todo`, `in_progress` ou `review` dans le lot courant, stoppe et dis « Sprint pas fini — lance `/app-build` d'abord. »

2. **Spawn `security-reviewer`, `qa-engineer` et `integration-agent` en parallèle** dans un seul message :
   - `security-reviewer` produit `docs/70-security-review.md` (auth fail-closed, secrets/Fernet, CORS, SSRF, confinement F-007). Un `critical`/`high` ouvert → stop.
   - `qa-engineer` déroule la passe de ship : `pytest -v` complet, boot `uvicorn app.main:app`, `GET /api/health` 200, migrations rejouées sans erreur sur une DB copiée. Retourne `QA READY` ou `QA BLOCKED` avec la liste.
   - `integration-agent` vérifie la cohérence transverse : OpenAPI ⇆ schémas Pydantic, migrations ⇆ entités `models/database.py`, `APP_VERSION` ⇆ CHANGELOG/notes.
   Si l'un remonte un bloquant, stoppe et remonte la liste combinée ; ne pas passer à la release.

3. **Spawn `release-manager`** avec la version (si donnée) et la checklist de préconditions — il exécute le pipeline `/release` (bump `APP_VERSION` sur `develop` → merge `develop`→`main` → tag `vX.Y.Z` → build+push image GHCR → vérif). Il rend `SHIP CANDIDATE: vX.Y.Z` ou `BLOCKED: ...` **avant** toute action irréversible.

4. **Si SHIP CANDIDATE** : imprime la sortie de `release-manager` verbatim. Pose UNE question avant toute commande sortante : « Confirmer le merge develop→main + tag vX.Y.Z + push image GHCR ? » Ne pousse rien sans confirmation explicite.

5. **Si BLOCKED** : imprime la liste des bloquants et le déblocage proposé. Suggère la bonne commande (`/app-build` pour les tickets manquants, `/incident` pour un bug avéré, `/app-status` pour le contexte).

## Sécurité

- Jamais d'auto-confirmation du merge/tag/push (release = sortant, difficilement réversible).
- Jamais de ship par-dessus un bug S1/S2 ouvert ou un finding sécurité critique.
- Jamais de bump de version majeure sans instruction utilisateur explicite.
- Après release : si le lot a touché modules/§5/§3, `/sync-context` avant de clôturer.
