---
description: Note le backend contre le House KB — fan-out des auditeurs spécialistes, rapport de gaps classé par sévérité, backlog de remédiation, puis fixes (Safe automatiques, Risky sur approbation)
argument-hint: [dimension ou "all" — ex. security, api, devops, observability ; défaut = all]
allowed-tools: Read, Write, Edit, Glob, Grep, Bash, Task, Agent
---

> 🟢 **PlexHub Backend — FastAPI/Python 3.13.** Autorité = `CLAUDE.md` §2/§3/§5/§9/§10. Audit = lis aussi `docs/audit/cleanroom-<dernier>/` (findings `CR-*`).

# /app-audit — Noter le backend, puis fermer les gaps

> 🟢 **MODE PLEXHUB BACKEND — l'audit existe déjà.** Ne **PAS** relancer un audit à blanc ni fan-out d'auditeurs.
> L'audit clean-room indépendant est dans **`docs/audit/cleanroom-<dernier>/`** (56 findings `CR-*`, recoupés au code) et a déjà été **bridgé** : `docs/31-board.md` = backlog `CR-*` (statuts de remédiation à jour, log `docs/audit/cleanroom-<dernier>/README.md`).
> **Procédure PlexHub Backend** : (1) lire le README de l'audit + `docs/31-board.md` ; (2) si l'audit a >~2 semaines OU le code a beaucoup bougé (HEAD ≠ bandeau CLAUDE.md), proposer un re-audit via **`/audit-cleanroom`** (pas via les auditeurs ci-dessous) ; (3) sinon aller directement au **GATE (étape 4)** : présenter la scorecard + le backlog groupé par sévérité et Safe/Risky, demander le périmètre, puis remédier via **`/fix-cleanroom`** (le flux natif) ou `/app-build`. Les étapes 1-3 « fan-out » ne s'appliquent **que** pour un service sans audit clean-room.

Dimension (optionnel, défaut = all) : $ARGUMENTS

## Préconditions

- Nécessite la baseline as-built de `/app-onboard` (au moins `docs/20-architecture.md` ou `docs/architecture/ARCHITECTURE.md`). Absente → lance `/app-onboard` d'abord (ou `/refresh-context` sur ce repo), puis continue.
- Invoque `house-conventions` et `brownfield-onboarding` avant d'auditer.

## Étapes

1. **Fan-out des auditeurs en parallèle** (un seul message). Chacun vérifie UNE dimension contre le pack House KB qu'il possède et rend ses findings, en lecture seule :
   - **Code Python/FastAPI** — `code-reviewer` vs `python-conventions.md` + `api-conventions.md` (async partout, logique dans `services/`/`workers/` pas dans les routers, Pydantic v2 camelCase aux frontières, `HTTPException` fail-closed, writers via `db_retry`), plus résultats `ruff check` / `pytest --cov`.
   - **Sécurité** — `security-reviewer` (auth `X-API-Key` fail-closed sur tout `/api/*`, secrets jamais persistés/loggés, Fernet au repos, CORS, SSRF `follow_redirects`, confinement d'écriture F-007).
   - **DevOps** — `devops-engineer` vs `git-workflow.md` (modèle de branches develop/main, CI pytest+lint, bornes de dépendances liées fastapi/instrumentator, hygiène secrets `.env`).
   - **Observabilité** — `observability-analyst` vs `observability.md` (métriques Prometheus métier, logs `request_id`, couverture des jobs planifiés).
   - **Perf** — `perf-benchmarker` (chemins chauds : listes unified, recherche, génération — sur serveur booté si possible).
   Si une dimension unique est nommée dans $ARGUMENTS, ne lance que celle-là.

2. **Consolider dans `docs/80-audit.md`.** Chaque finding porte :
   - une sévérité `S1`–`S4`,
   - la **règle House KB exacte violée** (ex. « api-conventions §réponses — dict brut sans `response_model` »),
   - un tag **Safe / Risky** selon la classification `brownfield-onboarding`,
   - un correctif recommandé en une ligne.
   Ouvre par une scorecard : passes/gaps par dimension et top risques.

3. **Construire le backlog de remédiation.** Spawn `tech-manager` pour transformer les findings en tickets `AUDIT-NNN` sur `docs/31-board.md`, priorisés par sévérité, chacun portant sa règle violée + tag Safe/Risky. Les tickets Risky reçoivent un court plan écrit et sont marqués `needs-approval`.

4. **GATE — présenter le résumé des gaps.** Imprime la scorecard et le backlog groupé par sévérité et Safe/Risky. Demande quoi corriger (ex. « tous les S1/S2 », « safe-only », une sélection). Attends.

5. **Remédier** via la boucle `/app-build` normale (ou `/fix-cleanroom` si les tickets sont des `CR-*`) sur les tickets approuvés :
   - **Safe** : corrigés automatiquement, gatés par `code-reviewer` et le cap 2 cycles.
   - **Risky** : exécutés seulement après approbation au gate ; le dev suit le plan du ticket, et les migrations suivent la règle maison (idempotentes, ajoutées en fin de chaîne, jamais de DDL destructif sans accord).

6. **Re-audit.** Après la vague de fixes, propose de relancer `/app-audit` pour confirmer la fermeture des gaps et mettre à jour `docs/80-audit.md`.

## Sécurité

- Les auditeurs sont en lecture seule ; aucun changement de code avant le GATE.
- Jamais d'exécution auto d'un changement Risky (migration, refonte large, contrat public, purge) — approbation explicite à l'étape 4.
- Remonter, pas enterrer : un finding ambigu est listé comme finding, pas deviné.
