---
name: sprint-planner
description: À utiliser pour convertir le backlog en un sprint backend exécutable, avec assignation parallèle des tickets et suivi des dépendances. Utilisé surtout par le tech-manager. Déclenché sur « planifie le sprint », « que fait le pod ensuite », ou dans le cadre de /feature (phase build).
---

# Sprint planner

Convertit `docs/10-prd.md` + `docs/22-impl-spec-backend.md` en `docs/30-sprint-plan.md` et
`docs/31-board.md`.

## Procédure

1. **Lis** le PRD et l'impl-spec backend. Note les dépendances de tickets — si PH-002 a besoin de la
   migration M0NN de PH-001, ou d'un service partagé que PH-001 modifie, c'est une dépendance.

2. **But du sprint** — une phrase en haut de `docs/30-sprint-plan.md`.

3. **Capacité** — roster réel d'agents backend (cf. `CLAUDE.md` §7). Par défaut : `backend-developer`
   + les spécialistes domaine pertinents (`db-migration-specialist`, `sync-specialist`,
   `ai-recsys-specialist`, `plex-generator-specialist`), `code-reviewer`, `security-reviewer`,
   `qa-engineer`, `integration-agent`. Ajuste au scope. Ne mobilise pas un spécialiste si aucun
   ticket ne touche son domaine.

4. **Assigne pour le parallélisme** — groupe les tickets pour que chaque agent ait du travail
   indépendant (périmètres de fichiers **disjoints**) à démarrer. Empile le travail dépendant derrière.

   ```
   Track A (db-migration-specialist)  : PH-001 (M010)
   Track B (sync-specialist)          : PH-002 → PH-005
   Track C (ai-recsys-specialist)     : PH-004 (depends on PH-001)
   Track D (plex-generator-specialist): PH-003 → PH-006
   Continu : code-reviewer, security-reviewer, qa-engineer
   ```

5. **Board** — `docs/31-board.md`. Les colonnes doivent matcher la forme de ticket du tech-manager :

   ```
   PH-NNN | F-NNN | Titre | Owner | Status | Depends on | Estimate | Spec | Acceptance | Notes
   ```

   - `F-NNN` est l'ID de feature PRD implémentée (pour que reviewers et QA tracent l'acceptation jusqu'au PRD).
   - `Owner` est un agent du roster (`backend-developer` ou un spécialiste domaine).
   - `Estimate` en taille relative (XS/S/M/L/XL).
   - `Spec` est une ancre courte comme `prd#F-001 + arch§3` pour que les devs n'aient pas à grepper.
   - `Acceptance` est le Given/When/Then copié du PRD (ou un résumé une-ligne + pointeur si long).
   - `Notes` porte le compteur de cycles de review (`cycles=0`, `cycles=1`, `cycles=2 → blocked`) et le lien `BUG-NNN-fix`.

   Status démarre à `todo`. Évolue via `in_progress → review → qa → done` (ou `blocked`).

6. **Definition of Done** — liste-la en haut du board pour que tout le monde utilise la même :
   - Code mergé sur **`main`**
   - `pytest -v` **vert**
   - boot OK : `uvicorn app.main:app` démarre
   - `GET /api/health` répond **200**
   - migrations **idempotentes** (re-run sans casse)
   - **OpenAPI à jour**
   - code-reviewer approuvé (+ security-reviewer si auth/secrets/crypto touchés)
   - QA a exercé les critères d'acceptation
   - le rapport quotidien mentionne la clôture

## Règles de parallélisme

- **En parallèle** quand les tickets A et B touchent des modules/fichiers différents.
- **Sérialise** quand la sortie de A est l'entrée de B (migration de schéma, service partagé,
  changement de contrat d'API).
- **Ne spawn jamais plus d'agents en parallèle qu'il n'y a de tickets indépendants prêts.** Les
  agents oisifs gaspillent des tokens ; les agents qui se chevauchent gaspillent le contexte des
  autres en conflits de merge.

## P0 vs stretch

Marque explicitement chaque ticket **P0** (le sprint échoue sans lui) ou **stretch** (pris si la
capacité reste). Ne planifie jamais un stretch devant un P0 dont il dépend. La capacité réelle prime
sur l'ambition : mieux vaut moins de tickets tous fermés (DoD complète) qu'un board à moitié vert.

## Format de sortie pour le handoff au tech-manager

```
SPRINT 1 LANCÉ
But : <une phrase>
Lancement parallèle :
- db-migration-specialist  ← PH-001 (M010)               [P0]
- sync-specialist          ← PH-002, PH-005              [P0]
- plex-generator-specialist← PH-003                      [P0]
- ai-recsys-specialist     ← PH-004 (après PH-001 done)  [P0]
File reviewers : code-reviewer (+ security-reviewer si auth/secrets)
QA : qa-engineer ← plan de test sprint 1
Stretch : PH-006 (plex-generator) si capacité
Rapport quotidien : docs/daily/<date>.md
DoD : pytest -v vert · boot uvicorn app.main:app · /api/health 200 · migrations idempotentes · OpenAPI à jour
```
