---
description: Applique les correctifs perf priorisés (impact/effort) issus de /benchmark, puis re-mesure avant/après sur serveur lancé. Délègue au spécialiste domaine du goulot.
argument-hint: <optionnel : goulot/correctif ciblé ex. "cache embedding sqlite-vec" ; sinon la liste priorisée du dernier /benchmark>
allowed-tools: Read, Write, Edit, Glob, Grep, Bash, Task, Agent
---

> 🟢 **PlexHub Backend — FastAPI/Python 3.13.** Branche `main`. Lis `.claude/WORKFLOWS.md` + `CLAUDE.md` §5/§9. Validation = `pytest -v` + boot `uvicorn app.main:app` + `GET /api/health` 200.

# /fix-bench-perf — corriger la perf, mesure à l'appui

Cible : $ARGUMENTS  *(correctif ciblé, sinon la liste priorisée du dernier `/benchmark`)*

## Phases
1. **Plan** — reprends le breakdown + la **liste priorisée (impact/effort)** du dernier `/benchmark`. Confirme le goulot par une mesure de référence (baseline `curl -w` / `ab` / `locust`, serveur lancé). Ordonne : plus gros impact / moindre risque d'abord.
2. **Correctif** — délègue au **spécialiste du goulot** : `ai-recsys-specialist` (cold-start/embedding/sqlite-vec/cache `ai_tmdb_cache`), `sync-specialist` (Xtream/enrichment, batchs/concurrence), `db-migration-specialist` (index, requêtes, `db_retry`/WAL), `plex-generator-specialist` (I/O images/NFO), sinon `backend-developer`. Patch ciblé ; **ne pas casser un contrat IA** (les 3 motifs 503 §9, caps 20 fetches/`/rank`, ranking `tv`/`movie` uniquement).
3. **Re-mesure avant/après** — `perf-benchmarker` rejoue exactement les mêmes scénarios ; tableau **avant → après** (p50/p95/p99, débit), gain chiffré. Refuse un correctif sans gain mesurable ou qui régresse un autre endpoint.
4. **Gate & merge** — `code-reviewer` (+ `qa-engineer` pour la non-régression) ; merge par `tech-manager`. Cap 2 cycles puis `blocked`.

## DoD
`pytest -v` vert · serveur boote · `GET /api/health` 200 · migrations idempotentes (si touché) · **gain perf chiffré avant/après** · pas de régression fonctionnelle (contrats IA intacts) · `ruff check` (si câblé).

## Garde-fous
- **Pas d'optim à l'aveugle** : tout correctif est justifié par une mesure avant/après.
- Risky (migration de schéma pour index, changement de contrat) → `needs-approval`.
- Idempotence/retry (max 5 essais) ; traçabilité chiffrée dans `docs/daily/<date>.md`.
