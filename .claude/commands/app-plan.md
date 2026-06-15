---
description: Planification — transforme un objectif en DAG de sous-issues dépendantes sur docs/31-board.md (Status/Depends on/Owner/Safe-Risky), via cto/tech-lead/tech-manager. GATE avant exécution.
argument-hint: <objectif à planifier, ex. "ajouter l'endpoint /api/ai/similar + cache + tests">
allowed-tools: Read, Write, Edit, Glob, Grep, Bash, Task, Agent
---

> 🟢 **PlexHub Backend — FastAPI/Python 3.13.** Branche `main`. Lis `.claude/WORKFLOWS.md` + `CLAUDE.md` §2/§3/§5/§9. Validation = `pytest -v` + boot `uvicorn app.main:app` + `GET /api/health` 200.

# /app-plan — découper un objectif en DAG de sous-issues

Objectif : $ARGUMENTS

Tu **planifies seulement** (pas de code). Sortie = un DAG de sous-issues sur `docs/31-board.md`, prêt à exécuter via `/feature` / `/fix-cleanroom`.

## Phases
1. **Cadrage archi** — `cto`/`tech-lead` (skill `engineering:system-design`) : cartographie l'impact (modules §2 : `api/`/`services/`/`workers/`/`db/`/`plex_generator/` ; flux §5 ; pièges §9 — migrations idempotentes, cold-start IA, `db_retry`/WAL, master-worker POSIX). Définit les **contrats** (routes, schémas Pydantic v2, OpenAPI) et les frontières des work-packages.
2. **Découpage en DAG** — `tech-manager` (skill `sprint-planner`) : crée les sous-issues sur `docs/31-board.md` avec colonnes **Status / Depends on / Owner / Safe-Risky** :
   - **Owner** par zone : `db-migration-specialist` (schéma/migrations), `sync-specialist` (Xtream/sync/enrichment), `ai-recsys-specialist` (embeddings/ranking/sqlite-vec), `plex-generator-specialist` (NFO/arbo), sinon `backend-developer`.
   - **Depends on** : arêtes explicites (ex. migration de schéma **avant** le service qui la consomme ; service **avant** son endpoint et ses tests).
   - **Safe-Risky** : marque `Risky·needs-approval` toute migration de schéma, durcissement CORS public, changement de contrat public, purge.
   - Identifie ce qui est **parallèle** (périmètres disjoints) vs **série** (dépendances).
3. **GATE** — présente le DAG (graphe de dépendances, parallèle vs série, classes Safe/Risky, owners) + un chemin critique estimé. **Attends le « go »** avant toute exécution. Ne lance aucun dev ici.

## Garde-fous
- **Planification ≠ exécution** : ce workflow ne modifie que `docs/31-board.md` (+ `docs/daily/<date>.md`). L'exécution passe par `/feature` (ou `/fix-cleanroom`).
- **Risky** explicitement tagué `needs-approval` dans le board.
- **Traçabilité** : chaque sous-issue a un ID, un owner, ses dépendances et sa classe. DoD rappelée par issue : `pytest -v` vert · serveur boote · `/api/health` 200 · migrations idempotentes · OpenAPI à jour si l'API change.
