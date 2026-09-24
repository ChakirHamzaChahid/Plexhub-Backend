---
description: Workflow Feature — orchestrateur → requirements → architecture+DAG → dev (spécialistes domaine) → tests → review/audit, via les agents studio
argument-hint: <objectif de la feature, ex. "ajouter un endpoint /api/ai/similar">
allowed-tools: Read, Write, Edit, Glob, Grep, Bash, Task, Agent
---

> 🟢 **PlexHub Backend — FastAPI/Python 3.13.** Dév **directement sur `develop`** (pas de branche par tâche ; `main` = release only). Lis `.claude/WORKFLOWS.md` + `CLAUDE.md` §2/§3/§5/§9. Validation = `pytest -v` + boot `uvicorn app.main:app` + `GET /api/health` 200.

# /feature — livrer une fonctionnalité en multi-agents

Objectif : $ARGUMENTS

Tu es l'**orchestrateur**. Construis un **DAG de sous-tâches dépendantes** puis enchaîne les agents spécialisés (**un seul rédacteur à la fois** ; seules les relectures partent en parallèle). Ne code pas toi-même.

## 🚀 Chemin mineur (une seule session, sans PRD ni board)

Revue Gemini du 2026-09-23 : le cycle complet est disproportionné pour une petite modification. **Avant toute chose, l'orchestrateur teste l'éligibilité.**

**Éligible seulement si TOUT est vrai** (sinon : chemin complet ci-dessous) :
- grille `model-effort-routing` : **C1 ≤ 1** (un seul paquet), **C2 ≤ 1**, **C3 = 0** ;
- ni migration ou changement de schéma, ni nouvelle route `/api/*`, ni auth/secrets/SSRF, ni worker ou pipeline, ni écrivain d'identité (`match_locked`), ni nouvelle dépendance pip ;
- **un seul ticket**.

Exemples : message d'erreur, champ d'un template admin, ligne de log, clé de configuration non sensible.

**Déroulé :**
1. Annoncer `CHEMIN MINEUR` avec une ligne par critère d'éligibilité, puis l'objectif et 2-3 critères d'acceptation directement dans le message.
2. Un seul `backend-developer` (couple noté par la grille) ; `pytest` ciblé + `ruff check`.
3. Une revue `code-reviewer` du diff ; les déclencheurs de revues spécialisées s'appliquent quand même.
4. `pytest -v` complet une fois + boot `uvicorn app.main:app` + `GET /api/health` 200 si le code servi change ; commit sur `develop`.

**Bascule obligatoire :** si un critère tombe en cours de route, on **s'arrête** et on repart sur le chemin complet.

