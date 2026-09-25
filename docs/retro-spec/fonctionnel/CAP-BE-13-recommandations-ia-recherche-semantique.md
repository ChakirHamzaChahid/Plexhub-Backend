# CAP-BE-13 — Recommandations IA & recherche sémantique

**Reconstituée le** : 2026-09-25 · **HEAD** : `78a384b` · **Sources** : code, tests, `CLAUDE.md` (§2 `embedding_service`/`recommendation_service`, §9 pièges 1 à 5, 11 et 14), `docs/architecture/flux-cles.md` (§5.5, §5.8 partie recherche), app TV (lecture seule, preuves `tv:`). Aucun PRD ne couvre cette capacité (`docs/10-prd-*.md` ne traitent que les téléchargements) : pas de tableau d'écart fiche ↔ PRD ; les écarts sont relevés face à `CLAUDE.md`, à `docs/architecture/flux-cles.md` et aux commentaires du code.
**Périmètre** : `POST /api/ai/rank`, `POST /api/ai/rank-multi`, `POST /api/ai/search`, `POST /api/ai/embed/rebuild`, `GET /api/ai/embed/jobs/{job_id}`, `GET /api/ai/embed/status` (`app/api/ai.py`) ; `app/services/embedding_service.py`, `app/services/recommendation_service.py` (hors `generate_blurb`), `app/workers/embedding_worker.py`, `find_by_imdb_id` de `app/services/tmdb_service.py` ; tables `ai_tmdb_cache` et `ai_embeddings` (migration 008). Les endpoints LLM purs (`/describe`, `/chat`, `/llm/status`, `/assistant`, `/blurb`) relèvent de CAP-BE-14, `/subtitles/translate` de CAP-BE-15 : ils ne sont cités ici que pour la garde partagée. Renvoi : `docs/retro-spec/_genere/inventaire.md` ; tables : `docs/retro-spec/technique/db/`.

## Résumé

