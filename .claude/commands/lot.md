---
description: Lot suivant d'un plan découpé en lots — bilan, plan du lot avec routage, « go », exécution séquentielle, DoD, revue unique, journal, commit et ligne de coûts. Une session par lot.
argument-hint: [chemin du plan, ex. docs/plans/2026-07-26-refacto-audit-v1-plan.md]
allowed-tools: Read, Write, Edit, Glob, Grep, Bash, Task, Agent
---

> 🟢 **PlexHub Backend — FastAPI / Python.** `develop`. Lis `.claude/WORKFLOWS.md` (garde-fous) et la skill `model-effort-routing` : une ligne `ROUTAGE …` avant **chaque** sous-agent, `model:` toujours explicite (le hook le refuse sinon).

# /lot — exécuter le lot suivant d'un plan existant

Quand : le plan et ses décisions existent déjà (un plan découpé en lots, un PRD) et il s'agit d'avancer **d'un lot**. Pour une fonctionnalité **sans** plan : `/feature`. Pour une refonte : `/refacto`.

**Une session par lot.** Lot fini → `/clear` (ou nouvelle session) avant le suivant ; la reprise se fait par le plan, `lot-state` et git, jamais par la conversation précédente.

## Étapes (session principale)

1. **Repère** (lecture courte, rien n'est lancé)
   - Plan : `$ARGUMENTS`, sinon le plan cité par la dernière section du journal (`docs/context/journal.md`, lue par le haut) ; aucun plan trouvable → demande lequel.
   - `python .claude/tools/lot-state.py` · `git status --short` · `git log --oneline -10` · `python .claude/tools/dod-gate.py status`.
   - Dans le plan : la table des lots et la section du lot visé, **par plage** (`Grep` du titre puis lecture ciblée), pas le plan entier s'il est long.

2. **Bilan en 5 lignes** : lots faits (commits + journal), travail non commité et à quel lot il appartient, lot suivant, risques, points « à trancher par Chakir » encore ouverts.
   - **Travail non commité** → c'est le lot en cours : on le **finit** (étapes 5 à 7) avant tout nouveau lot. Ne jamais le réécrire ni l'écraser.

3. **Plan du lot** — présenté, **pas exécuté** :
   - tickets (≤ 2 pour une feature simple ; sinon le découpage du plan), chacun avec son périmètre de fichiers ou de modules ;
   - **découpage par couche** : un ticket qui touche plus de 2 paquets ou empile migration SQLite + service/worker + router/contrat se coupe ;
   - une ligne `ROUTAGE` par sous-agent prévu (développeurs, revues), grille notée, **puis vérifiée** : colle les lignes dans `python .claude/tools/routage.py check` et corrige chaque `KO` avant de présenter le plan (le résultat `N ligne(s), 0 ecart(s)` figure dans le plan) ;
   - déclencheurs de revues spécialisées (tableau de `commands/feature.md`) : `security-reviewer`, `perf-benchmarker` lancés ou « non requis — aucun déclencheur » ;
   - **Risky** (migration SQLite, secrets, release `develop`→`main`, refacto large, `/dav` ou `/api/metadata/v1`) → dis-le : approbation explicite exigée ;
   - client d'API externe ajouté ou modifié → politique d'usage vérifiée en ligne, fixtures synthétiques = réserve au journal.
   **Attends le « go » de Chakir.**

4. **Exécution** — un seul rédacteur à la fois, **un `backend-developer` neuf par ticket** (ou spécialiste domaine : `db-migration-specialist`, `sync-specialist`, `ai-recsys-specialist`, `plex-generator-specialist`), avec une fiche de passage : ticket et critères, périmètre, extrait utile du plan (pas le plan entier), `git log`/diff du ticket précédent, pièges du domaine (titres). DoD ticket : `python .claude/tools/dod-gate.py run --fast`. Rapport ≤ 30 lignes. **Ticket minuscule** (un fichier, mécanique, C2 = 0, C3 = 0) : la session principale le fait elle-même (`model-effort-routing` §3), puis `run --fast`. Rapport **partiel** (limite `maxTurns` atteinte) → reprendre le **même** agent une fois, ou découper le ticket ; `BLOCKED` après trois échecs identiques → nouvelle tentative par un agent neuf (compteur unique, `model-effort-routing` §4).

5. **DoD du lot** : `python .claude/tools/dod-gate.py run` (ruff + pytest avec couverture + boot `/api/health`). Rouge → correctif par un agent neuf (compteur unique de `model-effort-routing` §4). Vérifier également l'idempotence des migrations et les contrats OpenAPI (`docs/contracts/`) si l'API change.

5bis. **QA** (seulement si un critère d'acceptation nécessite une vérification runtime spécifique — ex. UI `/admin` HTMX, SSE, `/dav`, backup/restore) : un `qa-engineer` exerce les critères concernés ; sinon la DoD et la revue tiennent lieu de QA, et le rapport le dit.

6. **Revue unique** du diff consolidé : `code-reviewer` (règle « agents en lecture seule ») + `security-reviewer` **dans le même message** si déclencheur, sur les seuls fichiers concernés. Correctifs revus sur leur seul commit. Cap 2 cycles, puis `BLOCKED` remonté verbatim.

7. **Clôture**
   - Journal : une section en tête de `docs/context/journal.md` (livré, vérifié, non vérifié, réserves, ouvert) + la ligne de coûts : `python .claude/tools/cost-tracker.py lot` (à coller telle quelle). Pilote effort `low` utilisé → une ligne « pilote low : <ticket> <sonnet|opus> passé du premier coup / repris en `medium` ». Ligne de coûts marquée ⚠️ cache < 80 % → chercher ce qui a cassé le cache et le noter.
   - Flux changé → `docs/architecture/flux-cles.md` ; bug résolu → un piège dans `.claude/skills/pitfalls/references/<domaine>.md`, puis `python .claude/tools/pitfalls-index.py` ; bandeau de `CLAUDE.md` seulement si structurel (modules, migrations, pièges).
   - Plan : le lot marqué fait (ou partiel, avec ce qui reste).
   - Commit sur `develop` : `git add <chemins>` explicite + `git diff --cached --name-only`.

8. **Rapport final** (≤ 15 lignes) : ce qui est livré, réserves, lot suivant, et « nouvelle session pour le lot suivant ».

## Anti-patterns
- ❌ Enchaîner deux lots dans la même session.
- ❌ Lire le plan, le board ou le journal en entier quand une plage suffit.
- ❌ Lancer un nouveau lot alors qu'un travail non commité existe.
- ❌ Reporter à Chakir une obligation documentée d'une API externe au lieu de la corriger dans le lot.
