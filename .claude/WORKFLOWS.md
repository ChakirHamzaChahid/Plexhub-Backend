# 🧭 Workflows multi-agents PlexHub Backend — ROUTEUR (auto-injecté au SessionStart)

> **Au début de CHAQUE session : identifie l'intention de l'utilisateur et applique le workflow correspondant via sa commande.** Ne pars jamais en solo sur une tâche multi-étapes — orchestre les agents. Autorité = `CLAUDE.md` (§2 modules, §3 conventions, §5 flux, §9 pièges). Backend **FastAPI / Python 3.13**, SQLite (async/WAL).
>
> 🌱 **MODÈLE DE BRANCHES (en vigueur)** : tout le développement courant se fait **directement sur `develop`** — **on ne crée PAS de branche par tâche** (`feature/*`/`fix/*`/`refactor/*` proscrites). `main` = **stable/release uniquement**, atteinte par merge de `develop` lors d'une release + tag `vX.Y.Z` (seule exception : `hotfix/<version>` depuis `main` pour un correctif urgent de prod). Validation d'un lot = `pytest -v` + boot `uvicorn app.main:app` + `GET /api/health` 200.

## Table de routage (intention → commande)
| L'utilisateur veut… | Workflow | Commande |
|---|---|---|
| Ajouter/implémenter une **fonctionnalité** (endpoint, service, worker) | Feature (orchestrateur→DAG→dev→QA→review) | **`/feature`** |
| **Refondre / extraire / migrer** du code | Refacto (Architecte→Migration→Validation→boucle) | **`/refacto`** |
| **Bug prod / 500 / régression / incident** | Incident (Monitor→Triage→Recherche→Correctif→Validation) | **`/incident`** |
| **Auditer** (qualité/sécu/perf, à blanc) | Audit clean-room | `/audit-cleanroom` |
| **Corriger les findings** d'audit | Remédiation (board complet) | `/fix-cleanroom` |
| **Mesurer la perf** (latence API) puis corriger | Bench + fixes | `/benchmark` → `/fix-bench-perf` |
| **Contexte/doc périmés** | Re-cartographie | `/refresh-context` (lourd) · `/sync-context` (léger) |
| **Publier une release** (merge develop→main, image Docker / tag) | Release (tests→merge→tag→image GHCR→vérif) | **`/release`** |
| Voir **l'état** du board | Snapshot | `/app-status` |

> Si l'intention est ambiguë, demande UNE clarification puis route. Si la tâche est triviale (1 fichier, question), réponds directement sans orchestration.

## Garde-fous communs (style « process » — ordre, traçabilité, sécurité)
- **Branche** : tout le travail se fait sur **`develop`** (commits directs, **jamais** de branche par tâche). Préconditions : `develop` propre/à jour, `CLAUDE.md` lu, env minimal (`.env`) pour les points runtime.
- **Parallélisme** : lance les sous-tâches **indépendantes** en parallèle (un agent par work-package, périmètres de fichiers disjoints) ; sérialise les dépendances (schéma DB, migrations, services partagés). Les agents parallèles commitent sur `develop` par **périmètres de fichiers disjoints** pour éviter les collisions.
- **DoD par lot** : `pytest -v` vert · serveur boote (`uvicorn app.main:app`) · `GET /api/health` 200 · **migrations idempotentes** (rejouables sans erreur) · `ruff check` propre (si câblé) · OpenAPI/contrat à jour si l'API change.
- **Gate review** : `code-reviewer` (+ `security-reviewer` si surface sensible : auth, secrets, entrée utilisateur, CORS) relit le diff **sur `develop`** (commits du lot) avant la suite ; **cap 2 cycles** de corrections puis `blocked` + remontée. Pas de promotion `develop`→`main` tant que la review n'est pas verte.
- **Risky = approbation humaine** : migration de schéma, refacto large, réécriture historique git, **release (merge develop→main)**, purge de données → `needs-approval`, jamais en auto.
- **Idempotence / retry** : une étape qui échoue se rejoue (max 5 essais) avant escalade ; pas d'effet de bord double.
- **Traçabilité** : board `docs/31-board.md`, rapport `docs/daily/<date>.md`, bugs `docs/51-bugs.md`.
- **Fraîcheur CLAUDE.md (anti-dérive, OBLIGATOIRE)** : tout commit qui touche modules (§2), schéma SQLite/migrations, flux (§5) ou conventions (§3) **met à jour le bandeau CLAUDE.md (date+HEAD) + la section concernée dans le même commit**, OU lance **`/sync-context`** avant de clôturer. Le détecteur SessionStart (`.claude/hooks/session-start.js`) signale la dérive.

## Les workflows (détail dans `.claude/commands/<nom>.md`)
- **`/feature`** — *Requirements (`cpo`) → Architecte+DAG (`cto`/`tech-lead`+`tech-manager`) → Dev (`backend-developer`, déléguer aux spécialistes domaine) → Test (`qa-engineer`) → Review (`code-reviewer`+`security-reviewer`)*. **Tout commité sur `develop`.**
- **`/refacto`** — *Architecte (`tech-lead` : cartographie + plan par étapes + contrats/ADR) → Migration fichier par fichier (`backend-developer`) → Validation régressions (`qa-engineer`+`perf-benchmarker`) → boucle (`tech-manager`)*. Gros moteur (services IA, plex_generator, schéma DB) = **vague isolée** + retest, mais **toujours en commits sur `develop`** (petits, verts, réversibles), pas de branche dédiée.
- **`/incident`** — *Monitor (`logs/plexhub.log`, `/metrics`, repro `curl`) → Triage (`tech-lead`, sévérité) → Recherche (cause racine `fichier:ligne`) → Correctif (`backend-developer`/spécialiste) → Validation (`qa-engineer` + smoke boot) → postmortem*. Correctif sur `develop` (ou `hotfix/<version>` depuis `main` **uniquement** si prod cassée à chaud).
- **Audit/Fix/Perf** = `/audit-cleanroom`, `/fix-cleanroom`, `/benchmark`→`/fix-bench-perf`.

## Agents disponibles (rappel)
Direction : `ceo`/`cpo`/`cto`/`tech-lead`/`tech-manager`. IC : `backend-developer`. Qualité : `qa-engineer`, `code-reviewer`, `security-reviewer`, `integration-agent`. Ops : `devops-engineer`, `release-manager`, `perf-benchmarker`, `observability-analyst`. Audit/contexte : `cleanroom-auditor`, `cleanroom-fixer`, `a0-cartographer`. Domaine : `db-migration-specialist`, `sync-specialist`, `ai-recsys-specialist`, `plex-generator-specialist`.
