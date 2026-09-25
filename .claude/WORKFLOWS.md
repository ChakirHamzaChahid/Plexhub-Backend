# 🧭 Workflows multi-agents PlexHub Backend — ROUTEUR (auto-injecté au SessionStart)

> **Au début de CHAQUE session : identifie l'intention de l'utilisateur et applique le workflow correspondant via sa commande.** Une tâche multi-étapes passe par son workflow ; **un seul rédacteur à la fois** — les sous-agents servent à explorer, vérifier et relire en contexte neuf, pas à écrire en parallèle (§ « Rédacteur unique » ci-dessous). Autorité = `CLAUDE.md` (§2 modules, §3 conventions, §5 flux, §9 pièges). Backend **FastAPI / Python 3.13**, SQLite (async/WAL).
>
> 🌱 **MODÈLE DE BRANCHES (en vigueur)** : tout le développement courant se fait **directement sur `develop`** — **on ne crée PAS de branche par tâche** (`feature/*`/`fix/*`/`refactor/*` proscrites). `main` = **stable/release uniquement**, atteinte par merge de `develop` lors d'une release + tag `vX.Y.Z` (seule exception : `hotfix/<version>` depuis `main` pour un correctif urgent de prod). Validation d'un lot = `pytest -v` + boot `uvicorn app.main:app` + `GET /api/health` 200.

## Les 7 workflows canoniques

| # | Workflow | Commande | Rôle |
|---|---|---|---|
| 1 | **Audit complet** (360°) | **`/audit-full`** | Diagnostic exhaustif lecture-seule (`full-auditor`), sortie `docs/audit/v*/`. Mensuel/trimestriel. |
| 2 | **Audit incrémental** (diff) | **`/wf-audit-incremental`** | Audit rapide du diff `<REF>..HEAD` (dernière release/lot). Post-lot ou hebdo. |
| 3 | **Benchmark** latence API | **`/benchmark`** | Mesures chiffrées par scénario (serveur booté, `/metrics`, logs `request_id`) + goulots. Puis `/fix-bench-perf` pour corriger. |
| 4 | **Sync context** (doc) | **`/sync-context`** | Recale le bandeau CLAUDE.md + sections impactées après un lot de commits structurels. Léger. |
| 5 | **Feature** (nouvelle fonctionnalité) | **`/feature`** | Requirements → Architecture + `/app-plan` → `/app-build` (dev séquentiel + review + QA) → gate `integration-agent`. **Chemin mineur** (1 session, sans PRD ni board) si les critères d'éligibilité de `feature.md` sont tous remplis ; revues sécurité/perf **uniquement sur déclencheur**. |
| 6 | **Refacto** (refonte ciblée) | **`/refacto`** | Architecte → Migration par étapes (indépendantes via `/app-build`) → Validation régressions → boucle (`/app-review`) → gate `integration-agent`. Risky par défaut. |
| 7 | **Incident** (bug/500/régression) | **`/incident`** | Monitor → Triage → Recherche cause racine → Correctif → Validation smoke boot + postmortem. |

