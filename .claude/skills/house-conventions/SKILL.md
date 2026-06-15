---
name: house-conventions
description: À invoquer AVANT d'écrire le moindre code, spec ou migration sur le backend PlexHub — charge le(s) pack(s) pertinents de la Knowledge Base maison pour que la sortie respecte les conventions réelles du backend au lieu de défauts génériques. Chaque agent IC et exec l'invoque en premier.
---

# House conventions (conventions maison)

Le projet embarque une Knowledge Base maison sous `.claude/knowledge/`, minée depuis le backend
réellement audité. Cette skill est la façon dont un agent charge le bon pack avant de travailler,
pour que ce qu'il produit ressemble au reste du backend — mêmes couches FastAPI, mêmes patterns
async/SQLAlchemy, mêmes invariants (migrations idempotentes, contrat 503 IA, secrets hors repo).

## Quand l'utiliser

Invoque cette skill **avant** la première édition/spec d'un ticket, sprint ou changement. Ça coûte
une lecture et évite toute une classe de retouches « ça ne ressemble pas à comment on construit ici ».

## Procédure

1. Identifie ce que tu t'apprêtes à faire et charge le(s) pack(s) correspondant(s). Les packs vivent
   sous **`.claude/knowledge/<pack>.md`** (relatif au projet). Si tu ne les trouves pas, glob
   `**/.claude/knowledge/stack-defaults.md` et lis les packs depuis ce dossier. Si tu ne les trouves
   vraiment pas, **STOP et signale un blocage** — ne procède jamais en silence sur des défauts
   génériques ; c'est exactement l'échec que cette skill évite.

   | Tu t'apprêtes à… | Lis |
   |---|---|
   | Choisir stack / versions / libs | `stack-defaults.md` |
   | Écrire ou relire du code Python/FastAPI (routers, services, workers, models) | `python-conventions.md` (+ `stack-defaults.md`) |
   | Concevoir / modifier des endpoints REST, codes d'erreur, contrat `X-API-Key`, OpenAPI | `api-conventions.md` |
   | Ajouter du logging, des métriques, toucher au request_id ou aux secrets | `observability.md` |
   | Mettre en place git, commits, CI, release Docker, versioning | `git-workflow.md` |

   Note : pour toucher au schéma SQLite / aux migrations, charge `python-conventions.md` (section
   SQLAlchemy/SQLite) **et** respecte la house law `CLAUDE.md` §9 (migrations idempotentes en fin de
   chaîne, DDL destructif = `needs-approval`).

2. Traite le pack comme le **plancher**. Suis-le sauf si les `docs/` du projet (architecture,
   `docs/22-impl-spec-backend.md`, `CLAUDE.md`) surchargent explicitement une règle — un projet peut
   être plus strict, jamais plus laxiste. L'autorité de vérité ultime reste **le code**
   (`fichier:ligne`) : un pack qui diverge du code doit être corrigé, pas suivi aveuglément.

3. Si le pack et les docs du projet **entrent en conflit**, les docs du projet gagnent pour ce
   projet, mais écris une ligne dans ton fragment de run (`docs/daily/<date>-<agent>-<ticket>.md`)
   notant la divergence, pour que le `tech-manager` décide si c'est la KB ou le projet qui a tort.

4. Si tu découvres une convention réellement nouvelle et réutilisable en travaillant, ajoute une
   ligne `LEARNING:` dans ce même fragment, pour que `/refresh-context` la replie dans la KB.

## Definition of Done (plancher backend, partout)

Tout changement doit, au minimum, satisfaire :
- `pytest -v` **vert** (pytest-asyncio en mode auto)
- boot OK : `uvicorn app.main:app` démarre sans erreur (lifespan, init DB)
- `GET /api/health` répond **200**
- migrations **idempotentes** (re-run sans casse, `IF NOT EXISTS`)
- **OpenAPI à jour** (schémas Pydantic v2 aux frontières, pas de dict nu)

## Anti-patterns

- Écrire le code d'abord et vérifier les conventions après — la retouche est le coût qu'on évitait.
- Inventer une nouvelle archi/un nouveau pattern alors qu'un pack en spécifie déjà un.
- Suivre la KB en silence quand la doc du projet dit autre chose (ou l'inverse) sans le signaler.
- « Corriger » un garde-fou maison (db_retry, SafeRotatingFileHandler, contrat 503 IA) par méconnaissance.
