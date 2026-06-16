---
description: Remédiation board complet des findings d'audit CR-* — exécution via cleanroom-fixer, gate code-reviewer, merge tech-manager. Boucle jusqu'à board vert.
argument-hint: <optionnel : IDs ciblés ex. "CR-SEC-001 CR-PERF-003" ; sinon tout le board>
allowed-tools: Read, Write, Edit, Glob, Grep, Bash, Task, Agent
---

> 🟢 **PlexHub Backend — FastAPI/Python 3.13.** Dév **directement sur `develop`** (pas de branche par tâche ; `main` = release only). Lis `.claude/WORKFLOWS.md` + `CLAUDE.md` §2/§3/§5/§9. Validation = `pytest -v` + boot `uvicorn app.main:app` + `GET /api/health` 200.

# /fix-cleanroom — remédier les findings d'audit

Cible : $ARGUMENTS  *(IDs `CR-*` ciblés, sinon tout le board)*

## Phases
1. **Charger le board** — lis le dernier `docs/audit/cleanroom-<date>/` + `docs/80-audit.md` + `docs/31-board.md`. Construis l'ordre d'attaque : **priorité × (Safe avant Risky)**, dépendances explicites. Les findings `Risky` (migration de schéma, secrets, CORS façade publique) restent `needs-approval`.
2. **Remédiation** — délègue chaque finding à l'agent **`cleanroom-fixer`** (déléguant lui-même au spécialiste domaine : `db-migration-specialist`, `sync-specialist`, `ai-recsys-specialist`, `plex-generator-specialist`, sinon `backend-developer`). Patch minimal ciblant la cause du finding ; périmètres disjoints en parallèle, dépendances en série.
3. **Gate review** — `code-reviewer` (qualité/conventions §3/§9) + `security-reviewer` (findings `CR-SEC-*` : auth, secrets/Fernet, CORS, entrée utilisateur) à chaque lot. **Cap 2 cycles** par finding puis `blocked` + remontée.
4. **Merge** — `tech-manager` intègre les lots verts, met à jour `docs/31-board.md` (Status `done`) et le score dans `docs/80-audit.md`.

## DoD (chaque finding)
`pytest -v` vert · serveur boote · `GET /api/health` 200 · **migrations idempotentes** (rejouables) · `ruff check` (si câblé) · OpenAPI à jour si l'API change · finding marqué résolu (preuve `fichier:ligne`).

## Garde-fous
- **Risky = approbation humaine** : migration de schéma, purge, changement de contrat public, durcissement CORS public → `needs-approval`.
- Pas d'auto-merge par-dessus un `REQUEST CHANGES` ; `BLOCKED` remonté verbatim.
- Idempotence/retry : une étape échouée se rejoue (max 5 essais) avant escalade.
- Traçabilité : board + `docs/daily/<date>.md` tenus à jour ; chaque correctif tracé au finding.