Le backend classe des titres par proximité sémantique. Les vecteurs viennent de fastembed : synopsis TMDB + genres, 384 dimensions, normalisés L2. Ils sont stockés dans sqlite-vec et indexés par `tmdb_id`. L'app TV s'en sert pour deux rangées : « Titres similaires » sur la fiche d'un média (`/rank`, un titre de référence) et « Pour toi » sur l'accueil (`/rank-multi`, centroïde pondéré de l'historique récent). Dans les deux cas, c'est la TV qui fournit les candidats, depuis son propre catalogue : le backend ne fait que les ordonner. Il récupère sur TMDB les vecteurs manquants, 20 au plus par appel. Toute panne IA fait retomber la TV en silence sur une source non IA. La recherche en langage naturel (`/search`), les explications LLM (`explain`) et la réindexation (`/embed/rebuild`) existent côté API mais n'ont **aucun appelant TV**. Aucun historique utilisateur n'est conservé : le cache ne contient que des données TMDB. Personas : spectateur TV (bénéficiaire invisible), opérateur (diagnostic, réindexation). **Aucun parcours Maestro** ne cible la capacité.

## User stories observées

- **US-BE-13.1** (P1) — En tant que spectateur TV, je vois sur la fiche d'un film ou d'une série des titres similaires, classés par proximité de sujet, afin de choisir quoi regarder ensuite.
  - Parcours : fiche média → `AiSimilarResolver` → `POST /api/ai/rank` → repli silencieux sur la source non IA en cas d'échec · Maestro : aucun
- **US-BE-13.2** (P1) — En tant que spectateur TV, je vois sur l'accueil des rangées « Pour toi » (films, séries) construites depuis mes derniers visionnages.
  - Parcours : accueil → `ForYouFeedProviderImpl` → `POST /api/ai/rank-multi` · Maestro : aucun
- **US-BE-13.3** (P2) — En tant qu'opérateur, je teste l'URL et la clé du backend IA depuis les réglages TV avant de l'activer.
  - Parcours : Réglages → section IA → « Tester » → `GET /api/ai/embed/status` · Maestro : aucun
- **US-BE-13.4** (P3) — En tant qu'opérateur, je lance une réindexation et j'en suis l'avancement. Aucun écran : appel API direct seulement.
  - Parcours : `POST /api/ai/embed/rebuild` → sondage de `GET /api/ai/embed/jobs/{id}` · Maestro : sans objet
- **US-BE-13.5** (P3) — En tant qu'intégrateur API, je cherche un titre par thème ou ambiance en langage naturel. Aucun appelant TV.
  - Parcours : `POST /api/ai/search` · Maestro : sans objet

## Exigences fonctionnelles

### Garde du routeur

- **FR-BE-611** — Toutes les routes de la capacité sont sous le routeur `/api/ai`, qui applique la dépendance `verify_api_key` à chaque route. — *implémenté* — preuve `app/api/ai.py:52-56` · test `tests/test_ai_rank.py:117`
- **FR-BE-612** — Le garde renvoie 503 « AI vector storage unavailable » quand sqlite-vec n'est pas chargé. Ce contrôle passe **avant** celui de la clé : sans sqlite-vec, un appel sans clé reçoit 503, pas 401. — *implémenté* (comportement constaté) — preuve `app/api/deps.py:76-90` · test : aucun sur l'ordre des deux contrôles
- **FR-BE-613** — La clé `X-API-Key` est acceptée si c'est le secret maître (comparaison en temps constant) ou une clé par utilisateur active. Sinon, ou si `AI_API_KEY` est vide : 401 « Invalid API key », jamais 503. — *implémenté* — preuve `app/api/deps.py:47-61`, `app/api/deps.py:86-90` · test `tests/test_ai_search.py:165`

### Classement item → item (`POST /api/ai/rank`)

- **FR-BE-614** — Requête en camelCase : `ref` (`tmdbId` et/ou `imdbId`, au moins un, sinon 422), `candidates[]` (même format, **sans borne de taille**), `limit` (1 à 200, défaut 20), `mediaType` (`movie` ou `tv`, défaut `movie`), `explain` (défaut `false`). — *implémenté* — preuve `app/api/ai.py:66-88` · test `tests/test_ai_rank.py:117`
- **FR-BE-615** — Chaque référence est résolue en `tmdb_id` : un `tmdbId` est pris tel quel, un `imdbId` seul passe par TMDB `/find`. Les doublons sont éliminés (le premier gagne) et les échecs comptés. La résolution est **séquentielle et non plafonnée** : le plafond de 20 ne s'applique qu'à l'hydratation. Une liste de candidats `imdbId` seuls déclenche donc un appel TMDB par candidat. — *partiel* (manque : aucune borne sur le nombre de résolutions TMDB par appel ; la TV envoie jusqu'à 1000 candidats) — preuve `app/api/ai.py:281-313`, `tv:data/src/main/java/com/chakir/plexhubtv/data/source/AiSimilarResolver.kt:99` · test : aucun sur le volume
- **FR-BE-616** — Référence non résolue : 422 « ref unresolvable ». Référence résolue mais sans vecteur après hydratation : 422 « ref embedding unavailable ». — *implémenté* — preuve `app/api/ai.py:327-332`, `app/api/ai.py:362-367` · test `tests/test_ai_rank.py:211` (premier cas ; second cas : aucun)
- **FR-BE-617** — Les candidats sont ordonnés par cosinus décroissant (produit scalaire de vecteurs normalisés) avec la référence, qui est exclue du résultat, puis tronqués à `limit`. — *implémenté* — preuve `app/api/ai.py:369-380`, `app/services/recommendation_service.py:365-390` · test `tests/test_recommendation_service.py:90`
- **FR-BE-618** — La réponse, en camelCase, contient `ranked[]` (`tmdbId`, `imdbId`, `score`, `explanation`) et les compteurs `cacheHits`, `cacheMisses` (vecteurs hydratés), `cacheMissesDropped` et `resolutionFailed`. — *implémenté* — preuve `app/api/ai.py:109-125`, `app/api/ai.py:388-394` · test `tests/test_ai_rank.py:231`

### Classement multi-références (`POST /api/ai/rank-multi`)

