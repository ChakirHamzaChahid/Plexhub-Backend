---
name: parallel-orchestrator
description: À utiliser pour lancer effectivement plusieurs agents en parallèle via l'outil de sous-agents (Task/Agent), à partir d'un plan de sprint et d'un board. Déclenché depuis /feature ou quand le tech-manager dit « lance le pod ». Encapsule les règles d'exécution concurrente sûre des agents sur le backend.
---

# Parallel orchestrator

Tu lances des agents IC en parallèle. Cette skill existe parce que les lancements parallèles ont
besoin de règles — sans elles, les agents se marchent dessus (conflits de fichiers, schéma DB
concurrent, services partagés écrasés).

## Quand l'utiliser

Appelée depuis `/feature` ou par le tech-manager une fois que `docs/31-board.md` a des tickets en
`todo` prêts à démarrer.

## Procédure

1. **Lis le board.** Trouve les tickets où `Status = todo` et où tous les `Depends on` sont `done`.

2. **Découpe en work-packages à périmètres de fichiers DISJOINTS.** C'est la règle d'or backend :
   deux agents en parallèle ne doivent pas toucher les mêmes fichiers/modules. Carte mentale des
   propriétaires de modules (cf. `CLAUDE.md` §7) :
   - `db-migration-specialist` → `app/db/migrations.py`, `app/models/database.py` (schéma SQLite)
   - `sync-specialist` → `app/services/xtream_service.py`, `tmdb_service.py`, `app/workers/sync_worker.py`, `enrichment_worker.py`
   - `ai-recsys-specialist` → `app/services/embedding_service.py`, `recommendation_service.py`, `app/api/ai.py`, `app/workers/embedding_worker.py`
   - `plex-generator-specialist` → `app/plex_generator/**`
   - `backend-developer` → reste de `app/api/`, `app/services/`, `app/utils/`

3. **Groupe par owner.** Une invocation d'agent par owner, en batch. Un même owner reçoit tous ses
   tickets prêts dans un seul prompt.

4. **Sérialise les dépendances dures.** Le **schéma DB** (migration M0NN) et les **services
   partagés** (ex. `utils/db_retry`, `config.py`, une signature de service consommée par plusieurs
   workers) sont des points de sérialisation : le ticket qui modifie le contrat passe **avant**, les
   consommateurs reprennent son commit. Ne lance jamais en parallèle deux tickets qui touchent la
   même migration ou le même module partagé.

5. **Lance dans un seul message.** Utilise l'outil de sous-agents (`Task`/`Agent`) avec plusieurs
   invocations dans le même message assistant pour qu'ils tournent en concurrence. C'est l'étape
   critique — des lancements séquentiels abandonnent le parallélisme qu'on vient de gagner.

6. **Chaque prompt d'agent** inclut :
   - Le(s) ID(s) de ticket
   - La ligne du board verbatim
   - Les pointeurs vers la section PRD, `docs/22-impl-spec-backend.md`, l'archi
   - Le périmètre de fichiers autorisé (les modules de cet owner — interdiction d'en sortir)
   - Le contrat de sortie attendu (`DONE: PH-NNN ...` ou `BLOCKED: PH-NNN ...`)
   - Rappel DoD backend : `pytest -v` vert · boot `uvicorn app.main:app` · `/api/health` 200 ·
     migrations idempotentes · OpenAPI à jour
   - Rappel : ne pas éditer les specs ; signaler les blocages et s'arrêter

7. **GATE + boucle de correction.** Streame les reviews — n'attends pas tout le batch. Dès qu'un
   développeur renvoie `DONE: PH-NNN`, passe la ligne du board à `Status = review` et spawn un
   `code-reviewer` (et `security-reviewer` si le ticket touche auth/secrets/crypto) pour cette
   branche au message suivant. Les reviewers tournent en parallèle entre eux **et** avec les devs
   encore en cours.

   **Gate review (sur `develop`)** : un lot n'est validé qu'après `APPROVED` du code-reviewer sur ses commits **sur `develop`** (pas de branche par tâche) ; le tech-manager ne promeut `develop`→`main` qu'à la release → le board
   passe `review → qa`. `REQUEST CHANGES` re-spawn le développeur d'origine avec les notes du
   reviewer. Cap de cycles de review (2 cycles) appliqué par l'orchestrateur — au-delà, on remonte à
   l'utilisateur. La GATE finale exige la DoD backend complète (tests verts, boot OK, /api/health
   200, migrations idempotentes, OpenAPI à jour) avant `qa → done`.

## Anti-patterns

- **Lancements séquentiels quand le parallèle est sûr.** Si PH-001 et PH-002 ne se chevauchent pas
  (périmètres de fichiers disjoints), ne les lance jamais dos-à-dos dans deux messages.
- **Lancements parallèles quand le série est requis.** Si deux tickets touchent la même migration ou
  le même service partagé, sérialise — le second reprend le commit du premier.
- **Oublier de réécrire le résultat dans le board.** Le board est la seule mémoire entre invocations.
- **Laisser un agent sortir de son périmètre de fichiers** (ex. un sync-specialist qui édite une
  migration) — c'est la source n°1 de conflits de merge.

## Exemple travaillé

État du board (prêt) :
```
PH-001 todo db-migration-specialist    (migration M010 : colonne embedding_model)
PH-002 todo sync-specialist            (xtream_service : pagination)
PH-003 todo plex-generator-specialist  (nfo_builder : champ studio)
PH-004 todo ai-recsys-specialist       (depends on PH-001)
```

Lancement correct :
```
[même message]
Agent(db-migration-specialist, "Travaille PH-001, périmètre app/db/migrations.py + models/database.py ...")
Agent(sync-specialist,         "Travaille PH-002, périmètre app/services/xtream_service.py ...")
Agent(plex-generator-specialist,"Travaille PH-003, périmètre app/plex_generator/nfo_builder.py ...")
```

PH-004 n'est PAS lancé tant que PH-001 (la migration dont il dépend) n'a pas renvoyé DONE.

Après les trois DONE :
```
[même message]
Agent(code-reviewer, "Relis les commits de PH-001 sur develop")
Agent(code-reviewer, "Relis les commits de PH-002 sur develop")
Agent(code-reviewer, "Relis les commits de PH-003 sur develop")
```

Puis PH-004 part avec ai-recsys-specialist, en parallèle de ce qui est désormais prêt.