## Phases
1. **Requirements** — **la session principale** (skill `product-management:write-spec`) transforme l'objectif en **user stories + critères d'acceptation** + non-goals + métriques + impact contrat d'API. Sortie : `docs/10-prd-<feature>.md`. Pose UNE question si l'intention produit est ambiguë. *(Mesuré le 2026-09-24 sur les transcripts Claude Code depuis le 2026-09-12 : **0 invocation** de `cpo`, `cto` et `tech-manager` — la planification se fait déjà dans la session principale. On l'assume : phases 1 et 2 dans **un seul contexte**, sans relais PRD → archi → board entre sous-agents, qui perdrait du contexte à chaque passage. `cpo` n'est lancé que si Chakir le demande.)*
2. **Architecte + DAG** — **la session principale** (skill `engineering:system-design`) conçoit l'archi (logique métier dans `services/`/`workers/`, routers `api/` = validation+délégation §2 ; schémas Pydantic v2 aux frontières §3 ; impact schéma SQLite/migrations → **propriétaire `db-migration-specialist`** ; pièges §9) + **contrats d'API** (OpenAPI). Puis elle découpe (skill `sprint-planner`) en **sous-issues dépendantes** sur `docs/31-board.md` (colonnes Status/Depends on/Owner/Safe-Risky), arêtes de dépendance explicites. **GATE** : présente le découpage + l'ordre d'exécution (séquentiel) ; attends le « go ». **Relecture en contexte neuf** (lecture seule) : `cto` ou `tech-lead` challenge l'archi **uniquement** si la grille donne C2 = 2 ou C3 = 2 ; il ne signale que les écarts de correction.
3. **Dev séquentiel** (rédacteur unique : une Task à la fois, dans l'ordre du DAG) — `backend-developer` mène ; **déléguer au spécialiste domaine selon la zone** :
   - `db-migration-specialist` — schéma SQLite / migrations (`db/migrations.py`, **idempotentes en fin de chaîne** §9).
   - `sync-specialist` — Xtream / sync / enrichment (`services/xtream_service`, `workers/sync_worker`, `enrichment_worker`).
   - `ai-recsys-specialist` — embeddings / ranking / sqlite-vec (`services/embedding_service`, `recommendation_service`, `api/ai.py`).
   - `plex-generator-specialist` — génération NFO/arbo (`plex_generator/**`).
   - `backend-developer` — routers/endpoints `api/`, services génériques, utils, si pas de spécialiste dédié.
4. **Test** — `qa-engineer` : tests `pytest` (pytest-asyncio mode auto, `respx` pour httpx) couvrant les critères d'acceptation ; exécute la validation.
5. **Review / Audit** — `code-reviewer` (qualité/conventions §3/§9) **toujours** ; `security-reviewer` et `perf-benchmarker` **uniquement si le diff coche un déclencheur** (§ « Déclencheurs des revues spécialisées » ci-dessous), sinon le rapport écrit « non requis — aucun déclencheur ». Merge par `tech-manager`.

## 🎯 Déclencheurs des revues spécialisées (décidés sur le diff, pas par habitude)

Revue Gemini du 2026-09-23 : `security-reviewer` et `perf-benchmarker` ne partent **que** si le diff du lot coche un déclencheur ci-dessous. Le couple modèle × effort de chaque revue est ensuite noté par la grille `model-effort-routing`.

| Revue | Lancée | Déclencheurs (au moins un fichier du diff) |
|---|---|---|
| `code-reviewer` | **toujours** | — |
| `security-reviewer` | **seulement si déclencheur**, dans le **même message** que `code-reviewer` | `api/deps.py`, `X-API-Key`, Basic Auth, `api/route_audit.py` · nouvelle route `/api/*` ou `/admin*` en POST (CSRF) · secrets, Fernet, `payload_crypto` · client httpx sortant, `utils/ssrf.py` · `/dav` · downloads (confinement `DOWNLOAD_DIR`) · `tv_auth` · CORS |
| `perf-benchmarker` | **seulement si déclencheur** | SQL ou index sur `media`, migration · `media_service`, `aggregation_service`, `unified_group_service` · workers (sync, enrichment, health-check, pipeline) · `plex_generator` · endpoints liste `/api/media/*` · appel bloquant sur la boucle d'événements |

Quand une revue spécialisée n'est pas lancée, le rapport de lot l'écrit : `security-reviewer : non requis — aucun déclencheur` (même chose pour `perf-benchmarker`).

## DoD (chaque sous-issue)
`pytest -v` vert · serveur boote (`uvicorn app.main:app`) · `GET /api/health` 200 · **migrations idempotentes** (rejouables sans erreur) · `ruff check` propre (si câblé) · **OpenAPI/contrat à jour** si l'API change. Boucle de correction max 5 essais ; cap 2 cycles review puis `blocked` (= tentatives 2 et 3 du compteur unique, sous-agent neuf à chaque fois).

## Sûreté
Risky (migration de schéma, refacto large, secrets, release) = `needs-approval`. Éditions de contrats **additives** quand possible. Jamais d'auto-merge par-dessus un `REQUEST CHANGES`. `BLOCKED` remonté verbatim. Secrets jamais en clair (tokens, clés API, Fernet).

> Raccourci : ce workflow s'appuie sur les briques `/app-plan` (DAG, phase 2), `/app-build` (exécution séquentielle + review streaming + QA, phases 3-5) et `/app-review` (review d'un lot isolé). `/feature` ajoute la phase Requirements en amont et la DoD backend. Routage (modèle, effort) des invocations = skill `model-effort-routing`.
