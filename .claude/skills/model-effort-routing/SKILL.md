---
name: model-effort-routing
description: Doctrine PlexHub Backend consultée par les orchestrateurs (Manager principal, tech-lead, cto, tech-manager, full-auditor) AVANT toute invocation d'un subagent via Task. Donne la matrice « famille de tâche → couple modèle+effort de départ », les défauts par modèle et la politique d'escalade RÉELLE (changement d'agent OU override modèle par invocation — l'effort n'est PAS overridable par invocation dans le SDK Claude Code). Vérifié doc Anthropic 2026-07 : effort ∈ {low, medium, high, xhigh, max}, précédence env > settings > frontmatter agent > défaut modèle. Task tool expose param `model` override, PAS `effort`.
allowed-tools: Read
effort: high
---

# Doctrine de routage modèle × effort — PlexHub Backend

> **Consultée par les orchestrateurs** (Manager principal, `tech-lead` pour /refacto, `cto`+`tech-manager` pour /feature, `full-auditor` pour les audits) AVANT toute invocation subagent. Cette skill est une **RÉFÉRENCE**, PAS un runtime — il n'y a pas d'agent routeur dédié (les orchestrateurs appliquent la matrice eux-mêmes).

## Ce qui est overridable à l'invocation, ce qui ne l'est pas

**Vérifié doc Anthropic 2026-07** :

| Paramètre | Overridable par Task/Agent invocation ? | Comment ajuster ? |
|---|---|---|
| **Modèle** (`haiku`/`sonnet`/`opus`/`fable`) | ✅ **Oui** — param `model:` du Task tool | Passe `model: "opus"` à l'invocation pour override le frontmatter |
| **Effort** (`low`/`medium`/`high`/`xhigh`/`max`) | ❌ **Non** — pas de param à l'invocation | Statique via frontmatter agent, OU env var globale `CLAUDE_CODE_EFFORT_LEVEL`, OU variante d'agent dédiée |

**Précédence effective** : `CLAUDE_CODE_EFFORT_LEVEL` (env, session) > `effortLevel` (settings) > `effort:` (frontmatter agent) > défaut modèle.

## Principe fondateur

Deux axes indépendants :

- **Modèle** = capacité brute (« quel niveau d'intelligence ? »)
- **Effort** = énergie cognitive dépensée (« combien de raisonnement avant de répondre ? »)

**Règle d'or** : partir au plus bas couple viable, escalader ciblé si la revue échoue.

## Matrice de départ par famille de tâche

| Famille | Modèle départ | Effort figé dans frontmatter | Exemples PlexHub Backend |
|---|---|---|---|
| Classification, extraction, lint/ruff fix, renommage | `haiku` | `low` | Renommer une constante, formater, corriger un import |
| Recherche fichier, grep, lecture d'audit/board | `haiku` | `medium` | Trouver les refs à un symbole, lister les tests d'un module |
| Endpoint CRUD simple, schéma Pydantic, template admin | `sonnet` | `medium` | Ajouter un champ camelCase, une colonne d'UI HTMX |
| Logique métier moyenne, tests pytest, service isolé | `sonnet` | `high` | Nouveau service de lecture, garde §9, fixture async |
| Architecture, refacto important, débogage difficile | `opus` | `high` | Découpe d'un god-file (`sync_worker`/`ai.py`), refonte agrégation |
| Sécurité / migration de schéma / cross-stack | `opus` | `xhigh` | Migration N+1 + entités + tests + doc, durcissement auth/CORS, chiffrement Fernet |
| Analyse ambiguë, stratégie, arbitrage transverse critique | `fable` | `xhigh` | Cause racine « database is locked » intermittent, audit clean-room 360° |
| Vérification finale d'un lot risky | `opus` ou `fable` | `high`/`xhigh` | Review d'une refonte cross-module, gate sécurité pré-release |

⚠️ **Fable 5 réservé** aux tâches complexes ET risquées (`full-auditor`/`cleanroom-auditor`/`a0-cartographer`).

## Défauts modèles (rappel Anthropic 2026-07)

| Modèle | Effort par défaut si non spécifié |
|---|---|
| Fable 5 | `high` |
| Sonnet 5 | `high` |
| Opus 4.8 | `high` |
| Opus 4.7 | `xhigh` |
| Opus 4.6 | `high` |
| Sonnet 4.6 | `high` |
| Haiku 4.5 | `medium` (implicite) |

**Fallback intelligent** : `xhigh` demandé sur Opus 4.6 tourne comme `high` (Claude Code retombe au plus haut supporté).

## Politique d'escalade RÉELLE sur échec de revue

Distinguer **échec de raisonnement** (réponse superficielle) et **échec de capacité** (le modèle « bute »).

⚠️ **Contrainte SDK** : l'`effort` n'est PAS overridable par invocation Task. Donc pas de « même agent + effort supérieur » via Task. Les leviers réels :

1. **1er échec** = ré-invoquer le **MÊME agent** avec un **prompt enrichi** (rapport de revue joint, hints « considère 3 hypothèses », « raisonne étape par étape »). Ça pousse le modèle à réfléchir plus SANS toucher l'effort config.
2. **2e échec** = ré-invoquer avec **override modèle** via param `model:` du Task tool (`backend-developer` sonnet → passe `model: "opus"` à l'invocation, l'agent tourne alors en `opus` avec son effort frontmatter).
3. **3e échec** = **changer d'agent** vers une variante plus musclée (ex. délégation à un spécialiste domaine — `db-migration-specialist`, `sync-specialist`, `ai-recsys-specialist`, `plex-generator-specialist` — OU à `tech-lead`/`full-auditor` opus/fable/xhigh).
4. **4e échec** = **BLOCKED** + suggérer à l'humain de poser `CLAUDE_CODE_EFFORT_LEVEL=xhigh` (ou `max`) et de relancer la session pour un dernier essai global. Cap 2 relances puis remontée.

Ne JAMAIS démarrer tout le monde en `opus + xhigh` par défaut → gaspillage massif de tokens.

## Points de départ recommandés par workflow

| Workflow | Orchestrateur (lit cette skill) | Exécution majoritaire | Vérification |
|---|---|---|---|
| `/feature` | `cpo`+`cto`+`tech-manager` opus/high | `backend-developer` sonnet/high, spécialistes domaine sonnet/high | `code-reviewer` opus/high, `security-reviewer` opus/xhigh si sensible |
| `/refacto` | `tech-lead` opus/xhigh (cartographie) | `backend-developer` sonnet/high (override `model:"opus"` sur vague critique) | `perf-benchmarker` opus/high, `code-reviewer` opus/xhigh |
| `/incident` | `tech-lead` opus/xhigh (cause racine) | `backend-developer`/spécialiste sonnet/high | `qa-engineer` sonnet/high + smoke boot `/api/health` |
| `/audit-full` | `full-auditor` fable/xhigh (monolithique) | — | Cross-check `code-reviewer` opus/high |
| `/wf-audit-incremental` | `full-auditor` opus/high (diff scope réduit) | — | (skip si delta trivial) |
| `/benchmark` | `perf-benchmarker` opus/high (monolithique) | — | Cross-check `observability-analyst` sonnet/medium |
| `/sync-context` | Manager principal sonnet/high (édition ciblée doc) | — | Auto-vérification (grep ancres) |
| `/fix-cleanroom` | `tech-manager` opus/high (dispatch board) | `cleanroom-fixer` opus/high | `code-reviewer` opus/high |

## Comment cette doctrine est appliquée en runtime

**Il n'y a PAS d'agent routeur runtime.** À la place :

1. Chaque **orchestrateur** de workflow doit **lire cette skill** avant d'invoquer un subagent (cf. `tech-lead`, `cto`, `tech-manager`, `full-auditor`).
2. L'orchestrateur décide **quel agent invoquer** en s'appuyant sur la matrice ci-dessus.
3. Si un override modèle est justifié pour cette invocation (ex. `backend-developer` normalement sonnet, mais pour la migration critique on passe `model: "opus"`), il le fait via le paramètre `model:` du Task tool.
4. Sur KO de revue, il applique la politique d'escalade : prompt enrichi → override modèle → changement d'agent → BLOCKED.

**Override global de session (levier ultime)** : `CLAUDE_CODE_EFFORT_LEVEL=xhigh` (ou `max`) posé en env var avant de lancer Claude Code force tous les agents de la session à ce niveau d'effort. Utile pour un `/audit-full` exhaustif ou un `/refacto` bloqué.

## Anti-patterns

- ❌ Tout en `opus + xhigh` par principe → tokens gaspillés, latence x3
- ❌ Tout en `haiku + low` pour économiser → régressions cachées, escalades multiples
- ❌ Tenter d'override l'`effort` par param Task (non supporté) — utiliser env var, variante d'agent, ou override modèle
- ❌ Monter le modèle avant d'avoir tenté prompt enrichi (moins coûteux)
- ❌ Ignorer la vérification (skip du reviewer) pour aller plus vite → dette qui explose en `/incident` plus tard
- ❌ Utiliser Fable 5 pour une tâche non risquée → coût élevé sans bénéfice mesurable
