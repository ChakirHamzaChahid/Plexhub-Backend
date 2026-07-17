---
description: Démarre un NOUVEAU projet de service backend — intake des requirements, vision CEO, puis PRD + architecture en parallèle, bootstrap CLAUDE.md + git
argument-hint: [idée en une ligne, optionnel]
allowed-tools: Read, Write, Edit, Glob, Grep, Bash, Task, Agent
---

> 🟢 **PlexHub Backend — FastAPI/Python 3.13.** ⚠️ Ce repo EXISTE déjà : `/app-init` ne sert que pour un **nouveau service** (greenfield). Pour ce repo, utiliser `/app-onboard` (léger) ou `/refresh-context` (natif, complet).

# /app-init — Lancer un nouveau projet backend

Tu démarres un projet de service backend neuf. L'idée en une ligne de l'utilisateur (si fournie) :

> $ARGUMENTS

## Étapes

1. **Lance le skill `requirements-intake`** pour obtenir un `docs/01-intake.md` propre. Si le prompt utilisateur est déjà détaillé, ne pose que les questions restées sans réponse.

2. **Spawn l'agent `ceo`** avec l'intake en entrée. Le CEO écrit `docs/00-vision.md`.

3. **Spawn `cpo` et `cto` en parallèle** dans un seul message — les deux lisent `docs/00-vision.md` et produisent leurs docs respectifs (PRD/backlog et architecture/principes — stack par défaut = House KB `stack-defaults.md` : FastAPI async + SQLAlchemy[asyncio] + SQLite WAL + httpx + Pydantic v2 + pytest).

4. **Spawn `tech-lead` et `devops-engineer` en parallèle** dans un seul message :
   - `tech-lead` lit architecture + PRD, écrit `docs/22-impl-spec-backend.md` (frontières de modules `api/`/`services/`/`workers/`/`db/`, contrats Pydantic, conventions d'erreurs).
   - `devops-engineer` lit l'architecture, écrit `docs/23-git-strategy.md`, un `.gitignore` Python, et le workflow CI (pytest + ruff + build image Docker) — seedé depuis le House KB `git-workflow.md`.

5. **Bootstrap du projet.** Génère, seedé depuis le House KB (`.claude/knowledge/`) et les docs produits :
   - le **`CLAUDE.md`** du projet cible — stack choisie, commandes build/run/test (`uvicorn`, `pytest -v`), règles de travail de l'équipe, convention de commit, et noms canoniques (pour que les agents ne devinent jamais) ;
   - le squelette `app/` (main.py + config.py + db/ + api/health) qui boote et répond `GET /api/health` 200 ;
   - un stub `docs/52-observability.md` si la télémétrie est dans le périmètre.

6. **Imprime un résumé** : chaque doc produit avec une description en une ligne, et la commande suivante suggérée (`/app-run` pour le flux quasi-autonome, ou `/app-plan` → `/app-build` pour le contrôle manuel).

## Contrat de sortie

Après cette commande, le workspace contient :

```
CLAUDE.md                    (conventions projet, seedé du House KB)
.gitignore
app/                         (squelette FastAPI qui boote)
docs/
  00-vision.md
  01-intake.md
  10-prd.md
  11-backlog.md
  20-architecture.md
  21-engineering-principles.md
  22-impl-spec-backend.md
  23-git-strategy.md
  52-observability.md        (si télémétrie en périmètre)
```

Si quelque chose manque, nomme-le explicitement dans le résumé avec la raison.
