---
name: parallel-orchestrator
description: Règles de lancement des sous-agents depuis /feature, /app-build, /fix-cleanroom ou le tech-manager. Depuis la revue du 2026-09-24 — UN SEUL rédacteur à la fois (les devs s'enchaînent) ; le parallèle est réservé aux agents en lecture (revues, exploration, audit). Nom historique conservé pour ne casser aucune référence.
---

# Orchestration des sous-agents — un rédacteur, des lecteurs en parallèle

> Le nom « parallel-orchestrator » est historique. Depuis le 2026-09-24, **on ne lance plus de développeurs en parallèle.**

## Pourquoi
- Chaque écriture porte des décisions implicites (nommage, contrat, gestion d'erreur) que l'autre rédacteur ne voit pas : deux devs parallèles produisent un code incohérent que la revue doit ensuite réconcilier (Cognition, « Don't Build Multi-Agents »).
- Anthropic observe que le code se prête mal au multi-agent (peu de tâches vraiment parallélisables, beaucoup de dépendances) et qu'un système multi-agent consomme de l'ordre de 15× les tokens d'un chat (*How we built our multi-agent research system*).
- `develop` n'a qu'un index git : deux rédacteurs simultanés mélangent leurs fichiers stagés.
- Les worktrees ne sont pas une échappatoire ici : voir `WORKFLOWS.md` § « Rédacteur unique ».

## Règles
1. **Un seul agent qui écrit à la fois** (`backend-developer` ou un spécialiste domaine). Les tickets prêts passent l'un après l'autre, dans l'ordre du board : dépendances d'abord, puis P0, puis Safe avant Risky. Une chaîne de tickets d'un même owner = **un seul** agent pour toute la chaîne.
2. **Le parallèle est pour les lecteurs** : relecteurs d'un même diff (`code-reviewer` (+ `security-reviewer` si déclencheur)) dans le même message, explorations, cartographies, audits — et `qa-engineer` s'il ne fait que **rédiger** son plan dans son rapport (l'orchestrateur l'écrit ensuite dans `docs/`).
3. **Un relecteur peut tourner pendant que le dev suivant écrit**, à condition de lire un **commit figé** (`git show <sha>`, `git diff <sha-avant>..<sha>`), jamais l'arbre de travail.
4. **Chaque prompt d'agent** contient : ID(s) de ticket (PH-NNN), ligne du board verbatim, pointeurs PRD / impl-spec, ligne `ROUTAGE`, périmètre de fichiers, contrat de sortie (`DONE: …` / `BLOCKED: …`), et « ne pas éditer les specs ; signaler et s'arrêter ». Rappel DoD backend : `pytest -v` vert · boot `uvicorn app.main:app` · `/api/health` 200 · migrations idempotentes · OpenAPI à jour.
5. **Échec** : compteur unique de `model-effort-routing` §4 — sous-agent **neuf** avec le rapport de revue (effort `high`), puis modèle +1, puis `BLOCKED`. Jamais la suite de la conversation qui a échoué.
6. **Le board est la seule mémoire** entre invocations : statut mis à jour après chaque retour.

## Exemple
Board prêt :
```
PH-001 todo db-migration-specialist    (migration M010)
PH-002 todo sync-specialist            (xtream_service : pagination)
PH-004 todo ai-recsys-specialist       (depends on PH-001)
```
Déroulé :
```
Agent(db-migration-specialist, "PH-001 …")                 # écrit, commit
[même message] Agent(code-reviewer, "relis le commit <sha> de PH-001")
               Agent(sync-specialist, "PH-002 …")          # écrit pendant que la revue lit un commit figé
…
Agent(ai-recsys-specialist, "PH-004 …")                  # après PH-001 DONE + APPROVED
```

## Anti-patterns
- ❌ Deux agents qui écrivent, lancés dans le même message.
- ❌ Un relecteur qui lit l'arbre de travail pendant qu'un dev écrit.
- ❌ Reprendre la conversation d'un agent qui a échoué au lieu d'un sous-agent neuf.
- ❌ Oublier de réécrire le résultat dans le board.
