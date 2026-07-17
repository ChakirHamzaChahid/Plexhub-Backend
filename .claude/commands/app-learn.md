---
description: Extrait les conventions d'un service livré vers le House Knowledge Base vivant — ajoute les apprentissages nets aux packs knowledge/*.md et remonte les conflits pour décision humaine
argument-hint: <chemin d'un service livré> [autres chemins...]   (défaut = le projet courant)
allowed-tools: Read, Write, Edit, Glob, Grep, Bash, Task, Agent
---

> 🟢 **PlexHub Backend — FastAPI/Python 3.13.** House KB = `.claude/knowledge/` (`python-conventions`, `api-conventions`, `stack-defaults`, `git-workflow`, `observability`).

# /app-learn — Faire grandir le House Knowledge Base

Chemins à miner (défaut = le projet courant / tout juste livré) : $ARGUMENTS

Le House KB (`.claude/knowledge/`) est **vivant**. Après avoir livré un service — ou pour ingérer un existant — cette commande replie ses conventions réelles dans les packs pour que les prochains projets démarrent plus malins.

## Étapes

1. **Résoudre les cibles.** Pour chaque chemin, confirme que c'est un vrai service (présence de `CLAUDE.md`, `README.md`, doc d'architecture, et/ou fichiers de deps `requirements.txt`/`pyproject.toml`). Si rien n'est donné, utilise le projet courant.

2. **Miner en parallèle.** Spawn un Agent `general-purpose` par service (un seul message) pour extraire, en lecture seule, un rapport structuré : stack & versions, architecture (layering routers/services/workers), persistance & migrations, tâches de fond/scheduler, auth & gestion de secrets, gestion d'erreurs & conventions HTTP, tests (fixtures, couverture), observabilité (métriques/logs), conventions git/commit, et les règles maison explicites « toujours/jamais » (ex. §9 pièges d'un CLAUDE.md).

3. **Diff contre le KB.** Pour chaque pack sous `.claude/knowledge/`, calcule :
   - **Nouveautés** — conventions pas encore capturées → propose de les ajouter.
   - **Confirmations** — règles existantes confirmées → note la confiance accrue, aucun changement.
   - **Conflits** — le service contredit un pack (ex. un autre pattern de retry DB, une autre convention de réponse). **Ne jamais écraser silencieusement.** Consigne les deux positions et remonte le conflit.

4. **Appliquer les ajouts, signaler les conflits.** Écris ajouts/confirmations dans les packs concernés. Pour chaque conflit, imprime :
   ```
   CONFLIT dans .claude/knowledge/<pack>.md
   Le KB dit :      <règle actuelle>
   <service> fait : <règle observée>
   Décision requise : laquelle devient le défaut maison ?
   ```
   Attends l'arbitrage de l'utilisateur sur chacun avant de changer une règle conflictuelle.

5. **Résumer.** Imprime un résumé de diff : packs touchés, conventions ajoutées, conflits en attente, et une note d'une ligne par service source. Ajoute une entrée datée à `CHANGELOG.md` (section KB) si le fichier existe.

## Règles

- Lecture seule sur les services sources — `/app-learn` n'édite jamais un service qu'il mine.
- Les ajouts sont auto-applicables ; les conflits exigent toujours une décision humaine.
- Garde les packs concrets et exemplifiés (extraits de code, `fichier:ligne`), pas de généralités.
