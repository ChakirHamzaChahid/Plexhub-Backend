# PlexHub Backend — Knowledge Base (packs de conventions)

Ces packs encodent **comment ce backend est réellement construit et exploité**. Chaque agent les lit
via la skill `house-conventions` **avant** d'écrire du code ou des specs.

## Packs

| Pack | Ce qu'il gouverne |
|---|---|
| `stack-defaults.md` | Langage, versions, libs, runtime, build/test réels du backend |
| `python-conventions.md` | Architecture FastAPI, couches, async, SQLAlchemy, validation, tests |
| `git-workflow.md` | Modèle de branches, commits, versioning, CI, release Docker, secrets |
| `observability.md` | Logging structuré (request_id), métriques Prometheus, règles « pas de secret » |
| `api-conventions.md` | Design des endpoints REST, codes d'erreur, contrat `X-API-Key`, OpenAPI |

## Comment les agents les utilisent
Un agent charge le(s) pack(s) pertinent(s) via `house-conventions` **avant** d'écrire, puis les suit
sauf si `CLAUDE.md` ou `docs/` du projet surchargent explicitement une règle. Les conventions maison
sont le **plancher**, pas le plafond.

## Gouvernance — vivant
`/refresh-context` (via `a0-cartographer`) re-cartographie le code à HEAD et corrige les packs +
`CLAUDE.md` ; `/sync-context` recale le bandeau de fraîcheur. **L'autorité de vérité reste le code**
(`fichier:ligne`) — un pack qui diverge du code doit être corrigé, pas suivi aveuglément.
