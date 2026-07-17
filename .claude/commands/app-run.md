---
description: Run quasi-autonome bout-en-bout — idée → scope-lock → boucle de sprint → ship-readiness, ne remontant que les blocages et les deux gates humains (scope-lock, ship)
argument-hint: [idée en une ligne, optionnel] [--yolo pour sauter le scope-lock]
allowed-tools: Read, Write, Edit, Glob, Grep, Bash, Task, Agent
---

> 🟢 **PlexHub Backend — FastAPI/Python 3.13.** Dév directement sur `develop`. Lis `.claude/WORKFLOWS.md` (garde-fous + routage modèle×effort `model-effort-routing`) + `CLAUDE.md`.

# /app-run — Piloter tout le chantier, quasi seul

Idée / flags : $ARGUMENTS

C'est le driver autonome. Il enchaîne init/onboard → plan → build → standup → boucle jusqu'à sprint terminé ou vrai blocage. **Seules deux choses arrêtent pour l'utilisateur : le scope-lock et le ship.** Tout le reste défile en rapports de standup. Enrobe cette commande dans `/loop` pour une exécution auto-cadencée.

## Règles d'exploitation

- L'équipe **n'invente jamais l'intention.** Quand un requirement est réellement ambigu, écris le blocage dans le standup et remonte-le verbatim — ne devine pas.
- Tous les agents de build invoquent le skill `house-conventions` avant de travailler (House KB = `.claude/knowledge/`).
- Respecte les rails existants : cap 2 cycles de review, pas de gate validé par-dessus un `REQUEST CHANGES`, un agent par ticket à la fois, aucune action destructive sur les données, Risky = `needs-approval`.

## Étapes

1. **Détecter greenfield vs brownfield.** Via la détection du skill `brownfield-onboarding`, vérifie si le répertoire cible contient déjà un service.
   - **Vide / pas de service → greenfield :** lance `/app-init` avec l'idée (requirements-intake → vision CEO → CPO/CTO en parallèle → tech-lead/devops-engineer en parallèle → bootstrap projet incl. `CLAUDE.md` + git).
   - **Service existant → brownfield :** lance `/app-onboard` (baseline as-built + `CLAUDE.md`), puis `/app-audit` (note vs House KB → `docs/80-audit.md` → backlog de remédiation). Si une idée/objectif a été donné, traite-le comme objectif d'upgrade et ajoute-le en tickets feature à côté des tickets `AUDIT-NNN`. **Sur CE repo** : la baseline existe déjà (`CLAUDE.md` + audit clean-room) — saute directement à la lecture de `docs/31-board.md`.

2. **GATE 1 — scope-lock / approbation d'audit (humain).**
   - *Greenfield :* imprime un brief d'un écran — vision, liste P0, headline d'architecture, effort estimé, top risque — et demande *« Périmètre approuvé, on lance le build ? »*
   - *Brownfield :* imprime la scorecard d'audit et le backlog groupé par sévérité et Safe/Risky, et demande *« Quels gaps corrige-t-on ? »* Les changements Risky ne partent que s'ils sont approuvés ici.
   Attends la réponse. Avec `--yolo`, saute le gate (greenfield : périmètre auto-approuvé ; brownfield : fixe tous les S1/S2 + Safe, diffère les Risky) et logge la décision.

3. **Plan.** Lance `/app-plan` (`cto`/`tech-lead` + `tech-manager` construisent le DAG parallèle du board via `sprint-planner`). Vérifie que chaque feature P0 a ses tickets de tests/garde associés.

4. **Boucle de build.** Lance la boucle `/app-build` en autonomie, round après round :
   - devs parallèles (`backend-developer` + spécialistes domaine selon Owner) → review streaming `code-reviewer` → gate de lot `tech-manager` (DoD : pytest + boot + `/api/health` + migrations + ruff) → `qa-engineer` → boucle bugs.
   - Après chaque round, spawn `tech-manager` pour écrire `docs/daily/standup-<today>.md` et imprime un standup de 3 lignes : comptes par statut, ce qui est passé, blocages.
   - **N'escalade à l'utilisateur que** pour : un blocage insoluble par l'équipe, le cap 2 cycles atteint, ou un conflit de périmètre/architecture. Remonte verbatim avec une réponse proposée.

5. **Ship-readiness.** Quand le board est drainé et zéro bug S1/S2 ouvert, spawn en parallèle : `security-reviewer` (passe sécurité ship), `integration-agent` (cohérence OpenAPI/migrations/version) et `observability-analyst` (métriques + logs des chemins P0 instrumentés).

6. **GATE 2 — ship (humain).** Passe la main à `/app-ship`, qui résume la readiness et demande une confirmation explicite avant tout merge `develop`→`main` / tag / push d'image. Jamais de push sans elle.

## Sortie

Un service backend conforme aux conventions du House KB, tous les docs plan/standup sous `docs/`, et un résumé final : ce qui a shippé, ce qui est différé (S3/S4), et la prochaine étape suggérée.
