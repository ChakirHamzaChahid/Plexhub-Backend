---
description: Mesure de perf — latence des endpoints sur scénarios représentatifs, breakdown chiffré, isolation du goulot. Lecture seule (mesure). Délègue à perf-benchmarker.
argument-hint: <scénario/endpoint ciblé, ex. "/api/ai/rank cold + warm" ; sinon chemins chauds par défaut>
allowed-tools: Read, Glob, Grep, Bash, Task, Agent
---

> 🟢 **PlexHub Backend — FastAPI/Python 3.13.** Dév **directement sur `develop`** (pas de branche par tâche ; `main` = release only). Lis `.claude/WORKFLOWS.md` + `CLAUDE.md` §5/§9. Validation = `pytest -v` + boot `uvicorn app.main:app` + `GET /api/health` 200.

# /benchmark — mesurer la latence des endpoints

Cible : $ARGUMENTS  *(scénario/endpoint ; sinon chemins chauds par défaut)*

Délègue à l'agent **`perf-benchmarker`**. **Mesure serveur lancé** — pas de spéculation sans chiffre.

## Phases
1. **Préparer** — boot `uvicorn app.main:app`, `GET /api/health` 200. Note l'env (DATA_DIR, IA configurée ?). Définis les scénarios : `/api/ai/rank` (**cold-start ~30 s** au 1ᵉʳ appel §9, puis warm), `/api/ai/rank-multi`, sync/enrichment, génération Plex, plus les endpoints CRUD chauds.
2. **Mesurer** — latence par endpoint avec `curl -w` (TTFB/total) ; charge avec `ab` ou `locust` (p50/p95/p99, débit). Distingue **cold-start IA** (1ᵉʳ embed) des appels chauds. Croise avec `/metrics` (Prometheus) et les durées loggées dans `logs/plexhub.log`.
3. **Breakdown chiffré** — décompose le temps par étape (réseau TMDB, embedding/ONNX, requête sqlite-vec, I/O DB sous `db_retry`/WAL, sérialisation). **Isole le goulot** dominant, prouvé par mesure `fichier:ligne` + chiffres.
4. **Restituer** — tableau avant (p50/p95/p99 par scénario), goulot identifié, hypothèses de cause, et **liste priorisée (impact/effort)** des correctifs candidats — à exécuter ensuite via **`/fix-bench-perf`**.

## Garde-fous
- **Lecture seule** : pas de modification de code ici (seulement mesure). Les correctifs vont dans `/fix-bench-perf`.
- Mesures **reproductibles** : mêmes inputs, plusieurs runs, warm-up explicite (cold IA noté à part).
- Traçabilité : rapport chiffré dans `docs/daily/<date>.md` ; chiffres datés (HEAD + date).
