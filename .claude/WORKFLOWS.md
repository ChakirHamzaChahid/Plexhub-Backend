# 🧭 Workflows multi-agents PlexHub Backend — RÉFÉRENCE COMPLÈTE

> Le SessionStart injecte la version courte **`.claude/WORKFLOWS-ROUTER.md`**, qui porte aussi les **règles d'économie de tokens (source unique, normatives)**. Ce fichier-ci se lit au lancement d'un workflow.

> **Au début de CHAQUE session : identifie l'intention de l'utilisateur et applique le workflow correspondant via sa commande.** Une tâche multi-étapes passe par son workflow ; **un seul rédacteur à la fois** — les sous-agents servent à explorer, vérifier et relire en contexte neuf, pas à écrire en parallèle (§ « Rédacteur unique » ci-dessous). Autorité = `CLAUDE.md` (§2 modules, §3 conventions, §5 flux) + `.claude/skills/pitfalls/`. Backend **FastAPI / Python 3.12-3.13**, SQLite (async/WAL).
>
> 🌱 **MODÈLE DE BRANCHES (en vigueur)** : tout le développement courant se fait **directement sur `develop`** — **on ne crée PAS de branche par tâche** (`feature/*`/`fix/*`/`refactor/*` proscrites). `main` = **stable/release uniquement**, atteinte par merge de `develop` lors d'une release + tag `vX.Y.Z` (seule exception : `hotfix/<version>` depuis `main` pour un correctif urgent de prod). Validation d'un lot = `python .claude/tools/dod-gate.py run` (`ruff check .` + `pytest` + boot `/api/health` 200).

## Les 8 workflows canoniques

