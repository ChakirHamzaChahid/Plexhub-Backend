---
description: Review de code d'un ticket ou d'un intervalle de commits sur develop
argument-hint: <ID de ticket (APP-NNN / CR-*) ou intervalle git (ex. HEAD~5..HEAD)>
allowed-tools: Read, Write, Edit, Glob, Grep, Bash, Task, Agent
---

> 🟢 **PlexHub Backend — FastAPI/Python 3.13.** Pas de branche par tâche : la review porte sur un **lot de commits `develop`**, pas sur une branche.

# /app-review — Review d'un lot

Cible : $ARGUMENTS

## Étapes

1. Résous `$ARGUMENTS` en un **intervalle de commits** :
   - ID de ticket (`APP-NNN`, `CR-*`, `BUG-NNN`) → retrouve les commits correspondants via `docs/31-board.md` (colonne Notes/commits) et/ou `git log --oneline --grep "<ID>"` sur `develop`.
   - Intervalle git (`<ref>..HEAD`, sha) → utilise-le tel quel.
   - Rien de résoluble → demande UNE clarification.

2. Spawn l'agent `code-reviewer` avec l'intervalle et l'ID de ticket (contexte : impl-spec + critères d'acceptation du board). Ajoute `security-reviewer` en parallèle si le diff touche une surface sensible (auth/`deps.py`, secrets/Fernet, CORS, écriture disque/download, entrée utilisateur).

3. Imprime le verdict **verbatim**. Si `REQUEST CHANGES`, imprime aussi la suite suggérée (re-spawn du dev avec les notes, ou demander à l'utilisateur comment procéder). Rappelle le cap 2 cycles.
