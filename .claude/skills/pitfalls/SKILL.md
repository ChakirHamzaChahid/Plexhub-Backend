---
name: pitfalls
description: >-
  Pièges connus de PlexHub Backend — règles issues de bugs réels, par
  domaine (DB/migrations, sync/Xtream/EPG, API/auth, scrape/enrichissement,
  IA/média/Plex/WebDAV, ops/runtime).
  À consulter AVANT d'éditer du code applicatif, et quand un
  comportement surprend. Remplace le §9 inliné de CLAUDE.md.
---

# Pièges connus PlexHub Backend

Chaque piège est une règle **payée par un bug réel**. L'index ci-dessous suffit souvent : si un titre parle de ce que tu t'apprêtes à toucher, ouvre le fichier du domaine pour le texte complet (cause, preuve `fichier:ligne`, règle).

## Comment l'utiliser

1. Repère le domaine du fichier que tu vas modifier dans la table ci-dessous.
2. Lis **seulement** l'index de ce domaine (`index/<domaine>.md`, un titre par piège).
3. Si un titre touche à ce que tu changes, lis son texte complet dans `references/<domaine>.md` (par `Grep` sur le titre, puis lecture par plages).

> Découpé depuis `CLAUDE.md` §9 (économie de tokens : évite de relire ~60 Ko de pièges à chaque tour). Le hook `.claude/hooks/pitfalls-guard.js` lit les mêmes fichiers d'index.

## Domaines

| Domaine | Couvre | Index des titres | Texte complet |
|---|---|---|---|
| `db-migrations` | SQLite WAL, `write_with_retry`, `immediate_write_session`, migrations 001→032, backup/restore, `fcntl.flock` | `.claude/skills/pitfalls/index/db-migrations.md` | `.claude/skills/pitfalls/references/db-migrations.md` |
| `sync-xtream` | Synchro Xtream, EPG en masse, panne fournisseur (`outage_strikes`), tagging adulte, durée d'épisode | `.claude/skills/pitfalls/index/sync-xtream.md` | `.claude/skills/pitfalls/references/sync-xtream.md` |
| `api-auth` | `route_audit`, scopes, `/metrics`, CSRF `Sec-Fetch-Site`, `/tv-auth`, SSRF, CORS, `/api/metadata/v1`, Pydantic | `.claude/skills/pitfalls/index/api-auth.md` | `.claude/skills/pitfalls/references/api-auth.md` |
| `scrape-enrichment` | Passerelle `/api/scrape/v1`, `scrape_cache`, `single-flight`, scraper manuel, `match_locked`, `count_requests()` | `.claude/skills/pitfalls/index/scrape-enrichment.md` | `.claude/skills/pitfalls/references/scrape-enrichment.md` |
| `ai-media-plex` | IA vectorielle (`fastembed`/`sqlite-vec`), Ollama/LLM/SSE, sous-titres, téléchargements physiques, WebDAV `/dav` | `.claude/skills/pitfalls/index/ai-media-plex.md` | `.claude/skills/pitfalls/references/ai-media-plex.md` |
| `ops-runtime` | Logs Windows, `asyncio.to_thread`, Docker non-root uid 1000, dépendances épinglées | `.claude/skills/pitfalls/index/ops-runtime.md` | `.claude/skills/pitfalls/references/ops-runtime.md` |

## Ajouter un piège

Tout bug résolu ajoute un piège : le texte dans `references/<domaine>.md` (puce `- **Titre** ...`), puis `python .claude/tools/pitfalls-index.py` (l'index est généré, ne pas l'éditer). `claude-doctor` signale un index en retard.
