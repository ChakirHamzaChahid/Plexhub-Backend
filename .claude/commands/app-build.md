---
description: Exécute le sprint — lance les devs en parallèle, review en streaming de chaque lot, gate DoD, QA, bugs réinjectés dans la boucle
argument-hint: [IDs de tickets, optionnel — défaut = tous les tickets prêts]
allowed-tools: Read, Write, Edit, Glob, Grep, Bash, Task, Agent
---

> 🟢 **PlexHub Backend — FastAPI/Python 3.13.** Dév **directement sur `develop`** (pas de branche par tâche ; `main` = release only). Lis `.claude/WORKFLOWS.md` (garde-fous + **politique de routage modèle×effort** — skill `model-effort-routing`) + `CLAUDE.md` §2/§3/§5/§9. Validation d'un lot = `pytest -v` + boot `uvicorn app.main:app` + `GET /api/health` 200 + migrations idempotentes + `ruff check`.

# /app-build — Exécuter le sprint

Tickets (optionnel, défaut = tous les tickets prêts) : $ARGUMENTS

## Étapes

1. **Lire l'état.**
   - `docs/31-board.md` — repère les tickets `Status = todo` dont tous les IDs `Depends on` sont `done`.
   - `docs/51-bugs.md` (s'il existe) — pour chaque `S1`/`S2` ouvert, vérifie qu'une ligne `BUG-NNN-fix` existe sur le board ; sinon, spawn `tech-manager` une fois pour les créer, puis relis le board.

2. **Spawn les développeurs en parallèle.** Utilise le skill `parallel-orchestrator` et consulte `model-effort-routing` pour le couple (modèle, effort) de chaque invocation. Lance les agents IC concurremment dans **un seul message** — une invocation par owner, chacune avec la liste complète des tickets qu'il travaille ce round :
   - `backend-developer` pour les tickets génériques FastAPI ;
   - les **spécialistes domaine** selon l'Owner du board : `db-migration-specialist` (schéma/migrations), `sync-specialist` (Xtream/enrichment), `ai-recsys-specialist` (embeddings/vec), `plex-generator-specialist` (NFO/arbo).
   Lance `qa-engineer` dans ce même message quand son travail est prêt (draft de plan de test dès l'impl-spec).
   ⚠️ **Périmètres de fichiers disjoints** entre agents parallèles (tous commitent sur `develop`) ; sérialise ce qui partage le schéma DB / les services communs.

3. **Review en streaming.** Dès qu'un dev rend `DONE: APP-NNN` :
   - Passe la ligne du board à `Status = review`.
   - Spawn immédiatement un `code-reviewer` sur le **lot de commits du ticket** (`git log`/`git diff` des commits du ticket sur `develop`) — n'attends pas les autres devs. Ajoute `security-reviewer` si la surface est sensible (auth, secrets, entrée utilisateur, CORS, écriture disque). Plusieurs reviewers peuvent tourner en parallèle, y compris pendant que d'autres devs travaillent encore.

4. **Traiter les verdicts.**
   - `APPROVED` → spawn `tech-manager` pour le **gate de lot** (DoD : `pytest -v` vert · boot `uvicorn app.main:app` · `GET /api/health` 200 · migrations rejouables · `ruff check` propre · OpenAPI à jour si l'API change). Gate vert → la ligne passe `review → qa`.
   - `REQUEST CHANGES` → re-spawn le dev d'origine avec les notes bloquantes (applique l'escalade `model-effort-routing` : prompt enrichi → override `model:` → changement d'agent). Trace le compteur dans la colonne `Notes` du board : `cycles=N`.
   - **Cap : 2 cycles de review.** Au 3ᵉ `REQUEST CHANGES`, stoppe la boucle pour ce ticket, statut `blocked`, et remonte à l'utilisateur l'historique complet reviewer + dev. Pas d'auto-retry au-delà.

5. **Passe QA.** Quand une vague de tickets est en `qa`, spawn `qa-engineer` une fois pour dérouler les critères d'acceptation (tests HTTP réels sur serveur booté quand pertinent). QA écrit les nouveaux défauts dans `docs/51-bugs.md`. Les S1/S2 reviennent dans la boucle à l'étape 1 du round suivant.

6. **Rapport quotidien.** Collecte les fragments `docs/daily/<today>-*.md` et spawn `tech-manager` pour les concaténer en `docs/daily/<today>.md`.

7. **Boucle** jusqu'à ce que le board n'ait plus de `todo` prêt ni rien en `review`/`qa`. Puis imprime le résumé de sprint et suggère `/app-plan` (sprint suivant), `/app-ship` (release), ou `/app-status` (inspection).

## Sécurité

- Jamais deux agents sur le même ticket simultanément.
- Jamais de gate de lot validé par-dessus un `REQUEST CHANGES`.
- Jamais de re-spawn au-delà du cap 2 cycles sans décision utilisateur.
- Si un dev rend `BLOCKED: APP-NNN`, remonte le blocage **verbatim** et stoppe ce ticket ; n'invente pas de réponse.
- Ticket `Risky·needs-approval` (migration de schéma, contrat public, purge) → n'exécute qu'après approbation humaine explicite.
