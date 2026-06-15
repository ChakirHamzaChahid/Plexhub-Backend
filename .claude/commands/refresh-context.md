---
description: Régénère le contexte (CLAUDE.md + docs/architecture/ARCHITECTURE.md) contre le code à HEAD. Lecture seule du code applicatif. Délègue au cartographe a0-cartographer.
allowed-tools: Read, Write, Edit, Glob, Grep, Bash, Task
---

> 🟢 **PlexHub Backend — FastAPI/Python 3.13.** Branche `main`. Lis `.claude/WORKFLOWS.md` + `CLAUDE.md` §2/§3/§5/§9. Validation = `pytest -v` + boot `uvicorn app.main:app` + `GET /api/health` 200.

# /refresh-context — re-cartographie complète @ HEAD

Objectif : remettre la documentation de contexte **à jour avec le code à HEAD**, pour qu'aucun agent
ne travaille sur une carte périmée. **Re-cartographie complète** (≠ `/sync-context` léger).

Étapes (Manager) :
1. Capture le repère : `git rev-parse --short HEAD` + date.
2. **Base prioritaire = la cartographie clean-room la plus récente** si elle existe : cherche
   `docs/audit/cleanroom-*/cartography.md` (la plus récente). C'est une **photo fraîche, indépendante
   et vérifiée `fichier:ligne`** du code — utilise-la comme **source de référence** pour régénérer
   `ARCHITECTURE.md` et les sections d'archi de `CLAUDE.md`, **de préférence au contenu périmé**.
   Recoupe quand même au code (une cartographie peut comporter des erreurs) et note tout écart.
   Si aucune `cartography.md` n'existe, régénère directement depuis le code.
3. Délègue à l'agent **`a0-cartographer`** (lecture seule du code) la **régénération** de :
   - `CLAUDE.md` : modules (§2 `app/`), conventions+références (§3), flux clés (§5), pièges (§9),
     état réel/stack (§10), tables d'ownership/skills (§7/§7bis) — et **met à jour le bandeau de
     fraîcheur en tête** (`> 🕒 À JOUR AU : <date> (HEAD <hash>)`).
   - `docs/architecture/ARCHITECTURE.md` : doc d'archi profond (graphe de modules, **stack/versions
     RÉELLES** lues dans `requirements.txt`/`requirements-dev.txt`, schéma SQLite actuel + chaîne de
     migrations `db/migrations.py`, flux `services/`/`workers/`, dette) — **retire tout bandeau PÉRIMÉ**
     une fois le contenu régénéré et corrige les faits (numéro de migration courant, fastembed/sqlite-vec…).
4. Chaque fait DOIT être prouvé par **`fichier:ligne`** (pas de reprise aveugle de l'ancien contenu).
   Marque « à confirmer » ce qui n'est pas vérifiable.
5. Commit sur `main` (`docs: refresh context @ HEAD <hash>`). Présente un court diff des changements
   de fond (ce qui était périmé et a été corrigé).

À lancer **avant toute grosse campagne** (run de missions, audit) et **après** des changements
structurels (schéma SQLite, modules, flux).
