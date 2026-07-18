# 🧭 Workflows multi-agents PlexHub Backend — ROUTEUR (auto-injecté au SessionStart)

> **Au début de CHAQUE session : identifie l'intention de l'utilisateur et applique le workflow correspondant via sa commande.** Ne pars jamais en solo sur une tâche multi-étapes — orchestre les agents. Autorité = `CLAUDE.md` (§2 modules, §3 conventions, §5 flux, §9 pièges). Backend **FastAPI / Python 3.13**, SQLite (async/WAL).
>
> 🌱 **MODÈLE DE BRANCHES (en vigueur)** : tout le développement courant se fait **directement sur `develop`** — **on ne crée PAS de branche par tâche** (`feature/*`/`fix/*`/`refactor/*` proscrites). `main` = **stable/release uniquement**, atteinte par merge de `develop` lors d'une release + tag `vX.Y.Z` (seule exception : `hotfix/<version>` depuis `main` pour un correctif urgent de prod). Validation d'un lot = `pytest -v` + boot `uvicorn app.main:app` + `GET /api/health` 200.

## Les 7 workflows canoniques

| # | Workflow | Commande | Rôle |
|---|---|---|---|
| 1 | **Audit complet** (360°) | **`/audit-full`** | Diagnostic exhaustif lecture-seule (`full-auditor`), sortie `docs/audit/v*/`. Mensuel/trimestriel. |
| 2 | **Audit incrémental** (diff) | **`/wf-audit-incremental`** | Audit rapide du diff `<REF>..HEAD` (dernière release/lot). Post-lot ou hebdo. |
| 3 | **Benchmark** latence API | **`/benchmark`** | Mesures chiffrées par scénario (serveur booté, `/metrics`, logs `request_id`) + goulots. Puis `/fix-bench-perf` pour corriger. |
| 4 | **Sync context** (doc) | **`/sync-context`** | Recale le bandeau CLAUDE.md + sections impactées après un lot de commits structurels. Léger. |
| 5 | **Feature** (nouvelle fonctionnalité) | **`/feature`** | Requirements → Architecture + `/app-plan` → `/app-build` (dev // + review + QA) → gate `integration-agent`. |
| 6 | **Refacto** (refonte ciblée) | **`/refacto`** | Architecte → Migration par étapes (indépendantes via `/app-build`) → Validation régressions → boucle (`/app-review`) → gate `integration-agent`. Risky par défaut. |
| 7 | **Incident** (bug/500/régression) | **`/incident`** | Monitor → Triage → Recherche cause racine → Correctif → Validation smoke boot + postmortem. |

**Commandes annexes** (utiles mais hors des 7) : `/audit-cleanroom` + `/fix-cleanroom` (audit table-rase `CR-*` → remédiation board), `/app-audit` (note vs House KB — sur ce repo, réutilise l'audit clean-room existant), `/fix-bench-perf` (suite de `/benchmark`), `/refresh-context` (régénération complète via `a0-cartographer`, plus lourd que `/sync-context`), `/release` (pipeline merge develop→main + tag + image GHCR) et son gate `/app-ship`, `/app-plan` (DAG board), `/app-build` (exécution sprint), `/app-review` (review d'un lot), `/app-status` (snapshot board), `/app-team` (roster), `/app-onboard`/`/app-init` (adoption/bootstrap d'un service), `/app-learn` (replier les conventions dans le House KB), `/app-run` (driver quasi-autonome bout-en-bout).

## Auto-activation contextuelle (routing par mots-clés)

Détecte l'intention dans le prompt de l'utilisateur et propose le workflow **avant** de coder. Si le match est ambigu → 1 clarification puis route ; si la tâche est triviale (1 fichier, question) → réponds directement.

| Mots-clés / patterns dans le prompt utilisateur | Workflow suggéré |
|---|---|
| "audit complet", "revue globale", "audit 360", "état du code" | `/audit-full` |
| "audit diff", "audit incrémental", "check ce qui a bougé", "audit du lot", "audit avant release" | `/wf-audit-incremental` |
| "benchmark", "profile", "perf", "latence", "endpoint lent", "mesure API" | `/benchmark` |
| "sync context", "recale CLAUDE.md", "doc périmée", "bandeau à jour" | `/sync-context` |
| "refresh context", "re-cartographie complète", "régénère l'archi" | `/refresh-context` |
| "feature", "implémente", "ajoute", "nouvelle fonctionnalité", "nouvel endpoint", "livre X" | `/feature` |
| "refacto", "refactor", "refonte", "extrait", "migre", "découpe" | `/refacto` |
| "incident", "bug", "500", "régression", "ça marche plus", "erreur en prod" | `/incident` |
| "audit clean-room", "audit à blanc", "audit indépendant" | `/audit-cleanroom` |
| "corrige findings audit", "fix cleanroom", "remédiation" | `/fix-cleanroom` |
| "corrige perf du bench", "applique reco perf" | `/fix-bench-perf` |
| "release", "publie", "tag", "image Docker", "livre version" | `/app-ship` → `/release` |
| "status", "où en est", "état sprint", "board" | `/app-status` |
| "planifie", "découpe en tickets", "DAG" | `/app-plan` |
| "exécute le sprint", "lance les devs", "drain le board" | `/app-build` |

> Si l'intention est ambiguë, demande UNE clarification puis route. Si la tâche est triviale (1 fichier, question), réponds directement sans orchestration.

## Garde-fous communs (style « process » — ordre, traçabilité, sécurité)
- **Branche** : tout le travail se fait sur **`develop`** (commits directs, **jamais** de branche par tâche). Préconditions : `develop` propre/à jour, `CLAUDE.md` lu, env minimal (`.env`) pour les points runtime.
- **Parallélisme** : lance les sous-tâches **indépendantes** en parallèle (un agent par work-package, périmètres de fichiers disjoints) ; sérialise les dépendances (schéma DB, migrations, services partagés). Les agents parallèles commitent sur `develop` par **périmètres de fichiers disjoints** pour éviter les collisions.
- **DoD par lot** : `pytest -v` vert · serveur boote (`uvicorn app.main:app`) · `GET /api/health` 200 · **migrations idempotentes** (rejouables sans erreur) · `ruff check` propre · OpenAPI/contrat à jour si l'API change.
- **Gate review** : `code-reviewer` (+ `security-reviewer` si surface sensible : auth, secrets, entrée utilisateur, CORS, écriture disque) relit le diff **sur `develop`** (commits du lot) avant la suite ; **cap 2 cycles** de corrections puis `blocked` + remontée. Pas de promotion `develop`→`main` tant que la review n'est pas verte.
- **Risky = approbation humaine** : migration de schéma, refacto large, réécriture historique git, **release (merge develop→main)**, purge de données → `needs-approval`, jamais en auto.
- **Idempotence / retry** : une étape qui échoue se rejoue (max 5 essais) avant escalade ; pas d'effet de bord double.
- **Traçabilité** : board `docs/31-board.md`, rapport `docs/daily/<date>.md`, bugs `docs/51-bugs.md`.
- **Fraîcheur CLAUDE.md (anti-dérive, OBLIGATOIRE)** : tout commit qui touche modules (§2), schéma SQLite/migrations, flux (§5) ou conventions (§3) **met à jour le bandeau CLAUDE.md (date+HEAD) + la section concernée dans le même commit**, OU lance **`/sync-context`** avant de clôturer. Le détecteur SessionStart (`.claude/hooks/session-start.js`) **et le hook git `post-commit`** (`.claude/hooks/post-commit`, installé dans `.git/hooks/`) signalent la dérive ; le gate review refuse un lot structurel dont la doc n'a pas suivi le code.

## Politique de routage modèle × effort (rationalise les tokens)

Chaque workflow multi-agents DOIT décider un couple `(modèle, effort)` par sous-tâche AVANT invocation. Ne PAS démarrer tout le monde en `opus + xhigh` par principe.

**Deux axes indépendants** (doc Anthropic 2026-07) :
- **Modèle** = capacité brute (`haiku` < `sonnet` < `opus` < `fable`)
- **Effort** = énergie cognitive dépensée (`low` < `medium` < `high` (défaut) < `xhigh` < `max`)

**Précédence** : `CLAUDE_CODE_EFFORT_LEVEL` (env var) > `effortLevel` (settings) > `effort:` (frontmatter agent) > défaut modèle.

⚠️ **Contrainte SDK à retenir** : le Task/Agent tool expose un paramètre `model:` override par invocation, mais **PAS de paramètre `effort` override**. L'effort d'un subagent = son frontmatter statique (ou l'env var si posée). Le routage runtime porte donc sur le CHOIX D'AGENT et l'OVERRIDE MODÈLE, pas sur l'effort par invocation.

**Doctrine complète** (matrice + escalade) = skill **`model-effort-routing`** (`.claude/skills/model-effort-routing/SKILL.md`), **consultée par les orchestrateurs** (`tech-lead` pour /refacto et /incident, `cto`+`tech-manager` pour /feature, `full-auditor` pour /audit-full et /wf-audit-incremental, Manager principal pour /sync-context).

**Règle d'or** = partir au plus bas couple viable, escalader ciblé sur `KO` de revue :
1. **1er échec** = même agent + **prompt enrichi** (rapport de revue + hints « considère 3 hypothèses »)
2. **2e échec** = **override modèle** via param `model:` du Task tool (ex. `backend-developer` sonnet → passe `model: "opus"`)
3. **3e échec** = **changement d'agent** vers une variante plus musclée (spécialiste domaine, `tech-lead`/`full-auditor` opus/fable)
4. **4e échec** = `BLOCKED` + recommander `CLAUDE_CODE_EFFORT_LEVEL=xhigh|max` en env var puis relance session (cap 2 relances)

**Qui lit cette skill** :
- `/feature` → `cto` et `tech-manager` la lisent avant de dispatcher les devs
- `/refacto` → `tech-lead` la lit avant de découper en vagues et invoquer `backend-developer`
- `/incident` → `tech-lead` la lit avant de router le fix
- `/audit-full` → `full-auditor` monolithique (pas de sous-invocation, applique la matrice à lui-même)
- `/sync-context`, `/benchmark`, `/wf-audit-incremental` → workflows mono-agent, la matrice guide le CHOIX de l'agent unique

**Override manuel** : `$env:CLAUDE_CODE_EFFORT_LEVEL="xhigh"` (PowerShell) avant la session pour forcer partout — utile pour un audit exhaustif ou un refacto bloqué.

## Détail des workflows « à orchestration » (dans `.claude/commands/<nom>.md`)
- **`/feature`** — *Requirements (`cpo`) → Architecture (`cto`/`tech-lead`) **délègue le découpage à `/app-plan`** → **exécution + review + QA via `/app-build`** (`backend-developer` + spécialistes domaine ; + `security-reviewer`/`perf-benchmarker` si surface sensible/chemin chaud) → **gate final `integration-agent`***. **Tout commité sur `develop`.** Réutilise réellement les briques `/app-plan`, `/app-build`, `/app-review`.
- **`/refacto`** — *Architecte (`tech-lead` : cartographie + plan par étapes + contrats/ADR) → Migration fichier par fichier (`backend-developer` ; étapes indépendantes via `/app-build`) → Validation régressions (`qa-engineer`+`perf-benchmarker`) → boucle (`tech-manager` + `/app-review`) → **gate final `integration-agent`***. Gros moteur (services IA, plex_generator, schéma DB) = **vague isolée** + retest, mais **toujours en commits sur `develop`** (petits, verts, réversibles), pas de branche dédiée.
- **`/incident`** — *Monitor (`logs/plexhub.log`, `/metrics`, repro `curl`) → Triage (`tech-lead`, sévérité) → Recherche (skill `systematic-debugging`, cause racine `fichier:ligne`) → Correctif (`backend-developer`/spécialiste) → Validation (`qa-engineer` + smoke boot) → postmortem*. Correctif sur `develop` (ou `hotfix/<version>` depuis `main` **uniquement** si prod cassée à chaud).
- **`/wf-audit-incremental`** — *Delta `<REF>..HEAD` (git) → Classification par zone d'impact → `full-auditor` en mode incrémental (skip zones non touchées) → Cross-check CLAUDE.md → Scorecard + Top findings + actions*.
- **Audit/Fix/Perf** = `/audit-cleanroom`, `/fix-cleanroom`, `/benchmark`→`/fix-bench-perf` ; `/audit-full` → `full-auditor` ; `/sync-context` → recalage inline (pas de délégation).

## Agents disponibles (rappel)
Direction : `ceo`/`cpo`/`cto`/`tech-lead`/`tech-manager`. IC : `backend-developer`. Qualité : `qa-engineer`, `code-reviewer`, `security-reviewer`, `integration-agent`, **`full-auditor`** (audit 360° + incrémental). Ops : `devops-engineer`, `release-manager`, `perf-benchmarker`, `observability-analyst`. Audit/contexte : `cleanroom-auditor`, `cleanroom-fixer`, `a0-cartographer`. Domaine : `db-migration-specialist`, `sync-specialist`, `ai-recsys-specialist`, `plex-generator-specialist`.
