---
description: Workflow Feature — orchestrateur → requirements → architecture+DAG → dev (spécialistes domaine) → tests → review/audit, via les agents studio
argument-hint: <objectif de la feature, ex. "ajouter un endpoint /api/ai/similar">
allowed-tools: Read, Write, Edit, Glob, Grep, Bash, Task, Agent
---

> 🟢 **PlexHub Backend — FastAPI/Python 3.13.** Branche `main`. Lis `.claude/WORKFLOWS.md` + `CLAUDE.md` §2/§3/§5/§9. Validation = `pytest -v` + boot `uvicorn app.main:app` + `GET /api/health` 200.

# /feature — livrer une fonctionnalité en multi-agents

Objectif : $ARGUMENTS

Tu es l'**orchestrateur**. Construis un **DAG de sous-tâches dépendantes** puis lance les agents spécialisés en parallèle sur les tâches non bloquantes (périmètres de fichiers disjoints). Ne code pas toi-même.

## Phases
1. **Requirements** — `cpo` (skill `product-management:write-spec`) : transforme l'objectif en **user stories + critères d'acceptation** + non-goals + métriques + impact contrat d'API. Sortie : `docs/10-prd-<feature>.md`. Pose UNE question si l'intention produit est ambiguë.
2. **Architecte + DAG** — `cto`/`tech-lead` (skill `engineering:system-design`) : conçoit l'archi (logique métier dans `services/`/`workers/`, routers `api/` = validation+délégation §2 ; schémas Pydantic v2 aux frontières §3 ; impact schéma SQLite/migrations → **propriétaire `db-migration-specialist`** ; pièges §9) + **contrats d'API** (OpenAPI). Puis `tech-manager` (skill `sprint-planner`) découpe en **sous-issues dépendantes** sur `docs/31-board.md` (colonnes Status/Depends on/Owner/Safe-Risky), arêtes de dépendance explicites. **GATE** : présente le découpage + ce qui est parallèle vs série ; attends le « go ».
3. **Dev en parallèle** (un message, une Task par owner, périmètres disjoints) — `backend-developer` mène ; **déléguer au spécialiste domaine selon la zone** :
   - `db-migration-specialist` — schéma SQLite / migrations (`db/migrations.py`, **idempotentes en fin de chaîne** §9).
   - `sync-specialist` — Xtream / sync / enrichment (`services/xtream_service`, `workers/sync_worker`, `enrichment_worker`).
   - `ai-recsys-specialist` — embeddings / ranking / sqlite-vec (`services/embedding_service`, `recommendation_service`, `api/ai.py`).
   - `plex-generator-specialist` — génération NFO/arbo (`plex_generator/**`).
   - `backend-developer` — routers/endpoints `api/`, services génériques, utils, si pas de spécialiste dédié.
4. **Test** — `qa-engineer` : tests `pytest` (pytest-asyncio mode auto, `respx` pour httpx) couvrant les critères d'acceptation ; exécute la validation.
5. **Review / Audit** — `code-reviewer` (qualité/conventions §3/§9) + `security-reviewer` (si surface sensible : auth `X-API-Key`, secrets/Fernet, entrée utilisateur, CORS) + `perf-benchmarker` (si chemin chaud touché : cold-start IA, latence endpoints). Merge par `tech-manager`.

## DoD (chaque sous-issue)
`pytest -v` vert · serveur boote (`uvicorn app.main:app`) · `GET /api/health` 200 · **migrations idempotentes** (rejouables sans erreur) · `ruff check` propre (si câblé) · **OpenAPI/contrat à jour** si l'API change. Boucle de correction max 5 essais ; cap 2 cycles review puis `blocked`.

## Sûreté
Risky (migration de schéma, refacto large, secrets, release) = `needs-approval`. Éditions de contrats **additives** quand possible. Jamais d'auto-merge par-dessus un `REQUEST CHANGES`. `BLOCKED` remonté verbatim. Secrets jamais en clair (tokens, clés API, Fernet).

> Raccourci : ce workflow s'appuie sur `/app-plan` (DAG) pour le découpage. `/feature` ajoute la phase Requirements en amont et la DoD backend.