**Commandes annexes** (utiles mais hors des 7) : `/audit-cleanroom` + `/fix-cleanroom` (audit table-rase `CR-*` → remédiation board), `/app-audit` (note vs House KB — sur ce repo, réutilise l'audit clean-room existant), `/fix-bench-perf` (suite de `/benchmark`), `/refresh-context` (régénération complète via `a0-cartographer`, plus lourd que `/sync-context`), `/release` (pipeline merge develop→main + tag + image GHCR — **seule voie de publication** : smoke de l'image candidate obligatoire avant le tag, rollback documenté) et son gate `/app-ship`, `/app-plan` (DAG board), `/app-build` (exécution sprint), `/app-review` (review d'un lot), `/app-status` (snapshot board), `/app-team` (roster), `/app-onboard`/`/app-init` (adoption/bootstrap d'un service), `/app-learn` (replier les conventions dans le House KB), `/app-run` (driver quasi-autonome bout-en-bout).

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
- **Rédacteur unique (revue 2026-09-24, NORMATIF)** : **un seul agent modifie l'arbre de travail à un instant donné** — `backend-developer` et les spécialistes domaine s'enchaînent, même sur des périmètres qui semblent disjoints. Le parallélisme est réservé aux agents **en lecture** : exploration, cartographie, revues (`code-reviewer` + `security-reviewer` dans le même message), audits. Pourquoi : chaque écriture porte des décisions implicites que l'autre rédacteur ignore (Cognition, « Don't Build Multi-Agents »), le code se parallélise mal (Anthropic, *multi-agent research system*) et `develop` n'a qu'un index git. Cette règle **prime** sur toute mention « en parallèle » encore présente dans une commande, une skill ou une fiche d'agent. **Pas de worktrees** : la règle projet est « tout sur `develop` », un worktree de sous-agent part de la branche par défaut du remote (`main`, pas `develop`), et `.worktreeinclude` y recopierait des fichiers gitignorés comme `.env` et ses secrets.
- **DoD par lot** : `pytest -v` vert · serveur boote (`uvicorn app.main:app`) · `GET /api/health` 200 · **migrations idempotentes** (rejouables sans erreur) · `ruff check` propre · OpenAPI/contrat à jour si l'API change.
- **Gate review** : `code-reviewer` (+ `security-reviewer` / `perf-benchmarker` **uniquement si le diff coche un déclencheur**, liste dans `commands/feature.md`) relit le diff **sur `develop`** (commits du lot) avant la suite ; **cap 2 cycles** de corrections puis `blocked` + remontée (= tentatives 2 et 3 du **compteur unique**, § routage ; le relecteur ne bloque que sur des écarts de correction — bug, contrat, sécurité, piège §9 —, le reste est une suggestion non bloquante). Pas de promotion `develop`→`main` tant que la review n'est pas verte **ni tant que `main` porte des commits absents de `develop`** (`git rev-list --count develop..main` doit valoir 0).
- **Risky = approbation humaine** : migration de schéma, refacto large, réécriture historique git, **release (merge develop→main)**, purge de données → `needs-approval`, jamais en auto.
- **Garde-fous non contournables (2026-09-25, repris d'ECC)** : `permissions.deny` sur `Read(.env)`/`Edit(.env)` (secrets locaux ; `.env.example` reste lisible) et `permissions.ask` sur `Edit(/pyproject.toml)` (config ruff/pytest/dépendances) — l'édition demande l'accord de Chakir même en mode auto/acceptEdits.
- **Idempotence / retry** : une étape qui échoue pour une cause **technique** (infra : daemon Gradle, ADB, réseau, verrou) se rejoue (max 5 essais) **dans** la tentative en cours, sans consommer le compteur unique ; un échec **de fond** (test rouge reproductible, revue KO) consomme une tentative (skill `model-effort-routing` §4). Pas d'effet de bord double.
- **Traçabilité** : board `docs/31-board.md`, rapport `docs/daily/<date>.md`, bugs `docs/51-bugs.md`.
- **Fraîcheur CLAUDE.md (anti-dérive, OBLIGATOIRE)** : tout commit qui touche modules (§2), schéma SQLite/migrations, flux (§5) ou conventions (§3) **met à jour le bandeau CLAUDE.md (date+HEAD) + la section concernée dans le même commit**, OU lance **`/sync-context`** avant de clôturer. Le détecteur SessionStart (`.claude/hooks/session-start.js`) **et le hook git `post-commit`** (`.claude/hooks/post-commit`, installé dans `.git/hooks/`) signalent la dérive ; le gate review refuse un lot structurel dont la doc n'a pas suivi le code.

## Politique de routage modèle × effort (rationalise les tokens)

Chaque workflow multi-agents DOIT décider un couple `(modèle, effort)` par sous-tâche AVANT invocation. Ne PAS démarrer tout le monde en `opus + xhigh` par principe.

**Deux axes indépendants** (doc Anthropic 2026-07) :
- **Modèle** = capacité brute — plage autorisée **`haiku` < `sonnet` < `opus`** (`fable` retiré le 2026-09-23 : plus cher qu'Opus 5.5 sans gain mesuré)
- **Effort** = énergie cognitive dépensée — plage autorisée **`medium` < `high`** (`low`, `xhigh`, `max` hors routage ; `xhigh`/`max` = décision de Chakir seul, par session)
- ⚠️ **Décision de Chakir (2026-09-23) : chaque tâche est notée** (grille 5 critères de la skill `model-effort-routing`) avant d'être confiée à un subagent, et reçoit le couple le plus bas qui la fera réussir — **jamais un couple fixé par rôle ou par workflow**. Chaque appel est précédé d'une ligne `ROUTAGE <tâche> : model=… effort=… agent=… — C1..C5 (raison)`.

**Précédence** : `CLAUDE_CODE_EFFORT_LEVEL` (env var) > `effortLevel` (settings) > `effort:` (frontmatter agent) > défaut modèle.

⚠️ **Contrainte SDK à retenir** : le Task/Agent tool expose un paramètre `model:` override par invocation, mais **PAS de paramètre `effort` override**. L'effort d'un subagent = son frontmatter statique (ou l'env var si posée). Le routage runtime porte donc sur le CHOIX D'AGENT et l'OVERRIDE MODÈLE, pas sur l'effort par invocation. **D'où les jumeaux** : chaque agent existe en `<agent>` (`effort: medium`, fiche SOURCE) et `<agent>-high` (`effort: high`, **générée** par `python .claude/tools/gen-effort-twins.py`, ne jamais l'éditer à la main ; `--check` détecte un jumeau absent, périmé ou orphelin). Choisir l'effort = choisir la fiche ; choisir le modèle = passer `model:` explicitement à chaque appel.

**Doctrine complète** (matrice + escalade) = skill **`model-effort-routing`** (`.claude/skills/model-effort-routing/SKILL.md`), **consultée par les orchestrateurs** (`tech-lead` pour /refacto et /incident, la session principale pour /feature (Analyser : `tech-lead`), `full-auditor` pour /audit-full et /wf-audit-incremental, Manager principal pour /sync-context).

**Agents intégrés `Explore` / `Plan` (2026-09-25)** : depuis Claude Code v2.1.198 ils héritent du modèle de la session, donc Opus — mesuré : 24 `Explore` en Opus sur 14 jours, 1 seul appel Haiku. **`model:` toujours explicite** (imposé par le hook `.claude/hooks/subagent-model-guard.js`) : `Explore` quick/medium = `haiku`, very thorough sur plusieurs modules = `sonnet` ; `Plan` noté par la grille. Détail : skill `model-effort-routing` § 3 (règle 2bis). `low` reste hors routage (sans effet sur Haiku), à réévaluer fin octobre avec `cost-tracker`.

**Compteur unique — 3 tentatives maximum par tâche (revue 2026-09-24)**. « Cycle de correction » (revue) et « cran d'escalade » (routage) désignent la **même** chose : il n'y a qu'un compteur. Partir au plus bas couple viable :
1. **Tentative 1** = le couple noté par la grille.
2. **Tentative 2** (1er KO : revue KO ou gate rouge) = **sous-agent neuf** — jamais la suite de la conversation qui a échoué — avec le rapport de revue joint, **et** effort +1 (fiche `<agent>-high`). Si déjà `high` (ou routé `haiku`, où l'effort compte peu) → modèle +1 à la place.
3. **Tentative 3** (2e KO) = **modèle +1** via `model:` (`haiku` → `sonnet` → `opus`), en gardant `high` — ou confier à un spécialiste domaine si le problème sort du périmètre de l'agent. Si déjà `opus`·`high`, il n'y a pas de tentative 3.
4. **KO suivant** = `BLOCKED` + remontée à Chakir, qui décide seul d'un éventuel `CLAUDE_CODE_EFFORT_LEVEL=xhigh` pour une session.
Chaque tentative = une nouvelle ligne `ROUTAGE`. Pourquoi 3 et un contexte neuf : la doc Claude Code recommande de repartir d'un contexte propre après deux corrections ratées plutôt que d'insister dans un contexte pollué par les essais échoués.
**Vocabulaire** : « session principale » = la conversation Claude Code que Chakir pilote (appelée « Manager principal » dans les fiches) ; elle orchestre, les sous-agents exécutent.

**Qui lit cette skill** :
- `/feature` → la session principale (qui planifie) la lit avant de dispatcher les devs ; `tech-lead` quand il mène l'Analyser (2bis)
- `/refacto` → `tech-lead` la lit avant de découper en vagues et invoquer `backend-developer`
- `/incident` → `tech-lead` la lit avant de router le fix
- `/audit-full` → `full-auditor` monolithique (pas de sous-invocation, applique la matrice à lui-même)
- `/sync-context`, `/benchmark`, `/wf-audit-incremental` → workflows mono-agent, la matrice guide le CHOIX de l'agent unique

**Override manuel (Chakir uniquement)** : `$env:CLAUDE_CODE_EFFORT_LEVEL="xhigh"` (PowerShell) avant la session force tous les agents à ce niveau et **court-circuite la grille** — réservé à un blocage après escalade complète, jamais posé par un agent.

**Mesure des tokens (2026-09-25, adapté d'ECC)** : un hook `Stop` asynchrone (`python .claude/tools/cost-tracker.py hook`) relit le transcript de la session **et** ceux de ses sous-agents, dédoublonne par `message.id` et tient `.claude/.cache/costs.jsonl` (une ligne par session, gitignorée). `python .claude/tools/cost-tracker.py report [--weeks N]` ventile par semaine : session principale / sous-agents, par workflow (attribution de la commande), par type d'agent et par modèle. C'est la mesure qui arbitre les routages contestés — pas l'intuition. Il tourne en arrière-plan (`async`), sort toujours en 0 et ne lit que les transcripts : il ne peut ni bloquer ni retarder un autre hook `Stop`.

**Survie à la compaction (2026-09-25, adapté d'ECC)** : un hook `PreCompact` (`python .claude/tools/lot-state.py save`) écrit `.claude/.cache/lot-state.md` — branche, HEAD, fichiers modifiés, `dod-gate status` (PlexHubTV), workflow en cours, dernières lignes `ROUTAGE` et tentatives vues par tâche, tickets ouverts du board, dernière demande. Au `SessionStart` `compact`/`resume`, il est réinjecté s'il a moins de 24 h ; au `startup`, une seule ligne le signale. C'est un instantané : git et le board font foi.

## Détail des workflows « à orchestration » (dans `.claude/commands/<nom>.md`)
- **`/feature`** — *Requirements + Architecture + découpage **dans la session principale**, un seul contexte (skills `product-management:write-spec`, `engineering:system-design`, `sprint-planner` ; phase 1bis **Clarifier** (≤ 5 questions à choix, tracées au PRD) puis 2bis **Analyser** (`tech-lead` neuf en lecture seule, matrice FR → tickets, un CRITICAL bloque le gate ; il challenge aussi l'archi si C2 = 2 ou C3 = 2) — skill `spec-quality`, modèle `.claude/templates/prd-template.md` — mesuré : 0 appel de `cpo`/`cto`/`tech-manager` du 12 au 24 septembre) → **exécution + review + QA via `/app-build`** (`backend-developer` + spécialistes domaine ; + `security-reviewer`/`perf-benchmarker` **uniquement sur déclencheur**, liste dans `feature.md`) ; **chemin mineur** en une session si éligible → **gate final `integration-agent`***. **Tout commité sur `develop`.** Réutilise réellement les briques `/app-plan`, `/app-build`, `/app-review`.
- **`/refacto`** — *Architecte (`tech-lead` : cartographie + plan par étapes + contrats/ADR) → Migration fichier par fichier (`backend-developer` ; étapes indépendantes via `/app-build`) → Validation régressions (`qa-engineer` ; `perf-benchmarker` **uniquement si la vague touche un chemin chaud**, liste dans `refacto.md`) → boucle (`tech-manager` + `/app-review`) → **gate final `integration-agent`***. Gros moteur (services IA, plex_generator, schéma DB) = **vague isolée** + retest, mais **toujours en commits sur `develop`** (petits, verts, réversibles), pas de branche dédiée.
- **`/incident`** — *Monitor (`logs/plexhub.log`, `/metrics`, repro `curl`) → Triage (`tech-lead`, sévérité) → **Confinement** si S1, ou S2 qui se propage (sauvegarde de la base, retour à la version précédente, sur accord) → Recherche (skill `systematic-debugging`, cause racine `fichier:ligne`) → Correctif (`backend-developer`/spécialiste) → Validation (`qa-engineer` + smoke boot) → postmortem*. Correctif sur `develop` (ou `hotfix/<version>` depuis `main` **uniquement** si prod cassée à chaud).
- **`/wf-audit-incremental`** — *Delta `<REF>..HEAD` (git) → Classification par zone d'impact → `full-auditor` en mode incrémental (skip zones non touchées) → Cross-check CLAUDE.md → Scorecard + Top findings + actions*.
- **Audit/Fix/Perf** = `/audit-cleanroom`, `/fix-cleanroom`, `/benchmark`→`/fix-bench-perf` ; `/audit-full` → `full-auditor` ; `/sync-context` → recalage inline (pas de délégation).

## Agents disponibles (rappel)
Direction : `ceo`/`cpo`/`cto`/`tech-lead`/`tech-manager`. IC : `backend-developer`. Qualité : `qa-engineer`, `code-reviewer`, `security-reviewer`, `integration-agent`, **`full-auditor`** (audit 360° + incrémental). Ops : `devops-engineer`, `release-manager`, `perf-benchmarker`, `observability-analyst`. Audit/contexte : `cleanroom-auditor`, `cleanroom-fixer`, `a0-cartographer`. Domaine : `db-migration-specialist`, `sync-specialist`, `ai-recsys-specialist`, `plex-generator-specialist`.
