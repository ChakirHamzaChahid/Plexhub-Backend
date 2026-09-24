---
description: Workflow Incident — Monitor → Triage (sévérité S1..S4) → Recherche cause racine → Correctif → Validation → Postmortem, orienté run/exploitation avec garde-fous
argument-hint: <symptôme, ex. "500 sur /api/ai/rank" / "sync KO sur compte Xtream X">
allowed-tools: Read, Write, Edit, Glob, Grep, Bash, Task, Agent
---

> 🟢 **PlexHub Backend — FastAPI/Python 3.13.** Dév **directement sur `develop`** (correctif urgent prod = `hotfix/<version>` depuis `main`). Lis `.claude/WORKFLOWS.md` + `CLAUDE.md` §9. Validation = `pytest -v` + boot `uvicorn app.main:app` + `GET /api/health` 200.

# /incident — triage & remédiation en multi-agents

Symptôme : $ARGUMENTS

Workflow **orienté process** (ordre, traçabilité, garde-fous priment sur la créativité).

## Phases
1. **Monitor** — capture les faits : `logs/plexhub.log` (filtre par **`request_id`**, niveaux ERROR/exceptions), métriques `/metrics` (Prometheus), **repro `curl`** du parcours fautif (codes HTTP, latence `curl -w`). **Aucune hypothèse non vérifiée.**
2. **Triage** — `tech-lead` : **sévérité** (S1 crash boot / corruption DB / perte de données → S4 mineur), périmètre, préconditions, impact. Décide correctif immédiat vs investigation. Distingue les **503 IA contractuels** (§9 : `AI_API_KEY` vide, sqlite-vec non chargé, fastembed KO) d'un vrai bug.
2bis. **Confinement (S1, ou S2 qui se propage) — AVANT la recherche de cause** (revue 2026-09-24). On arrête les dégâts d'abord, on comprend ensuite. Uniquement des actions **réversibles**, chacune **approuvée par Chakir** et consignée :
   - **Sauvegarde d'abord** : copie de la base SQLite (fichier + `-wal`/`-shm`, service arrêté ou via `sqlite3 .backup`) avant toute autre action.
   - **Régression introduite par une release** → redéployer la version précédente — image `ghcr.io/…:<X.Y.Z précédente>` (tag d'image **sans** `v`) ou `git checkout v<prev>` + `docker compose up -d --build`, selon le mode de déploiement. Si `v<bad>` a appliqué une migration non rétro-compatible, restaurer la sauvegarde prise **avant** le déploiement de `v<bad>` (voir `/release` § Rollback) — jamais de migration « à l'envers » improvisée.
   - **Un worker ou un traitement corrompt des données** → l'arrêter (désactivation par configuration/env si elle existe, sinon arrêt du conteneur) plutôt que de laisser tourner pendant l'enquête.
   - **Rien à confiner** (bug non déployé) → le noter et passer à la phase 3.
   Jamais de purge, de `DELETE` ni de réécriture de tag au titre du confinement.
3. **Recherche (cause racine)** — skill **`engineering:systematic-debugging`** (+ `engineering:debug`) : reproduire → isoler → diagnostiquer, prouvé **`fichier:ligne`** (log + code). **Pas de patch avant cause racine confirmée.**
4. **Correctif** — agent **spécialisé selon la zone** : `db-migration-specialist` (schéma/migrations/`db_retry`), `sync-specialist` (Xtream/sync/enrichment), `ai-recsys-specialist` (embeddings/ranking/sqlite-vec), `plex-generator-specialist` (NFO/arbo), sinon `backend-developer` (routers/services/utils). Patch minimal ciblant la cause.
5. **Validation** — `qa-engineer` : test de non-régression couvrant le cas + **smoke boot OBLIGATOIRE** (`uvicorn app.main:app` démarre, `GET /api/health` 200). **Rejouer les migrations** si schéma touché (idempotence §9). Re-mesure si perf (latence endpoint, `/metrics`).
6. **Postmortem** — skill `engineering:incident-response` : entrée `docs/51-bugs.md` (+ `docs/daily/<date>.md`), cause racine `fichier:ligne` + correctif + prévention (test de garde ajouté).

## Garde-fous (style « process »)
- **Préconditions/post-conditions** explicites par étape ; **seuils** numériques (ex. « latence /api/ai/rank chaud < 500 ms », « 0 exception au boot »).
- **Retry/idempotence** : rejouer une étape sans effet de bord double ; migrations rejouables.
- **Validation humaine** pour toute action risquée (migration destructive, release, purge de données, changement de signature) → `needs-approval`.
- **Traçabilité** : chaque décision et mesure consignée. `BLOCKED` remonté verbatim, on n'invente pas de réponse.
- Sécurité : ne jamais exposer un secret en clair dans les logs (tokens Plex, clés API, Fernet) — `request_id` oui, secrets non (§3/§9.10).
