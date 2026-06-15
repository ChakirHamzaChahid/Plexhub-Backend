---
description: Workflow Refacto — Architecte (plan par étapes + ADR + contrats) → Migration fichier par fichier → Validation régressions → boucle de correction
argument-hint: <cible de refonte, ex. "extraire le ranking de recommendation_service">
allowed-tools: Read, Write, Edit, Glob, Grep, Bash, Task, Agent
---

> 🟢 **PlexHub Backend — FastAPI/Python 3.13.** Branche `main`. Lis `.claude/WORKFLOWS.md` + `CLAUDE.md` §2/§3/§5/§9. Validation = `pytest -v` + boot `uvicorn app.main:app` + `GET /api/health` 200. **Pas de big-bang : migration par étapes.**

# /refacto — refonte sûre en multi-agents

Cible : $ARGUMENTS

## Phases
1. **Architecte** — `tech-lead` (skills `engineering:architecture` + `architecture-decision-records`) : analyse le code existant (réutilise `docs/architecture/ARCHITECTURE.md` ; ne relance `a0-cartographer` que si périmé), **cartographie les dépendances**, produit un **plan de migration par étapes** + **contrats** (signatures/ports/schémas Pydantic stables) + un ADR. Identifie les pièges §9 touchés (migrations idempotentes, cold-start IA, `db_retry`/WAL, `asyncio.to_thread` pour les appels bloquants, master-worker POSIX). **GATE** : valide le plan avant de toucher au code.
2. **Migration** — `backend-developer` (ou le spécialiste domaine concerné : `db-migration-specialist`, `sync-specialist`, `ai-recsys-specialist`, `plex-generator-specialist`) applique **fichier par fichier / étape par étape**, en gardant le comportement (caractérisation par tests d'abord si zone non couverte). Périmètre strict, conventions §3.
3. **Validation** — `qa-engineer` (tests de non-régression `pytest`) + `perf-benchmarker` (si chemin chaud : re-mesure latence endpoints serveur lancé, avant/après). Chaque étape doit rester verte (et `/api/health` 200) avant la suivante.
4. **Boucle de correction** — `tech-manager` renvoie les échecs (tests/review/perf) à l'agent d'implémentation **jusqu'à état stable**. `code-reviewer` gate à chaque étape. Cap 2 cycles/étape.

## Garde-fous (refonte = Risky par défaut → needs-approval)
- **Gros moteur (services IA, `plex_generator`, schéma DB)** = **vague isolée**, retest complet avant merge.
- **Migration de schéma SQLite** = `needs-approval` : migrations **idempotentes** (`IF NOT EXISTS`, `ADD COLUMN` gardé), ajoutées **en fin** de `run_migrations()` (§9). Rejouables sans erreur. Pas de wipe.
- Préserver les contrats publics (routes, schémas Pydantic) tant que possible ; tout changement de signature = annoncé + adaptateurs + OpenAPI à jour.
- Pas de big-bang : commits petits et verts ; rollback simple par étape.

## DoD (chaque étape)
`pytest -v` vert · serveur boote · `GET /api/health` 200 · migrations idempotentes · `ruff check` (si câblé) · OpenAPI à jour si l'API change.
