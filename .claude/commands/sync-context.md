---
description: MAJ LÉGÈRE de la fraîcheur de CLAUDE.md — recale le bandeau (date+HEAD), ajoute un delta daté des commits depuis le dernier repère, et flag/MAJ les sections impactées. Alternative cheap à /refresh-context.
allowed-tools: Read, Write, Edit, Glob, Grep, Bash
---

> 🟢 **PlexHub Backend — FastAPI/Python 3.13.** Branche `main`. Objectif : que CLAUDE.md ne dérive plus. **Léger et fréquent** (≠ `/refresh-context` qui régénère intégralement via `a0-cartographer`). À lancer **après chaque lot de commits structurels** (ou quand le détecteur SessionStart signale la péremption). Validation = `pytest -v` + boot `uvicorn app.main:app` + `GET /api/health` 200.

# /sync-context — recaler la fraîcheur de CLAUDE.md

## Étapes
1. **Repères** : `git rev-parse --short HEAD` (= `NEW`) + date du jour. Lis le bandeau « À JOUR AU : … (HEAD `OLD`) » en tête de `CLAUDE.md`.
2. **Si `NEW == OLD`** : rien à faire, annonce « déjà à jour » et stop.
3. **Delta** : `git log --oneline OLD..NEW` + `git diff --name-only OLD..NEW`. Résume les changements de fond (pas le cosmétique).
4. **Flag des sections impactées** (heuristique chemin → section de CLAUDE.md) — pour CHAQUE zone modifiée, vérifie/MAJ la section correspondante (prouve par `fichier:ligne`, ne recopie pas l'ancien) :
   | Chemin modifié | Section CLAUDE.md à vérifier |
   |---|---|
   | `app/db/**`, `db/migrations.py`, entités `models/database.py` | §2 (modules) + §9 (migrations idempotentes, schéma SQLite courant) |
   | `app/services/**`, `app/workers/**` | §5 (flux : sync §5.1, enrichment §5.2, validation §5.3, génération Plex §5.4, recos IA §5.5) |
   | `app/api/**`, `api/deps.py` | §5 (flux endpoints) + conventions d'API §3 (Pydantic v2, `X-API-Key`, 503 IA, OpenAPI) |
   | `requirements.txt`, `requirements-dev.txt` | §10 (état réel / stack & versions) + §4 (build/run/test) |
   | `.claude/**`, `docs/31-board.md`, `docs/80-audit.md` | §7/§7bis (agents & skills), §11 (workflows) |
5. **Réécris le bandeau** en tête : `> 🕒 **À JOUR AU : <date> (HEAD `NEW`).**` + une ligne de delta concise (« depuis `OLD` : <résumé> »). **Supprime les avertissements obsolètes** une fois le contenu réellement reflété. Garde la règle de maintenance + le pointeur workflows.
6. **Honnêteté** : ce que tu n'as pas pu vérifier en profondeur, marque-le « à confirmer ». Si les changements sont **massifs/structurels** (refonte de modules, schéma SQLite sauté de plusieurs migrations), **recommande `/refresh-context`** (re-cartographie complète) au lieu de bricoler le bandeau.
7. **Commit** : `docs: sync-context @ HEAD <NEW>` (CLAUDE.md + ARCHITECTURE.md si touché).

## Limites (vs /refresh-context)
- `/sync-context` = recalage **incrémental** ciblé (bandeau + sections impactées par le delta). Rapide, à faire souvent.
- `/refresh-context` = **régénération complète** par `a0-cartographer` (lecture seule de tout le code). À faire après une grosse campagne ou quand la dérive est trop large.
