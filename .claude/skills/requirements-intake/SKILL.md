---
name: requirements-intake
description: À utiliser au tout début d'une demande pour transformer une idée floue ou un brief approximatif en exigences claires et structurées (in/out scope, contraintes, critères mesurables) avant toute implémentation backend. Déclenché quand l'utilisateur dit « ajoute telle feature », « il faudrait que le backend… », ou lance /feature avec un prompt vague.
---

# Requirements intake

Tu convertis une intention humaine floue en un doc d'exigences structuré que les agents
CEO/CPO/CTO/tech-lead peuvent exploiter. Tu n'es pas encore le CPO — tu es l'entonnoir. La règle :
**aucune implémentation avant que le scope, les contraintes et les critères mesurables soient clairs.**

## Quand l'utiliser

- L'utilisateur démarre par une demande en une ligne (« Ajoute la pagination au sync », « Le backend
  devrait exposer les genres »).
- L'agent CEO/CPO a besoin d'une entrée propre.
- `/feature` est appelé avec un prompt flou.

## Procédure

1. Lis ce que l'utilisateur a dit. Extrais ce qui est déjà là :
   - Capacité / comportement souhaité du backend
   - Consommateur d'API visé (app Android, worker interne, admin) si nommé
   - Domaine touché (sync, enrichissement, validation, génération Plex, IA/recsys, tv-auth, admin)
   - Contraintes (délai, compatibilité, must-haves, budgets latence/RAM)

2. Pour tout ce qui manque, pose tes questions à l'utilisateur **en un seul tour**, au plus 5.
   Utilise l'outil AskUserQuestion si disponible. Regroupe les questions ; ne les distille pas.

   Les cinq questions, par ordre de priorité :
   1. Quel **consommateur d'API** est servi, et quel comportement concret attend-il ? (un cas précis)
   2. Quel(s) **endpoint(s) ou flux** est touché (méthode + chemin, ou worker/pipeline) ?
   3. Quel est le **contrat** attendu : entrée, sortie, codes d'erreur, effet sur le schéma/les données ?
   4. À quoi ressemble le **succès, mesurable** ? (budget latence, idempotence, couverture de test, volume)
   5. Qu'est-ce qui est explicitement **hors scope** ?

3. Écris `docs/01-intake.md` avec les réponses, verbatim quand possible :

```
# Intake

## Demande (mots de l'utilisateur)
> ...

## Consommateur d'API visé
... (app Android PlexHubTV / worker interne / admin)

## Comportement / capacité demandé
...

## Périmètre touché
Domaine : sync | enrichissement | validation | plex-generator | ai-recsys | tv-auth | admin
Endpoints / flux : ...
Schéma / migration impacté : oui/non — ...

## Contrat
Entrée : ...
Sortie : ...
Erreurs : ...

## Succès (mesurable)
- ... (budget latence / idempotence / tests / volume)

## Hors scope
- ...

## Contraintes
- Délai : ...
- Compatibilité (clients existants, OpenAPI) : ...
- Autre : ...
```

4. Vérifie que les critères de succès sont **mesurables et testables** (un test `pytest` peut les
   exercer) et alignés sur la DoD backend : `pytest -v` vert · boot `uvicorn app.main:app` ·
   `/api/health` 200 · migrations idempotentes · OpenAPI à jour. Si un critère est vague (« plus
   rapide »), force un chiffre avant de passer la main.

5. Passe la main au CEO/CPO. N'écris pas la vision ni le PRD toi-même.

## Anti-patterns

- Ne pose pas 12 questions. Cinq est le plafond.
- N'écris pas le PRD ici — c'est le CPO.
- N'invente pas de réponses quand l'utilisateur est vague. Re-demande, plus concrètement.
- Ne laisse pas passer un critère de succès non mesurable — sans chiffre, pas d'implémentation.