- **FR-BE-619** — Requête : `refs[]`, `candidates[]`, `limit` (1 à 200), `mediaType`, `excludeRefs` (défaut `true`), `explain`. `refs` vide : 422 « refs cannot be empty ». Aucune référence résolue : 422 « no refs resolved ». — *implémenté* — preuve `app/api/ai.py:91-106`, `app/api/ai.py:414-425` · test `tests/test_ai_rank_multi.py:234`
- **FR-BE-620** — Avec `excludeRefs=true`, les références sont retirées des candidats **avant** le classement, pour que `limit` reste exact. — *implémenté* — preuve `app/api/ai.py:432-437` · test `tests/test_ai_rank_multi.py:156`, `tests/test_ai_rank_multi.py:183`
- **FR-BE-621** — Le vecteur de requête est le centroïde des références disponibles, pondéré puis renormalisé L2. Aucune référence avec vecteur : 422 « no ref embeddings available ». — *implémenté* — preuve `app/api/ai.py:459-468`, `app/services/embedding_service.py:98-107` · test `tests/test_ai_rank_multi.py:209` (cas 422 : aucun)
- **FR-BE-622** — Réponse au même contrat que `/rank`. — *implémenté* — preuve `app/api/ai.py:401`, `app/api/ai.py:486-492` · test `tests/test_ai_rank_multi.py:156`

### Explications « pourquoi recommandé » (`explain`)