| # | Workflow | Commande | Rôle |
|---|---|---|---|
| 1 | **Audit complet** (360°) | **`/audit-full`** | Diagnostic exhaustif lecture-seule (`full-auditor`), sortie `docs/audit/v*/`. Mensuel/trimestriel. |
| 2 | **Audit incrémental** (diff) | **`/wf-audit-incremental`** | Audit rapide du diff `<REF>..HEAD` (dernière release/lot). Post-lot ou hebdo. |
| 3 | **Benchmark** latence API | **`/benchmark`** | Mesures chiffrées par scénario (serveur booté, `/metrics`, logs `request_id`) + goulots. Puis `/fix-bench-perf` pour corriger. |
| 4 | **Sync context** (doc) | **`/sync-context`** | Recale le bandeau `CLAUDE.md` + sections impactées après un lot de commits structurels. Léger. |
| 5 | **Feature** (nouvelle fonctionnalité) — ⚠️ **2 sessions** (spec, puis build en contexte neuf) | **`/feature`** | Requirements → Architecture + `/app-plan` → `/app-build` (dev séquentiel + review + QA) → gate `integration-agent`. **Chemin mineur** (1 session, sans PRD ni board) si les critères d'éligibilité de `feature.md` sont tous remplis ; revues sécurité/perf **uniquement sur déclencheur**. |
| 6 | **Refacto** (refonte ciblée) | **`/refacto`** | Architecte → Migration par étapes (indépendantes via `/app-build`) → Validation régressions → boucle (`/app-review`) → gate `integration-agent`. Risky par défaut. |
| 7 | **Incident** (bug/500/régression) | **`/incident`** | Monitor → Triage → Recherche cause racine → Correctif → Validation smoke boot + postmortem. |
| 8 | **Lot** (lot suivant d'un plan existant) | **`/lot`** | Bilan 5 lignes → plan du lot + ROUTAGE → « go » → devs l'un après l'autre → DoD → revue unique → journal + ligne de coûts → commit. Une session par lot. |

**Commandes annexes** : `/audit-cleanroom` + `/fix-cleanroom` (audit table-rase `CR-*` → remédiation board), `/app-audit`, `/fix-bench-perf`, `/refresh-context` (régénération complète via `a0-cartographer`), `/release` (pipeline merge `develop`→`main` + tag + image GHCR — **seule voie de publication**) et son gate `/app-ship`, `/app-plan`, `/app-build`, `/app-review`, `/app-status`, `/app-team`, `/app-onboard`/`/app-init`, `/app-learn`, `/app-run`. État d'un lot : `python .claude/tools/lot-state.py`.

## Auto-activation contextuelle (routing par mots-clés)

Détecte l'intention dans le prompt de l'utilisateur et propose le workflow **avant** de coder. Si le match est ambigu → 1 clarification puis route ; si la tâche est triviale (1 fichier, question) → réponds directement.

| Mots-clés / patterns dans le prompt utilisateur | Workflow suggéré |
|---|---|
| "lot suivant", "reprends le plan", "continue le plan" | `/lot` |
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
| "status", "où en est", "état sprint", "board" | `python .claude/tools/lot-state.py` + `git log --oneline -10` |

## Garde-fous communs (style « process » — ordre, traçabilité, sécurité)
- **Branche** : tout le travail se fait sur **`develop`** (commits directs, **jamais** de branche par tâche). Préconditions : `develop` propre/à jour, `CLAUDE.md` lu, `.env` pour les points runtime.
- **Rédacteur unique (NORMATIF)** : **un seul agent modifie l'arbre de travail à un instant donné** — `backend-developer` et les spécialistes domaine s'enchaînent, même sur des périmètres qui semblent disjoints. Le parallélisme est réservé aux agents **en lecture** : exploration, cartographie, revues (`code-reviewer` + `security-reviewer` dans le même message), audits. **Pas de worktrees** : tout sur `develop`.
- **DoD par lot — DÉTERMINISTE** : elle se joue en **une commande** qui produit ses preuves, et un hook `Stop` refuse de clore un tour si du code source a été modifié sans contrôle vert depuis.
  ```
  python .claude/tools/dod-gate.py run        # complète : ruff + pytest (couverture) + boot /api/health
  python .claude/tools/dod-gate.py run --fast # rapide : ruff + pytest --no-cov (par ticket)
  python .claude/tools/dod-gate.py status     # où j'en suis
  python .claude/tools/dod-gate.py verify --full  # gate final : vérifie empreinte + DoD complète
  ```
  Un fichier Markdown ne déclenche rien (une session de doc n'est jamais bloquée). Après 3 blocages consécutifs il cesse d'insister. Coupe-circuit : `PLEXHUB_DOD_GATE=off` (à dire à Chakir, jamais en silence).
- **DoD à DEUX niveaux** :
  - **Ticket (agent dev)** = `python .claude/tools/dod-gate.py run --fast` (ou `pytest tests/test_<cible>.py -q --no-cov` pendant l'itération, puis `dod-gate.py run --fast` avant de rendre la main).
  - **Lot (gate final)** = `python .claude/tools/dod-gate.py run` (ruff + pytest complet avec `--cov-fail-under=70` + boot `/api/health` 200) + migrations idempotentes + OpenAPI/contrat à jour si l'API change.
- **Gate review** : `code-reviewer` (+ `security-reviewer` / `perf-benchmarker` **uniquement si le diff coche un déclencheur**, liste dans `commands/feature.md`) relit le diff consolidé **sur `develop`** ; **cap 2 cycles** de corrections puis `BLOCKED` + remontée (= tentatives 2 et 3 du **compteur unique**). Le relecteur ne bloque que sur des écarts de correction (bug, contrat, sécurité, piège) ; le correctif d'un cycle est revu sur son seul commit.
  - Si la surface est sensible, `security-reviewer` et `code-reviewer` partent **dans le même message** (`security-reviewer` sur les seuls fichiers qui cochent un déclencheur), et leurs findings sont renvoyés **ensemble** au dev.
- **Découpage** : **≤ 2 tickets** pour une feature simple ; **un seul agent dev à la fois**, tickets enchaînés, **chaque ticket dans un agent NEUF** avec une fiche de passage.
- **Doc de lot** (journal `docs/context/journal.md`, pièges `.claude/skills/pitfalls/` ; bandeau `CLAUDE.md` **seulement si structurel**) : **au gate final uniquement**, jamais par ticket.
- **Client d'API externe** : un lot qui ajoute ou modifie un client d'API externe vérifie la politique d'usage dans la doc officielle avant de classer un point « à trancher par Chakir », et note toute fixture synthétique comme réserve au journal.
- **Git** : `git add <chemins>` explicite **et** `git diff --cached --name-only` vérifié avant chaque commit (`bash-guard.js` bloque `git add .`/`-A`/`-u`, `git commit -a`, `--force`, `--hard`, création de branche).
- **Risky = approbation humaine** : migration SQLite, refacto large, réécriture historique git, **release (merge `develop`→`main`)**, purge de données → `needs-approval`, jamais en auto.
- **Garde-fous qualité non contournables** : `permissions.deny` sur `Read(.env)`/`Edit(.env)`/`gh secret set|delete|remove`, et `permissions.ask` sur `Edit(/pyproject.toml)`, `Edit(/requirements.txt)`, `Edit(/requirements-dev.txt)`.
- **Idempotence / retry** : une étape qui échoue pour une cause **technique** (réseau, verrou SQLite transitoire) se rejoue (max 5 essais) **dans** la tentative en cours, sans consommer le compteur unique ; un échec **de fond** consomme une tentative (`model-effort-routing` §4).
- **Traçabilité** : board `docs/31-board.md` (`Grep` ciblé, jamais lu en entier), journal `docs/context/journal.md`, rapport `docs/daily/<date>.md`, bugs `docs/51-bugs.md` + `.claude/skills/pitfalls/`.
- **Découpage `/feature` en 2 sessions** : la coupure après le gate archi est **normative**. La session 2 démarre en contexte neuf.
- **Fraîcheur `CLAUDE.md` (anti-dérive, OBLIGATOIRE)** : tout commit qui touche modules (§2), schéma SQLite/migrations, flux (§5) ou conventions (§3) **met à jour le bandeau `CLAUDE.md` (date+HEAD) + la section concernée dans le même commit**, OU lance **`/sync-context`** avant de clôturer. Le détecteur SessionStart (`.claude/hooks/session-start.js`) et le hook git `post-commit` signalent la dérive.

## Politique de routage modèle × effort (rationalise les tokens)

Chaque sous-agent reçoit un couple `(modèle, effort)` **noté par la grille** de la skill `model-effort-routing` — **source unique** des règles, des exemples et de l'escalade — annoncé par une ligne `ROUTAGE <tâche> : model=… effort=… agent=… — C1..C5 (raison)` et vérifié par `python .claude/tools/routage.py check`. Rappels : modèle `haiku` < `sonnet` < `opus`, passé par `model:` (obligatoire, refusé sinon par `.claude/hooks/subagent-model-guard.js`) ; effort `medium` < `high` (`low` en pilote pour `backend-developer-low`, tickets vérifiables C2 ≤ 1, C3 = 0, C4 = 0 ; généré par `python .claude/tools/gen-effort-twins.py`) ; Opus pour un agent qui écrit si C2 = 2 ou C3 = 2, pour un lecteur seulement en sécurité et en synthèse d'audit ; jamais Fable pour un sous-agent ; `xhigh`/`max` = Chakir seul. `Explore` = `haiku` (`sonnet` pour un balayage « very thorough »).

**Compteur unique — 3 tentatives maximum par tâche**. « Cycle de correction » (revue) et « cran d'escalade » (routage) désignent la **même** chose :
1. **Tentative 1** = le couple noté par la grille.
2. **Tentative 2** (1er KO : revue KO ou gate rouge) = **sous-agent neuf** avec le rapport de revue joint, **et** effort +1 (fiche `<agent>-high`). Si déjà `high` (ou `haiku`) → modèle +1 à la place.
3. **Tentative 3** (2e KO) = **modèle +1** via `model:` (`haiku` → `sonnet` → `opus`), en gardant `high`. Si déjà `opus`·`high`, il n'y a pas de tentative 3.
4. **KO suivant** = `BLOCKED` + remontée à Chakir.

**Override manuel (Chakir uniquement)** : `$env:CLAUDE_CODE_EFFORT_LEVEL="xhigh"` (PowerShell).

**Mesure des tokens** : hook `Stop` asynchrone (`python .claude/tools/cost-tracker.py hook`) → `.claude/.cache/costs.jsonl`. En fin de lot, `python .claude/tools/cost-tracker.py lot` donne la ligne à coller dans le journal (`/lot`).

**Survie à la compaction** : hook `PreCompact` (`python .claude/tools/lot-state.py save`) → `.claude/.cache/lot-state.md`, réinjecté au `SessionStart` `compact`/`resume`.

## Détail des workflows
Le déroulé de chaque workflow est dans `.claude/commands/<nom>.md`, **source unique** : ce fichier ne le recopie pas.

## Agents disponibles (rappel)
⚠️ **`.claude/agents/` fait foi.**
Direction/orchestration : `ceo`, `cpo`, `cto`, `tech-lead`, `tech-manager`. IC : `backend-developer` (`-low` pilote, `-high`). Qualité : `qa-engineer`, `code-reviewer`, `security-reviewer`, `integration-agent`, `full-auditor`. Ops : `devops-engineer`, `release-manager`, `perf-benchmarker`, `observability-analyst`. Audit/contexte : `cleanroom-auditor`, `cleanroom-fixer`, `a0-cartographer`. Domaine : `db-migration-specialist`, `sync-specialist`, `ai-recsys-specialist`, `plex-generator-specialist`.
