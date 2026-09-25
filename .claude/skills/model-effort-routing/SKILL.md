---
name: model-effort-routing
description: Doctrine PlexHub Backend consultée par les orchestrateurs (Manager principal, tech-lead, cto, tech-manager, full-auditor) AVANT CHAQUE invocation d'un subagent via Agent/Task. Chaque tâche est notée sur une grille de 5 critères qui fixe son MODÈLE (haiku / sonnet / opus, passé en `model:` à l'appel) et son EFFORT (medium / high, choisi via la fiche `<agent>` ou son jumeau `<agent>-high`). Donne aussi l'escalade sur échec. Plage autorisée = haiku..opus × medium..high ; low, xhigh, max et fable sont hors routage.
allowed-tools: Read
effort: high
---

# Doctrine de routage modèle × effort — PlexHub Backend

> **Décision de Chakir (2026-09-23)** : on ne fixe plus un couple modèle/effort par workflow ou par rôle. **Chaque tâche est évaluée** avant d'être confiée à un subagent, et reçoit le couple le plus bas qui la fera réussir. Objectif : ne pas brûler de tokens sur des tâches simples, sans sous-équiper les tâches risquées.

## 1. Plage autorisée

| Axe | Valeurs autorisées | Hors routage |
|---|---|---|
| **Modèle** | `haiku` < `sonnet` < `opus` | `fable` (retiré le 2026-09-23 : plus cher qu'Opus 5.5 sans gain mesuré) |
| **Effort** | `medium` < `high` | `low` (trop de régressions cachées ; sans effet sur Haiku 4.5, absent de la liste des modèles qui gèrent l'effort — à réévaluer fin octobre 2026 avec les données de `.claude/tools/cost-tracker.py report`), `xhigh` et `max` (coût nettement supérieur pour un gain faible, d'après Anthropic) |

`xhigh`/`max` ne peuvent être posés **que par Chakir**, à la main, pour une session (`CLAUDE_CODE_EFFORT_LEVEL`). Aucun agent, aucune matrice, aucune escalade automatique ne les choisit.

## 2. Mécanique : comment appliquer le couple choisi

Vérifié dans la doc Claude Code (« sub-agents », 2026-09) : **le modèle se choisit à l'appel, l'effort non** (il est figé dans le frontmatter de l'agent).

| Axe | Levier |
|---|---|
| **Modèle** | paramètre `model:` de l'outil Agent/Task (`"haiku"`, `"sonnet"`, `"opus"`) — **toujours le passer explicitement**, ne jamais compter sur le défaut de la fiche |
| **Effort** | choix de la fiche : `<agent>` = `medium`, `<agent>-high` = `high` |

Les jumeaux `<agent>-high.md` sont **générés** par `python .claude/tools/gen-effort-twins.py` depuis la fiche de base : on n'édite **que** la fiche de base, puis on relance le script (`--check` vérifie qu'aucun jumeau n'est absent, périmé ou orphelin).

⚠️ Opus 5.5 tourne par défaut en **`medium`** (doc Anthropic « Effort », vérifié 2026-09-23) ; les fiches de base le fixent explicitement pour que ce soit un choix et non un défaut subi.

⚠️ Haiku : l'effort y a peu d'effet — pour une tâche routée `haiku`, utiliser la fiche de base.

## 3. Grille d'évaluation (à remplir pour CHAQUE tâche)

| Critère | 0 | 1 | 2 |
|---|---|---|---|
| **C1 Portée** | 1 fichier | plusieurs fichiers d'un même paquet (`api/`, `services/`, `workers/`…) | plusieurs paquets, ou backend + app Android |
| **C2 Zone à risque** (pièges §9) | aucune | UI admin HTMX, schémas Pydantic, config/`.env` | migration SQLite, écritures concurrentes (`write_with_retry`), auth/SSRF/secrets, workers master/pipeline, écrivains d'identité (`match_locked`), `/dav`, downloads, release Docker |
| **C3 Ambiguïté** | spec précise, solution connue | spec partielle, un choix de conception à faire | cause inconnue, diagnostic à mener, arbitrage |
| **C4 Vérification** | un contrôle automatique tranche tout de suite (`pytest` ciblé, `ruff`) | tests partiels, à compléter | indirecte : vrai verrou SQLite, boot Docker, homelab/prod, navigateur (HTMX), revue, mesure perf |
| **C5 Nature** | mécanique (renommer, formater, grep, doc factuelle) | implémentation | jugement (revue, architecture, diagnostic, audit) |

### Règle de décision — modèle

1. **`opus`** si **au moins une** de ces conditions : C2 = 2 · C3 = 2 · (C5 = 2 **et** C1 = 2).
2. Sinon **`haiku`** si **toutes** : C5 = 0 · C1 = 0 · C2 = 0 · C4 = 0.
   **2bis.** Sinon **`haiku`** aussi pour une tâche **mécanique en lecture seule** (C5 = 0, aucune écriture : inventaire, grep, comptage, localisation d'un appel), **quelle que soit C1** — c'est le cas typique d'`Explore`. **`sonnet`** si elle doit tenir un contexte très large (lecture exhaustive de plusieurs modules, synthèse demandée). Jamais `opus` : une recherche qui est en fait un diagnostic (C3 = 2) se confie à l'agent de diagnostic, pas à `Explore`.
3. Sinon **`sonnet`**.

### Règle de décision — effort

- **`high`** si **au moins une** : C3 ≥ 1 · C4 = 2 · C2 = 2 · C1 = 2.
- Sinon **`medium`**.

### Agents intégrés `Explore` et `Plan` (règle du 2026-09-25)

Depuis Claude Code v2.1.198, `Explore` **hérite du modèle de la session principale** (donc Opus), et `Plan` aussi (doc « sub-agents », vérifié le 2026-09-25). Mesuré sur les transcripts des 14 jours précédents : 24 lancements d'`Explore` en Opus, 1 seul appel Haiku sur toute la période. D'où :

- **Toujours passer `model:`** à l'appel (le paramètre d'appel prime sur tout le reste).
- `Explore` en `quick` ou `medium` → **`haiku`** ; en `very thorough` sur plusieurs modules → **`sonnet`** (règle 2bis).
- `Plan` → noté par la grille comme une tâche de jugement (C5 = 2), en pratique `sonnet`, `opus` seulement si C2 = 2 ou C3 = 2.
- Effort : pas de fiche jumelle pour un agent intégré → écrire `effort=session` dans la ligne `ROUTAGE`.
- ⚠️ **Résultat vide = à contre-vérifier.** Test du 2026-09-25 : `Explore` sur Haiku a répondu « 0 fichier » pour `SyncWriteGate` (filtre `type: kt` invalide pour ripgrep, erreur avalée) alors que `git grep` en trouve 25. Avant de conclure qu'un symbole n'existe pas, contre-vérifier par `git grep`.

### Trace obligatoire

Avant chaque appel, l'orchestrateur écrit **une ligne de routage** dans son plan ou dans le board :

```
ROUTAGE <id tâche> : model=<haiku|sonnet|opus> effort=<medium|high> agent=<fiche> — C1=x C2=x C3=x C4=x C5=x (<raison en 5-10 mots>)
```

Sans cette ligne, la revue du lot peut refuser le routage. Elle sert aussi à recalibrer la grille après coup (tâche réussie du 1er coup en `high` alors que `medium` aurait suffi ⇒ la grille surestime).

### Exemples PlexHub Backend

| Tâche | C1 C2 C3 C4 C5 | Routage |
|---|---|---|
| Ajouter une clé dans `.env.example` + `config.py` | 1 1 0 0 0 | `sonnet` · `medium` (C2 = 1 exclut haiku) |
| Corriger une erreur `ruff` dans un seul fichier | 0 0 0 0 0 | `haiku` · `medium` |
| Nouveau service de lecture + tests `pytest`, spec claire | 1 0 0 0 1 | `sonnet` · `medium` |
| Nouvelle colonne dans l'UI admin HTMX | 1 1 1 2 1 | `sonnet` · `high` (à vérifier en navigateur) |
| Migration N+1 + entité + tests | 2 2 0 1 1 | `opus` · `high` |
| Revue d'un diff qui touche `write_with_retry` | 1 2 1 2 2 | `opus` · `high` |
| Revue d'un diff de template admin (2 fichiers) | 1 1 0 2 2 | `sonnet` · `high` |
| Cause racine d'un « database is locked » intermittent | 2 2 2 2 2 | `opus` · `high` |
| Recaler le bandeau de `CLAUDE.md` (`/sync-context`) | 0 0 0 0 0 | `haiku` · `medium` |
| `Explore` quick : trouver tous les appels d'une fonction | 2 0 0 0 0 | `haiku` · session (règle 2bis) |
| `Explore` very thorough : cartographier un flux sur 3 modules avec synthèse | 2 0 1 0 0 | `sonnet` · session (contexte large) |

## 4. Compteur unique sur échec (revue KO, gate rouge) — 3 tentatives max

Révisé le 2026-09-24. Il n'y a **qu'un compteur par tâche** : un « cycle de correction » de la revue et un « cran » d'escalade sont la même chose. Chaque tentative = une nouvelle ligne `ROUTAGE`.

1. **Tentative 1** : le couple noté par la grille.
2. **Tentative 2** (1er KO) : **sous-agent neuf** — jamais la suite de la conversation qui a échoué — avec le rapport de revue joint et des pistes (« considère 3 hypothèses »), **et** effort +1 (`medium` → fiche `-high`). Si déjà `high`, ou si la tâche est routée `haiku` (l'effort y compte peu) : modèle +1 à la place.
3. **Tentative 3** (2e KO) : **modèle +1** (`haiku` → `sonnet` → `opus`), en gardant `high` — ou spécialiste domaine si le problème sort du périmètre de l'agent. Si `opus` · `high` est déjà atteint, pas de tentative 3.
4. **KO suivant** : **`BLOCKED`** + remontée à Chakir, qui décide seul d'un éventuel `CLAUDE_CODE_EFFORT_LEVEL=xhigh` pour une session.

Pourquoi un contexte neuf et 3 tentatives : la doc Claude Code (*Best practices*) recommande, après deux corrections ratées, de repartir d'un contexte propre avec un meilleur prompt plutôt que d'insister dans un contexte encombré d'essais échoués.

## 5. Les orchestrateurs eux-mêmes

Leur propre couple se décide avec la même grille, au moment où la session principale les lance :

- `/feature` : planification (PRD + archi + board) et Clarifier dans la **session principale** ; Analyser (phase 2bis) = `tech-lead` neuf en lecture seule, `sonnet` · `medium` par défaut, `opus` · `high` si C2 = 2 ou C3 = 2 (il challenge alors aussi l'archi).
- `/refacto` et `/incident` : `tech-lead` · `high` dès que la cause ou le plan de migration n'est pas évident (C3 ≥ 1).
- `/audit-full` : `full-auditor` · `opus` · `high` (jugement cross-module, vérification indirecte).
- `/wf-audit-incremental` : `full-auditor` · `sonnet` ou `opus` selon les zones du diff (C2), `medium` si le delta est trivial.
- `/benchmark` : `perf-benchmarker` · `opus` · `high` (mesure de latence réelle = C4 = 2).
- `/sync-context` : session principale, travail le plus souvent `haiku`/`sonnet` · `medium`.
- `/fix-cleanroom` : `tech-manager` note **chaque finding** avec la grille avant de le confier à `cleanroom-fixer` (ou à un spécialiste domaine).

## 6. Défauts des modèles (rappel)

| Modèle | Effort par défaut si non spécifié |
|---|---|
| Opus 5.5 | `medium` (doc Anthropic « Effort », vérifié 2026-09-23) |
| Sonnet 5 | `high` |
| Haiku 4.5 | `medium` (implicite) |

## 7. Anti-patterns

- ❌ Appeler un subagent sans ligne `ROUTAGE` ni `model:` explicite.
- ❌ Lancer `Explore` ou `Plan` sans `model:` : ils héritent du modèle de la session, donc Opus.
- ❌ Choisir le couple par habitude de rôle (« un reviewer, c'est opus/high ») au lieu de noter la tâche.
- ❌ Tout en `opus` · `high` « par sécurité » → tokens gaspillés, latence ×2-3.
- ❌ Relancer l'agent qui a échoué dans la même conversation au lieu d'un sous-agent neuf, ou dépasser 3 tentatives sans passer par Chakir.
- ❌ Lancer deux agents qui écrivent en même temps (voir `WORKFLOWS.md` § « Rédacteur unique »).
- ❌ Éditer un fichier `<agent>-high.md` : il est régénéré, la modification serait perdue. Éditer la fiche de base puis relancer `gen-effort-twins.py`.
- ❌ Utiliser `low`, `xhigh`, `max` ou `fable` sans décision explicite de Chakir.
- ❌ Sauter la revue pour aller plus vite → dette qui revient en `/incident`.