- **FR-BE-623** — Avec `explain=true`, une phrase d'explication en français est générée par Ollama pour les 5 premiers résultats, avec 4 appels simultanés au plus et 20 s par appel. Un échec laisse `explanation=null`, sans jamais de 503. — *implémenté* — preuve `app/api/ai.py:189-197`, `app/api/ai.py:220-274`, `app/api/ai.py:382-386` · test `tests/test_ai_explain.py:137`, `tests/test_ai_explain.py:179`
- **FR-BE-624** — Le prompt cite les titres lus dans `ai_tmdb_cache.title`. Aucun écrivain ne remplit cette colonne (FR-BE-632) : le LLM reçoit donc `tmdb:<id>` pour le candidat et « le(s) titre(s) de référence » pour la référence. — *partiel* (manque : les titres réels ; les explications portent sur des identifiants) — preuve `app/api/ai.py:241-258`, `app/services/recommendation_service.py:129-131` · test : aucun avec un cache peuplé par le code de production
- **FR-BE-625** — La TV n'envoie jamais `explain` : ses DTO de requête n'ont pas ce champ. — *implémenté* (aucun appelant TV pour l'option) — preuve `tv:core/network/src/main/java/com/chakir/plexhubtv/core/network/ai/dto/RankRequestDto.kt:19-20`, `tv:core/network/src/main/java/com/chakir/plexhubtv/core/network/ai/dto/RankMultiRequestDto.kt:27-32`

### Cache et hydratation des vecteurs

- **FR-BE-626** — Les vecteurs connus sont lus en une seule requête paramétrée `IN (...)` sur `ai_embeddings`. — *implémenté* — preuve `app/services/recommendation_service.py:65-80` · test `tests/test_recommendation_service.py:72`
- **FR-BE-627** — Seuls les 20 premiers absents sont hydratés, en parallèle, avec 10 s maximum par tâche. Le reste, les dépassements de délai et les échecs TMDB sont comptés dans `cacheMissesDropped`, sans erreur. — *implémenté* — preuve `app/services/recommendation_service.py:31-32`, `app/services/recommendation_service.py:161-207` · test `tests/test_recommendation_service.py:109`, `tests/test_recommendation_service.py:131`
- **FR-BE-628** — Hydratation d'un titre : détails TMDB, film ou série selon `mediaType`. Le texte vectorisé est `synopsis + genres` ; un titre sans synopsis ni genres n'est pas vectorisé. — *implémenté* — preuve `app/services/recommendation_service.py:98-122` · test `tests/test_recommendation_service.py:109`
- **FR-BE-629** — Chaque titre hydraté est écrit dans une session à lui : upsert de `ai_tmdb_cache` avec `embedded_at` renseigné, puis DELETE + INSERT sur `ai_embeddings` (vec0 refuse l'upsert). — *implémenté* — preuve `app/services/recommendation_service.py:124-156`
- **FR-BE-630** — Si le modèle d'embedding est indisponible pendant l'hydratation, `EmbeddingUnavailableError` interrompt tout l'appel : 503 « AI model unavailable » sur `/rank` et `/rank-multi`. — *implémenté* — preuve `app/services/recommendation_service.py:192-195`, `app/api/ai.py:351-356`, `app/api/ai.py:450-455` · test `tests/test_recommendation_service.py:156`
- **FR-BE-631** — Le cache est indexé par `tmdb_id` seul, sans `media_type`, et la lecture des vecteurs ignore `mediaType`. Or les identifiants TMDB des films et des séries sont deux espaces distincts : un film et une série de même numéro partagent la même entrée, et le type stocké est écrasé par le dernier upsert. — *partiel* (manque : clé composite ou contrôle du type à la lecture) — preuve `app/db/migrations.py:320-335`, `app/services/recommendation_service.py:78`, `app/services/recommendation_service.py:132-133` · test : aucun
- **FR-BE-632** — La colonne `ai_tmdb_cache.title` est écrite à `NULL` en dur par l'hydratation, et aucun autre code de production ne la remplit. Seuls les tests la peuplent. — *partiel* (manque : le titre, pourtant renvoyé par TMDB) — preuve `app/services/recommendation_service.py:129-131` · test `tests/test_ai_search.py:47` (peuplement manuel)
- **FR-BE-633** — Un vecteur en cache n'expire jamais : le cache n'a ni TTL ni rafraîchissement. Un synopsis corrigé côté TMDB ou un changement de modèle laisse l'ancien vecteur en place. — *implémenté* (comportement constaté) — preuve `app/services/recommendation_service.py:65-80` (aucun filtre d'âge), `app/db/migrations.py:326-335`
- **FR-BE-634** — Pour un `imdbId`, TMDB `/find` ne retient que `movie_results` ou `tv_results`, selon le type demandé. Les épisodes, saisons et personnes sont ignorés et comptés dans `resolutionFailed`. — *implémenté* — preuve `app/services/tmdb_service.py:486-532` · test : aucun par la route

### Modèle d'embedding

- **FR-BE-635** — Modèle par défaut : `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`, 384 dimensions, remplaçable par `AI_EMBED_MODEL`. Les poids se cachent dans `AI_EMBED_CACHE_DIR` s'il est défini. — *implémenté* — preuve `app/services/embedding_service.py:26-36`, `app/services/embedding_service.py:50-62` · test `tests/test_embedding_service.py:56`
- **FR-BE-636** — Le modèle est chargé au premier appel, jamais au démarrage. C'est un singleton protégé par un verrou ; le chargement et l'inférence tournent hors de la boucle d'événements (`asyncio.to_thread`). Le code annonce un démarrage à froid d'environ 30 s, sans le mesurer. Un échec de chargement lève `EmbeddingUnavailableError`. — *implémenté* — preuve `app/services/embedding_service.py:11`, `app/services/embedding_service.py:65-82` · test `tests/test_embedding_service.py:98`, `tests/test_embedding_service.py:119`
- **FR-BE-637** — Tous les vecteurs produits (passages, requêtes, centroïdes) sont normalisés L2. — *implémenté* — preuve `app/services/embedding_service.py:85-116`
- **FR-BE-638** — Rien ne vérifie la cohérence entre le modèle choisi et la table `vec0 FLOAT[384]`. Un `AI_EMBED_MODEL` d'une autre dimension, ou un changement de modèle, n'invalide pas les vecteurs existants. — *partiel* (manque : contrôle de dimension et réindexation au changement de modèle) — preuve `app/services/embedding_service.py:30-36`, `app/db/migrations.py:320-323` · test : aucun

### Recherche sémantique (`POST /api/ai/search`)

- **FR-BE-639** — Requête : `query` (au moins 1 caractère), `mediaType` optionnel, `limit` (1 à 50, défaut 20). — *implémenté* — preuve `app/api/ai.py:128-135` · test `tests/test_ai_search.py:174`
- **FR-BE-640** — La requête est d'abord reformulée par Ollama, si possible, en 15 s maximum. En cas d'échec ou de réponse vide, la requête brute est utilisée, sans jamais de 503. `queryUsed` renvoie le texte effectivement vectorisé. — *implémenté* — preuve `app/api/ai.py:520-539` · test `tests/test_ai_search.py:249`
- **FR-BE-641** — La requête est vectorisée ; si le modèle est indisponible : 503 « AI model unavailable ». — *implémenté* — preuve `app/api/ai.py:541-554`
- **FR-BE-642** — Recherche KNN native sqlite-vec (`embedding MATCH :vec AND k = :k`), score = `1 - distance²/2`. Le filtre `mediaType` s'applique **après** le KNN : on demande `min(limit×4, 200)` voisins, puis 2000 si le filtre laisse moins de `limit` résultats et que la page était pleine. Deux requêtes au plus. — *implémenté* — preuve `app/services/recommendation_service.py:233-310` · test `tests/test_recommendation_service.py:175`
- **FR-BE-643** — La réponse contient `results[]` (`tmdbId`, `title`, `mediaType`, `score`), `queryUsed` et `model`. `title` vaut toujours `null` en production (FR-BE-632). `model` renvoie la constante par défaut, et non le modèle effectivement chargé si `AI_EMBED_MODEL` est surchargé (`/embed/status` renvoie, lui, le nom résolu). — *partiel* (manque : titre réel ; `model` faux en cas de surcharge) — preuve `app/api/ai.py:565-578`, `app/api/ai.py:783` · test `tests/test_ai_search.py:290`
- **FR-BE-644** — Aucun appelant TV. — *implémenté* (sans consommateur) — preuve : aucune occurrence de `api/ai/search` dans le dépôt TV ; l'interface `tv:core/network/src/main/java/com/chakir/plexhubtv/core/network/ai/AiRecommendationApi.kt:26` n'expose que rank, rank-multi et embed/status

### Réindexation (`/embed/rebuild`, `/embed/jobs/{id}`)

- **FR-BE-645** — `POST /embed/rebuild` crée un job `ai_rebuild_<ms>` et le lance en tâche de fond. Réponse : 202 + `jobId`. Le job ne démarre jamais tout seul au boot. — *implémenté* — preuve `app/api/ai.py:713-726`, `app/workers/embedding_worker.py:136-150` · test `tests/test_ai_jobs.py:109`
- **FR-BE-646** — Le job parcourt `ai_tmdb_cache WHERE embedded_at IS NULL` par pages de 50 (curseur `tmdb_id > :cursor`, sans `OFFSET`). Pour chaque ligne : DELETE + INSERT dans `ai_embeddings`, puis mise à jour de `embedded_at`. — *implémenté* — preuve `app/workers/embedding_worker.py:71-126` · test `tests/test_embedding_worker.py:113`
- **FR-BE-647** — En usage normal, le job n'a rien à traiter : le seul écrivain de `ai_tmdb_cache` (l'hydratation) renseigne toujours `embedded_at`. Il ne reprend pas non plus les vecteurs existants après un changement de modèle. — *partiel* (manque : une source de lignes `embedded_at IS NULL` ou une réindexation complète) — preuve `app/services/recommendation_service.py:129-135`, `app/workers/embedding_worker.py:80` · test : aucun
- **FR-BE-648** — Si le modèle est indisponible, tout le job passe à `failed`. Toute autre erreur sur une ligne est comptée dans `errors` et le parcours continue. — *implémenté* — preuve `app/workers/embedding_worker.py:96-109` · test `tests/test_embedding_worker.py:141`
- **FR-BE-649** — Les jobs sont gardés en mémoire du processus (100 au plus, le plus ancien sorti en premier). `GET /embed/jobs/{id}` renvoie `status`, `processed`, `errors`, `lastError`, `startedAt` et `finishedAt`, ou 404 « job not found ». Un autre processus ne voit pas ces jobs, et plusieurs rebuilds peuvent tourner en même temps (aucun verrou d'exécution unique). — *implémenté* (limites constatées) — preuve `app/workers/embedding_worker.py:22-54`, `app/api/ai.py:729-738` · test `tests/test_embedding_worker.py:59`, `tests/test_ai_jobs.py:128`
- **FR-BE-650** — Aucun appelant TV, aucun écran d'administration. — *implémenté* (sans consommateur) — preuve : aucune occurrence de `embed/rebuild` dans le dépôt TV ; l'interface `tv:core/network/src/main/java/com/chakir/plexhubtv/core/network/ai/AiRecommendationApi.kt:26` ne l'expose pas

### Diagnostic (`GET /api/ai/embed/status`)

- **FR-BE-651** — Le diagnostic renvoie `totalEmbeddings`, `totalCacheEntries`, `pendingEmbed`, `lastIndexedAt`, `rssMb` (entier), `modelLoaded`, `modelName` (nom résolu), `embeddingDim`, `vecLoaded` et `vecError`. Il ne charge pas le modèle. — *implémenté* — preuve `app/api/ai.py:171-183`, `app/api/ai.py:745-787` · test `tests/test_ai_status.py:88`, `tests/test_ai_status.py:132`

### Usage par l'app TV

- **FR-BE-652** — « Titres similaires » : la TV appelle `/rank` avec jusqu'à 1000 candidats tirés de son catalogue local. Elle garde les résultats en cache mémoire 10 minutes, 50 entrées au plus, avec une clé incluant une empreinte des candidats. — *implémenté* — preuve `tv:data/src/main/java/com/chakir/plexhubtv/data/source/AiSimilarResolver.kt:41-99`, `tv:data/src/main/java/com/chakir/plexhubtv/data/repository/AiRecommendationRepositoryImpl.kt:76-114`
- **FR-BE-653** — « Pour toi » : la TV appelle `/rank-multi` avec ses derniers visionnages comme références et jusqu'à 1000 candidats, sans cache côté client. — *implémenté* — preuve `tv:data/src/main/java/com/chakir/plexhubtv/data/source/ForYouFeedProviderImpl.kt:50-116`
- **FR-BE-654** — Réglages : le test de connexion appelle `/embed/status`, et la TV n'en lit qu'une partie des champs. — *implémenté* — preuve `tv:data/src/main/java/com/chakir/plexhubtv/data/repository/AiRecommendationRepositoryImpl.kt:142-163`
- **FR-BE-655** — Tout échec IA (401, 503, 422, délai, liste vide) fait retomber la TV en silence sur la source non IA. Le client HTTP IA a des délais courts (connexion 5 s, lecture 15 s), inférieurs au démarrage à froid de ~30 s annoncé côté backend. — *implémenté* — preuve `tv:domain/src/main/java/com/chakir/plexhubtv/domain/usecase/GetSimilarMediaWithAiFallbackUseCase.kt:29-52`, `tv:core/network/src/main/java/com/chakir/plexhubtv/core/network/ai/AiNetworkModule.kt:54-55`

## Règles métier

- **RG-BE-13.1** — Au plus 20 hydratations TMDB par appel, 10 s par tâche ; le surplus est compté, jamais traité. — preuve `app/services/recommendation_service.py:31-32`
- **RG-BE-13.2** — Poids du centroïde : `max(0.1, 1.0 − 0.1 × i)` pour la i-ème référence disponible (1.0, 0.9, … puis 0.1 à partir de la 10ᵉ). L'ordre des `refs` envoyé par le client compte. — preuve `app/api/ai.py:467`
- **RG-BE-13.3** — Bornes de `limit` : 1 à 200 pour `/rank` et `/rank-multi`, 1 à 50 pour `/search`. `mediaType` ∈ {`movie`, `tv`}. — preuve `app/api/ai.py:86-87`, `app/api/ai.py:103-104`, `app/api/ai.py:134-135`
- **RG-BE-13.4** — Sur-échantillonnage du KNN filtré : facteur 4, paliers 200 puis 2000 (plafond dur) ; au-delà, on accepte de rendre moins de résultats. — preuve `app/services/recommendation_service.py:233-234`
- **RG-BE-13.5** — Explications : 5 au plus par appel, 4 appels simultanés, 20 s par appel. — preuve `app/api/ai.py:191-197`
- **RG-BE-13.6** — Réindexation : pages de 50, 100 jobs mémorisés au plus. — preuve `app/workers/embedding_worker.py:22-23`
- **RG-BE-13.7** — Texte vectorisé : synopsis TMDB + genres, séparés par un saut de ligne, sans préfixe de type E5. — preuve `app/services/recommendation_service.py:112-117`, `app/services/embedding_service.py:114`

## États, erreurs et cas limites

| Situation | Comportement observé | Preuve |
|---|---|---|
| sqlite-vec non chargé | 503 « AI vector storage unavailable » sur toute route `/api/ai`, avant le contrôle de clé | `app/api/deps.py:81-85` |
| Clé absente ou invalide | 401 « Invalid API key » | `app/api/deps.py:86-90` |
| Modèle d'embedding indisponible | 503 « AI model unavailable » (`/rank`, `/rank-multi`, `/search`) ; job de réindexation en `failed` | `app/api/ai.py:351-356`, `app/api/ai.py:549-554`, `app/workers/embedding_worker.py:98-104` |
| `ref` sans `tmdbId` ni `imdbId` | 422 (validation) | `app/api/ai.py:74-78` |
| `ref` non résolue / sans vecteur | 422 « ref unresolvable » / « ref embedding unavailable » | `app/api/ai.py:327-332`, `app/api/ai.py:362-367` |
| `refs` vide / non résolues / sans vecteur | 422 « refs cannot be empty » / « no refs resolved » / « no ref embeddings available » | `app/api/ai.py:414-425`, `app/api/ai.py:461-465` |
| Plus de 20 vecteurs manquants | 20 hydratés, le reste compté dans `cacheMissesDropped` ; le client doit rappeler pour compléter | `app/services/recommendation_service.py:175-176` |
| Candidat sans synopsis ni genres | jamais vectorisé, donc jamais classé (compté comme abandonné) | `app/services/recommendation_service.py:112-115` |
| Ollama injoignable (`explain`, reformulation) | `explanation=null` ; requête brute, 200 | `app/api/ai.py:267-272`, `app/api/ai.py:537-539` |
| `/search` sur un cache vide | `results` vide, 200 | `app/services/recommendation_service.py:279-280` |
| `/search` filtré sur un type minoritaire | jusqu'à 2000 voisins lus ; en deçà, moins de `limit` résultats | `app/services/recommendation_service.py:305` |
| Job inconnu (ou lancé sur un autre processus) | 404 « job not found » | `app/api/ai.py:735-737` |
| Panne IA vue de la TV | repli silencieux, aucune erreur affichée | `tv:domain/src/main/java/com/chakir/plexhubtv/domain/usecase/GetSimilarMediaWithAiFallbackUseCase.kt:40-51` |

## Dépendances

- Autres capacités : `CAP-BE-02` (clés API, garde `verify_api_key`) ; `CAP-BE-06` (même client TMDB, mais tables IA séparées de `media`) ; `CAP-BE-09` (aucune donnée partagée : les candidats viennent du catalogue **local de la TV**, pas de `media`) ; `CAP-BE-14` (Ollama pour `explain` et la reformulation ; `/assistant` réutilise `semantic_search_with_overview`) ; `CAP-BE-15` (garde partagée seulement).
- Autre dépôt : endpoints `backend:POST /api/ai/rank`, `backend:POST /api/ai/rank-multi`, `backend:GET /api/ai/embed/status` ← appels `tv:core/network/src/main/java/com/chakir/plexhubtv/core/network/ai/AiRecommendationApi.kt:26`.
- Données : `ai_tmdb_cache` (clé `tmdb_id`) et `ai_embeddings` (vec0 `FLOAT[384]`), migration 008 ; elle exige sqlite-vec chargé sur la connexion (`app/db/migrations.py:299-344`) — voir `docs/retro-spec/technique/db/`.

## Écarts relevés

| Écart | Nature | Suite proposée |
|---|---|---|
| docs/architecture/flux-cles.md §5.8 décrit un sur-échantillonnage fixe `min(limit*4,200)` ; le code monte à 2000 (CR-P08) | doc ↔ code | Mettre à jour flux-cles §5.8 |
| docs/architecture/flux-cles.md §5.5 cite ai.py:716, ai.py:732 et ai.py:748 pour rebuild, jobs et status ; les routes sont à `app/api/ai.py:713`, `app/api/ai.py:729` et `app/api/ai.py:745` | doc périmée (lignes) | Recaler au prochain `/sync-context` |
| Le docstring d'`embedding_service` affirme que `verify_api_key` attrape `EmbeddingUnavailableError` pour renvoyer 503 ; en réalité ce sont les endpoints, et le garde ne contrôle que sqlite-vec | commentaire faux | Corriger `app/services/embedding_service.py:13-14` |
| La colonne `ai_tmdb_cache.title` n'est jamais remplie : `/search` renvoie `title: null` et les explications citent `tmdb:<id>` | code partiel · tests trompeurs (titres injectés à la main) | Écrire `data.title` à l'hydratation, ou retirer le champ — à trancher par Chakir |
| Cache indexé par `tmdb_id` sans type : collision film/série de même numéro | défaut latent · test manquant | Clé `(tmdb_id, media_type)` (migration) ou contrôle du type — à trancher |
| La réindexation ne traite que `embedded_at IS NULL`, que rien ne produit ; aucune réindexation complète après un changement de modèle (CLAUDE.md §9 piège 5 la présente comme l'outil de reconstruction) | fonctionnalité quasi morte | Ajouter un mode « tout réindexer » ou documenter l'usage réel |
| Résolution `imdbId` → TMDB sans plafond, alors que la TV envoie jusqu'à 1000 candidats ; le plafond de 20 (CLAUDE.md §9 piège 4) ne couvre que l'hydratation | spec ↔ code · risque de latence | Plafonner ou paralléliser la résolution ; mesurer |
| `/search` renvoie `model` = constante par défaut, `/embed/status` le nom résolu | incohérence de contrat | Utiliser `_resolve_model_name()` |
| Délais TV (lecture 15 s) inférieurs au démarrage à froid annoncé (~30 s) | risque d'intégration non testé | Préchauffage du modèle, ou délai plus long pour le premier appel |
| Aucun test pour : l'ordre 503/401 du garde, « ref embedding unavailable », « no ref embeddings available », le filtrage épisode/personne de `/find` par la route | test manquant | Ajouter aux tests `tests/test_ai_rank.py` / `tests/test_ai_rank_multi.py` |
| Aucune métrique métier IA (abandons, latence, avancement de la réindexation) : seule l'instrumentation HTTP générique couvre ces routes | observabilité | À décider selon le besoin opérationnel |

## Questions ouvertes

- Quelle part des candidats envoyés par la TV n'a qu'un `imdbId` ? Autrement dit, combien d'appels TMDB `/find` séquentiels un `/rank` à froid déclenche-t-il, et tient-il dans les 15 s de lecture côté TV ? — à vérifier par test.
- Le premier `/rank` après un redémarrage du backend (chargement du modèle) dépasse-t-il le délai TV, ce qui ferait retomber la TV sur la source non IA ? — à vérifier par test.
- La collision film/série sur `tmdb_id` se produit-elle dans le catalogue réel, et avec quelle fréquence ? — à vérifier par test (requête sur une base de production anonymisée).
- Une base antérieure contient-elle encore des lignes `embedded_at IS NULL`, ce qui justifierait la réindexation actuelle ? — à vérifier par test (`pendingEmbed` de `/embed/status` sur l'instance du homelab).
- Deux `POST /embed/rebuild` dans la même milliseconde partagent le même `jobId` et donc la même entrée de suivi : est-ce acceptable ? — à vérifier par test.
