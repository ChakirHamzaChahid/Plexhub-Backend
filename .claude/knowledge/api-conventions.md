# API Conventions — PlexHub Backend

## Forme des endpoints
- REST sous **`/api`** (un router par domaine). UI admin = HTML/HTMX **sans** préfixe. Router IA = préfixe propre **`/api/ai`** (monté après les autres `/api`).
- Verbe HTTP cohérent : `GET` (lecture, idempotent), `POST` (action/création), etc. Chemins en kebab/snake stables — un changement de forme publique = annoncé (le client Android `PlexHubTV` consomme).
- Réponses **Pydantic v2** typées ; statut explicite. Travaux longs → `202 Accepted` + `jobId` à poller (modèle `embed/rebuild` → `embed/jobs/{id}`).

## Auth
- En-tête **`X-API-Key`** (dépendance `deps.py`). Endpoints IA exigent `AI_API_KEY` configurée, sinon **503 `AI service not configured`**.

## Codes d'erreur
- `400` validation, `401/403` auth, `404` introuvable, `409` conflit, `422` Pydantic, `429` rate-limit, `503` dépendance indisponible.
- **503 IA (contractuels, ne pas modifier le `detail`)** : `AI service not configured` · `AI vector storage unavailable` · `AI model unavailable`.

## Contrat & idempotence
- Toute API publique reste documentée (OpenAPI auto FastAPI + `docs/40-api.md` si présent). `GET /docs` et `/openapi.json` exposent le contrat.
- Endpoints d'action idempotents quand ça a du sens (auto-provision, rebuild = scan + DELETE-puis-INSERT). Bornes explicites (cap 20 fetches TMDB/`/rank`).

## CORS & sécurité
- `CORS_ORIGINS` **explicite** en façade publique (pas `*`). GZip activé (>1000 o). Jamais renvoyer un token/secret dans une réponse ou un message d'erreur.
