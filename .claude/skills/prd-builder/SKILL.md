---
name: prd-builder
description: À utiliser pour construire un Product Requirements Document complet du backend PlexHub à partir d'un fichier de vision. Utilisé surtout par l'agent CPO. Déclenché sur « écris le PRD », « spécifie le produit », ou dans le cadre de /feature (phase plan).
---

# PRD builder

Construit `docs/10-prd.md` à partir de `docs/00-vision.md` et `docs/01-intake.md`. Pour un backend,
le « produit » est une **API consommée par des clients** (app Android PlexHubTV, services internes,
outils admin) — les personas sont des consommateurs d'API, les journeys sont des flux d'API.

## Sections requises (dans l'ordre)

1. **Résumé produit** — 2-3 phrases (ce que le backend offre, à qui).

2. **Personas** — 1 à 3 consommateurs d'API, chacun avec : nom, objectif, frustration, contexte
   d'usage. Ex. : *app Android PlexHubTV* (consomme `/api/media`, `/api/ai/rank`), *worker planifié
   interne* (sync/enrichissement), *opérateur admin* (UI HTMX `/admin`).

3. **User journeys (flux d'API)** — 3 à 7 paragraphes en prose. Chaque paragraphe : déclencheur →
   appels d'API enchaînés (méthode + chemin) → état attendu → état de succès. Ex. : « Le client
   demande des recommandations : `POST /api/ai/rank` avec un `tmdb_id` → le service embedding résout,
   sqlite-vec ranke, cache `ai_tmdb_cache` → réponse triée ; au 1ᵉʳ appel, cold start ~30 s attendu. »

4. **Table des features** —

   | ID | Nom | Description | Priorité | Critères d'acceptation |
   |----|-----|-------------|----------|------------------------|

   Utilise F-001, F-002, … La priorité est P0/P1/P2. Pour un backend, une feature est typiquement un
   endpoint/contrat, un flux de worker, ou une garantie non-fonctionnelle (latence, idempotence).

5. **User stories** — pour chaque feature P0, 1 à 5 stories :
   *En tant que [persona consommateur d'API], je veux [appel/comportement] afin que [résultat]*
   plus une acceptation **Given/When/Then** orientée contrat d'API. Exemple :

   > **Given** un `tmdb_id` de type `movie` valide et `AI_API_KEY` configurée
   > **When** le client appelle `POST /api/ai/rank`
   > **Then** la réponse est `200` avec une liste triée par score décroissant, et les épisodes/saisons
   > sont comptés en `resolutionFailed` puis droppés (jamais rankés).

   Couvre les cas d'erreur contractuels : ex. `AI_API_KEY` vide → **503** `AI service not configured`.

6. **Hors scope** — liste explicite (ce que le backend ne fera PAS — ex. « aucun historique
   utilisateur persisté »).

7. **Questions ouvertes** — ce qui nécessite l'input du CEO ou de l'utilisateur.

## Barre de qualité

- Chaque story est implémentable par un backend-developer sans nouvelle conversation.
- Chaque critère d'acceptation est **testable** (un test `pytest` peut l'exercer : service + endpoint
  via `httpx.AsyncClient`/`TestClient`, mocks externes via respx).
- Chaque critère respecte la DoD backend : `pytest -v` vert · boot `uvicorn app.main:app` ·
  `/api/health` 200 · migrations idempotentes · OpenAPI à jour.
- Chaque P0 remonte à un journey, qui remonte à un persona, qui remonte à la vision. Si tu ne peux
  pas tracer une feature jusqu'à la vision, supprime-la.

## Anti-patterns

- Décrire une UI/des écrans — c'est un backend ; décris des contrats d'API et des flux.
- Critères d'acceptation non testables (« doit être rapide » sans budget chiffré).
- Inventer un comportement qui contredit un invariant maison (`CLAUDE.md` §9, ex. contrat 503 IA).
