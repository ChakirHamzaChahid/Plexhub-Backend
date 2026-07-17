---
description: Liste les agents du pod backend et le rôle de chacun (roster lecture seule)
allowed-tools: Read, Glob
---

> 🟢 **PlexHub Backend — FastAPI/Python 3.13.** Autorité = `CLAUDE.md` §7 (agents & ownership). Dév directement sur `develop` (pas de branche par tâche ; `main` = release only).

# /app-team — Roster

Imprime l'équipe dans cet ordre, chaque ligne au format `<rôle> — <charte en une ligne>` :

**Exec**
- ceo — vision, métriques de succès, périmètre
- cpo — PRD, user stories, backlog produit
- cto — architecture, stack, principes d'ingénierie (part des défauts du House KB)

**Management**
- tech-lead — spec d'implémentation, patterns, senior hands-on
- tech-manager — sprint plan, board, rapport quotidien, gate de lot, standups, coordination du pod

**Build**
- backend-developer — implémentation FastAPI : endpoints, services, workers, modèles, migrations, tests pytest (plusieurs en parallèle sur périmètres disjoints)
- db-migration-specialist — schéma SQLite, chaîne de migrations idempotentes (001→N), entités ORM
- sync-specialist — sync Xtream, enrichissement TMDB, validation de flux
- ai-recsys-specialist — embeddings fastembed, ranking, sqlite-vec, motifs 503
- plex-generator-specialist — génération NFO/arborescence/.strm, import NFO
- qa-engineer — plans de test, exécution, filing de bugs, sign-off ship

**Qualité & gates**
- code-reviewer — gate sur chaque lot de commits `develop` (verdict APPROVED / REQUEST CHANGES)
- security-reviewer — passe sécurité (auth fail-closed, secrets, CORS, SSRF, Fernet) avant release / sur surface sensible
- integration-agent — cohérence transverse modules ↔ OpenAPI ↔ migrations ↔ entités
- full-auditor — audit 360° indépendant lecture seule (`/audit-full`) + mode incrémental (`/wf-audit-incremental`)

**Ops & Release**
- devops-engineer — stratégie git, CI GitHub Actions, Dockerfile/compose, hygiène secrets
- release-manager — bump `APP_VERSION`, merge develop→main, tag `vX.Y.Z`, image GHCR, notes de release
- perf-benchmarker — latence API mesurée étape par étape (`/metrics`, logs `request_id`), goulots `fichier:ligne`
- observability-analyst — métriques Prometheus, logs, santé des jobs planifiés

**Audit & contexte**
- cleanroom-auditor — audit clean-room à blanc (findings `CR-*`, anti-ancrage)
- cleanroom-fixer — remédiation des findings `CR-*` P0→dette, patch minimal + test de garde
- a0-cartographer — re-cartographie `CLAUDE.md` + `docs/architecture/ARCHITECTURE.md` (`/refresh-context`)

Termine en rappelant que la taille et les rôles se règlent par projet via `tech-manager`, et que tout agent de build lit le House KB (`.claude/knowledge/` : `python-conventions`, `api-conventions`, `stack-defaults`, `git-workflow`, `observability`) via le skill `house-conventions` avant de travailler.
