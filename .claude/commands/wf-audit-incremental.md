---
description: Audit INCRÉMENTAL du diff (lecture seule) — jauge la santé de ce qui a changé depuis un repère (dernière release, dernier merge, ou <ref> passée en argument). Rapide, ciblé, ne re-fait PAS un audit 360°.
argument-hint: [ref git, ex. "v1.4.1" ou "main" ou "<sha>" — défaut = dernière release taguée, sinon HEAD~50]
allowed-tools: Read, Write, Edit, Glob, Grep, Bash, Task, Agent
---

> 🟢 **PlexHub Backend — FastAPI/Python 3.13.** `develop`. Lis `.claude/WORKFLOWS.md` (garde-fous + **routage modèle×effort** `model-effort-routing` — applique le couple (modèle, effort) adéquat à chaque agent) + `CLAUDE.md` §9 (pièges) / §10 (dette `CR-*`). Complément léger et fréquent à `/audit-full` (360° complet).

# /wf-audit-incremental — auditer uniquement ce qui a bougé

Objectif : produire un rapport **court, ciblé, actionnable** sur le **diff** entre un repère (dernière release, dernier gate vert, ou `$ARGUMENTS`) et `HEAD`, sans refaire l'audit complet du repo. Complète `/audit-full` (mensuel/trimestriel) — c'est l'audit **hebdomadaire** ou **post-lot**.

## Repère par défaut
1. Cherche la dernière release taguée `v*` : `git describe --tags --abbrev=0 --match 'v*' 2>/dev/null`.
2. Sinon prend `HEAD~50` (bornage de sécurité).
3. Un argument `$ARGUMENTS` (ref/tag/sha/branche) l'emporte sur la valeur par défaut.

Publie la ref choisie en tête du rapport (« Audit incrémental : `<REF>..HEAD` — N commits, M fichiers »).

## Étapes (Manager)
1. **Repère + delta**
   - `git log --oneline <REF>..HEAD` (commits inclus)
   - `git diff --stat <REF>..HEAD` (volumétrie)
   - `git diff --name-only <REF>..HEAD` (fichiers touchés)
   - Si le delta est **vide** → écris « rien à auditer depuis `<REF>` » et stop.
   - Si le delta est **massif** (>200 fichiers OU >10 000 lignes changées) → recommande **`/audit-full`** au lieu de continuer.

2. **Classification par zone d'impact** (heuristique chemin → dimension, calquée sur `/sync-context`)
   | Chemin | Dimension à auditer |
   |---|---|
   | `app/db/**`, `app/models/database.py` | Migrations **idempotentes** en fin de chaîne (§9.6), schéma ORM ⇆ migrations alignés (CR-C05), index (CR-P02), `EncryptedString`/Fernet |
   | `app/workers/sync_worker.py`, `app/workers/enrichment_worker.py`, `app/services/{xtream,tmdb,category,scrape_cache}_service.py` | Sync/enrichment (§5.1/§5.2) : idempotence par `dto_hash`, éviction par slot (CR-F02), budget TMDB (CR-F03), differential cleanup épisodes (CR-F01) |
   | `app/api/**`, `app/main.py` (montage routers), `app/api/deps.py` | **Auth fail-closed** sur tout `/api/*` (X-API-Key, 401, temps constant), conventions camelCase/`response_model`, 3 conventions de montage (CR-A04), pas de logique métier dans les routers |
   | `app/api/ai.py`, `app/services/{embedding,recommendation,subtitle,ollama}_service.py`, `app/workers/embedding_worker.py` | Motifs 503 contractuels (§9.2/§9.12), cold-start, rebuild jamais au boot, cap 20 hydrate, sqlite-vec/M008 |
   | `app/plex_generator/**`, `app/services/nfo_import_service.py` | Génération .strm/NFO : idempotence created/updated/deleted, écritures bloquantes sur la boucle (CR-C01), prune folder-aware |
   | `app/services/download*`, `app/services/plex_*`, `app/workers/download_worker.py`, `app/api/*download*` | **Secrets** (URL Xtream / token Plex jamais persistés/loggés/renvoyés), confinement F-007 `resolve_confined`, `follow_redirects` anti-SSRF, worker master-only |
   | `requirements*.txt`, `pyproject.toml`, `Dockerfile`, `docker-compose.yml`, `.github/**` | **Bornes liées** fastapi<0.116 ⇆ instrumentator<8 (§10), CI pytest+cov+ruff, Python 3.12 image vs 3.13 CI |
   | `app/templates/**`, `app/api/admin*` | UI admin HTMX : Basic Auth au mount, pièges triggers htmx (§5.10), CSRF (dette CR-S07) |
   | `.claude/**`, `docs/**`, `CLAUDE.md` | Cohérence agents/commands/workflows, fraîcheur bandeau |

3. **Délègue à `full-auditor`** en **mode incrémental** (skills `production-code-audit` + `security-audit` + `application-performance-performance-optimization`) avec ce mandat :
   - « Audite UNIQUEMENT le diff `<REF>..HEAD`. Ne re-vérifie pas les zones non touchées. Pour chaque zone touchée : (a) régression vs invariants §9 ? (b) trous sécurité (auth/secrets/SSRF/confinement) ? (c) régression perf plausible (chemin chaud : listes unified, recherche, génération, sync) ? (d) dette technique/doc à jour ? »
   - Sortie = `docs/audit/incremental/<date>-<REF>-to-HEAD.md`
   - Format : findings `INC-<DATE>-<N>` (id incrémental court), sévérité S1/S2/S3, cause `fichier:ligne`, correctif suggéré.
   - **Skip explicite** de toute zone non touchée (économie de contexte).

4. **Cross-check avec CLAUDE.md**
   - Le bandeau `À JOUR AU : … HEAD …` couvre-t-il `<REF>..HEAD` ? Sinon flag « CLAUDE.md périmé sur ce lot → lancer `/sync-context` ».
   - Chaque changement structurel (schéma/migrations / §5 flux / §9 pièges nouveaux) a-t-il été reflété dans CLAUDE.md ? Sinon liste les sections à MAJ.

5. **Présente 3 blocs (dans l'ordre)** :
   - **Scorecard delta** — 1 note globale (A/B/C/D) + une note par dimension impactée
   - **Top findings S1/S2** (max 10) avec `fichier:ligne` + effort estimé
   - **Actions recommandées** — `/incident` pour un S1 avéré · `/sync-context` si CLAUDE.md à recaler · `/audit-full` si delta > seuil · continuer sans action si tout est vert

## Anti-patterns
- ❌ Refaire un audit 360° du repo — utiliser `/audit-full`
- ❌ Modifier du code — cette commande est **lecture seule**
- ❌ Bloquer si `<REF>` invalide — dégrader vers `HEAD~50` et signaler
- ❌ Ignorer les zones sans finding — les mentionner « clean »

## Cadence recommandée
- **Après chaque lot `/feature` ou `/refacto`** (5-15 commits) — repère = début du lot
- **Avant chaque `/release`** — repère = dernière release taguée (`v1.4.1`, etc.)
- **Hebdomadaire** sur `develop` — repère = commit d'il y a 7 jours (`git rev-list -1 --before='7 days ago' HEAD`)

> Sortie type : `docs/audit/incremental/<date>-<REF>-to-HEAD.md` — 1 à 2 pages, actionnable en <30 min.
