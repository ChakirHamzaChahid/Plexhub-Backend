# CAP-BE-14 — Génération LLM & assistant

**Reconstituée le** : 2026-09-25 · recalée le 2026-10-02 (Lot 10) · **HEAD** : `aa3270f` · **Sources** : code, tests, `CLAUDE.md` (§2 `ollama_service`, §3 Erreurs, §9 pièges 2, 12, 13, 14), `docs/architecture/flux-cles.md` (§5.7, §5.8), `.env.example`, app TV (lecture seule, preuves `tv:`). Aucun PRD ne couvre cette capacité (`docs/10-prd-*.md` ne traitent que les téléchargements) : pas de tableau d'écart fiche ↔ PRD ; les écarts sont face à `CLAUDE.md`, à `docs/architecture/flux-cles.md` et aux commentaires du code.
**Périmètre** : six routes du routeur `/api/ai` (`app/api/ai.py`) — `POST /describe`, `POST /chat` (réponse unique ou flux SSE), `GET /llm/status`, `POST /assistant`, `POST /blurb`, `GET /blurb/{tmdb_id}` — ; le client `app/services/ollama_service.py` ; `generate_blurb` dans `app/services/recommendation_service.py` ; la table `ai_media_blurb` (migration 012) ; les réglages `OLLAMA_URL` et `OLLAMA_MODEL`. Renvoi : `docs/retro-spec/_genere/inventaire.md`. La table est décrite dans `docs/retro-spec/technique/db/`. Hors périmètre : la recherche sémantique, les explications `explain` de `/rank` et la reformulation de `/search` (CAP-BE-13, même client Ollama), la traduction de sous-titres (CAP-BE-15).

## Résumé

Le backend expose un LLM auto-hébergé (Ollama, modèle `gemma4:e4b` par défaut) sous six routes authentifiées par `X-API-Key` : une présentation courte d'un titre (`/describe`), un chat libre en réponse unique ou en flux SSE (`/chat`), une sonde de disponibilité (`/llm/status`), un assistant qui répond à partir des titres du catalogue IA (`/assistant`, RAG sur la recherche sémantique de CAP-BE-13) et un synopsis français avec étiquettes d'ambiance mis en cache sans expiration (`/blurb`). Toute panne d'Ollama donne un 503 distinct de celui de fastembed, sauf en flux SSE où une sentinelle `[ERROR]` remplace le code HTTP. **Aucun client n'appelle ces routes** : l'app TV ne les déclare pas, et aucun parcours Maestro ne les couvre. Persona effectif : opérateur ou script porteur d'une clé. `describe`, `chat` et `llm/status` n'ont **aucun test**.

## User stories observées

- **US-BE-14.1** (P3) — En tant qu'opérateur, je vérifie que le LLM est joignable et que le modèle configuré est installé, afin de diagnostiquer les 503 des autres routes IA.
  - Parcours : appel direct à `GET /api/ai/llm/status` · Maestro : sans objet
- **US-BE-14.2** (P3) — En tant que client API, j'obtiens une présentation de deux ou trois phrases, en français ou en anglais, pour donner envie de regarder un titre.
  - Parcours : appel direct à `POST /api/ai/describe` · Maestro : aucun
- **US-BE-14.3** (P3) — En tant que client API, je dialogue librement avec le LLM, en réponse complète ou en flux progressif.
  - Parcours : appel direct à `POST /api/ai/chat` · Maestro : aucun
- **US-BE-14.4** (P2) — En tant qu'utilisateur d'un futur client, je pose une question en langage naturel et l'assistant me répond en ne citant que des titres du catalogue, avec la liste des sources.
  - Parcours : appel direct à `POST /api/ai/assistant` · Maestro : aucun
- **US-BE-14.5** (P3) — En tant que client API, je récupère un synopsis court et des étiquettes d'ambiance pour un titre TMDB, générés une fois puis relus depuis le cache.
  - Parcours : `POST /api/ai/blurb` puis `GET /api/ai/blurb/{tmdb_id}` · Maestro : aucun

## Exigences fonctionnelles

### Routes, authentification et gardes

- **FR-BE-656** — Le système expose six routes LLM sur le routeur `/api/ai` : `POST /describe`, `POST /chat`, `GET /llm/status`, `POST /assistant`, `POST /blurb`, `GET /blurb/{tmdb_id}`. Les réponses sont en camelCase. — *implémenté* — preuve `app/api/ai.py:52-56`, `app/api/ai.py:622`, `app/api/ai.py:860`, `app/api/ai.py:883`, `app/api/ai.py:909`, `app/api/ai.py:1091`, `app/api/ai.py:1214-1218` · test `tests/test_ai_assistant.py:325`
- **FR-BE-657** — Chaque route exige `X-API-Key` (secret maître ou clé par utilisateur active). Clé absente ou invalide : 401 « Invalid API key ». — *implémenté* — preuve `app/api/deps.py:86-90` · test `tests/test_ai_assistant.py:200`, `tests/test_ai_blurb.py:357` (clé absente seulement) ; `describe`, `chat`, `llm/status` : aucun
- **FR-BE-658** — Le garde du routeur renvoie 503 « AI vector storage unavailable » quand sqlite-vec n'est pas chargé, **avant** le contrôle de clé. Cela vaut pour `describe`, `chat` et `llm/status`, qui n'utilisent aucun vecteur : la sonde de disponibilité du LLM est elle-même indisponible si sqlite-vec manque. — *implémenté* (comportement constaté) — preuve `app/api/deps.py:76-90` · test : aucun

### Présentation d'un titre (`/describe`)

- **FR-BE-659** — Requête : `title` (obligatoire), `overview`, `genres` (liste), `year`, `language` (`fr` ou `en`, défaut `fr`). Réponse : `recommendation`, `model`. — *implémenté* — preuve `app/api/ai.py:803-819` · test : aucun
- **FR-BE-660** — Le prompt demande une présentation « enthousiaste » de 2-3 phrases, sans spoiler, dans la langue choisie ; les genres absents deviennent « N/A », le synopsis absent « Non disponible ». Appel `ollama_service.generate` (`/api/generate`, sans flux) ; la réponse est rognée de ses blancs. — *implémenté* — preuve `app/api/ai.py:863-878`, `app/services/ollama_service.py:25-31` · test : aucun
- **FR-BE-661** — Toute exception (transport, délai, statut HTTP, JSON inattendu) donne 503 « LLM unavailable — Ollama unreachable or model not loaded », journalisée en avertissement. — *implémenté* — preuve `app/api/ai.py:852-857`, `app/api/ai.py:876-880` · test : aucun

### Chat libre (`/chat`)

- **FR-BE-662** — Requête : `messages[]` (`role` parmi `user`, `assistant`, `system` ; `content` libre) et `stream` (défaut `false`). Sans flux, la conversation entière part vers `ollama_service.chat` (`/api/chat`) ; réponse `reply`, `model`. — *implémenté* — preuve `app/api/ai.py:822-840`, `app/api/ai.py:886`, `app/api/ai.py:902-904`, `app/services/ollama_service.py:34-40` · test : aucun
- **FR-BE-663** — Sans flux, toute exception donne le 503 LLM. — *implémenté* — preuve `app/api/ai.py:905-906` · test : aucun
- **FR-BE-664** — Avec `stream=true`, la réponse est un flux `text/event-stream` : un événement `data: <fragment>` par fragment non vide, puis `data: [DONE]`. — *implémenté* — preuve `app/api/ai.py:888-900`, `app/services/ollama_service.py:43-59` · test : aucun
- **FR-BE-665** — En flux, seul le **contenu du dernier message** est envoyé, comme prompt brut à `/api/generate` : l'historique et les messages `system` sont ignorés, contrairement au mode sans flux. — *partiel* (manque : conversation multi-tours en flux) — preuve `app/api/ai.py:891-893` · test : aucun
- **FR-BE-666** — Une panne pendant le flux n'a pas de code HTTP d'erreur (le 200 est déjà envoyé) : le flux émet `data: [ERROR]` puis se ferme ; le client doit reconnaître la sentinelle. — *implémenté* — preuve `app/api/ai.py:896-898` · test : aucun
- **FR-BE-667** — Chaque fragment est écrit tel quel après `data: ` : un fragment qui contient un saut de ligne n'est pas découpé en plusieurs lignes `data:`, ce qui casse le découpage SSE côté client. — *partiel* (manque : encodage SSE des fragments multi-lignes) — preuve `app/api/ai.py:894` · test : aucun
- **FR-BE-668** — Une liste `messages` vide n'est pas rejetée : sans flux, `[]` part vers `/api/chat` ; en flux, le prompt est une chaîne vide. — *implémenté* (comportement constaté) — preuve `app/api/ai.py:832`, `app/api/ai.py:892` · test : aucun

### Sonde du LLM (`/llm/status`)

- **FR-BE-669** — `GET /llm/status` renvoie toujours 200 avec `healthy`, `model`, `ollamaUrl`, `detail`. — *implémenté* — preuve `app/api/ai.py:843-850`, `app/api/ai.py:909-918` · test : aucun
- **FR-BE-670** — La sonde interroge `GET /api/tags` (délai 5 s) ; `healthy` vaut vrai si et seulement si `OLLAMA_MODEL` figure **exactement** dans les noms installés. `detail` vaut « ok », ou cite le modèle manquant et la liste des modèles installés, ou le texte de l'exception. — *implémenté* — preuve `app/services/ollama_service.py:62-73` · test : aucun
- **FR-BE-671** — La sonde ne teste pas la génération : un Ollama qui liste le modèle mais échoue sur `/api/generate` est déclaré sain, alors que les autres routes renvoient 503 (faux positif observé en juillet 2026). — *partiel* (manque : essai de génération) — preuve `app/services/ollama_service.py:66-70`, `tv:docs/daily/2026-07-02-feature-backend-subtitle-translation.md:27-29` · test : aucun
- **FR-BE-672** — La réponse expose l'URL interne d'Ollama, la liste des modèles installés et le texte brut des exceptions à tout porteur de clé. — *implémenté* (comportement constaté) — preuve `app/api/ai.py:916-917`, `app/services/ollama_service.py:70`, `app/services/ollama_service.py:73` · test : aucun

### Assistant catalogue (`/assistant`)

- **FR-BE-673** — Requête : `message` (au moins 1 caractère), `mediaType` (`movie`, `tv` ou absent), `history[]` (`role`, `content`), `limit` (1 à 10, défaut 5). Réponse : `reply`, `sources[]` (`tmdbId`, `title`, `mediaType`), `model`. — *implémenté* — preuve `app/api/ai.py:588-619` · test `tests/test_ai_assistant.py:209`, `tests/test_ai_assistant.py:325`, `tests/test_ai_assistant.py:381`
- **FR-BE-674** — Le message est vectorisé ; fastembed indisponible donne 503 « AI model unavailable » (motif distinct du 503 LLM). — *implémenté* — preuve `app/api/ai.py:645-653` · test : aucun
- **FR-BE-675** — Les `limit` titres les plus proches sont lus par `semantic_search_with_overview` (CAP-BE-13), filtrés par `mediaType` ; `sources` reprend ces titres dans l'ordre de la recherche. — *implémenté* — preuve `app/api/ai.py:655-668` · test `tests/test_ai_assistant.py:219`, `tests/test_ai_assistant.py:272`
- **FR-BE-676** — Le prompt système, en français, impose de ne répondre qu'avec les titres listés et de ne pas en inventer. Chaque titre y figure sous la forme `- <titre> : <synopsis tronqué à 300 caractères>` ; sans titre, le libellé est `tmdb:<id>` ; sans résultat, le bloc vaut « (aucun titre disponible) ». — *implémenté* — preuve `app/api/ai.py:585`, `app/api/ai.py:664-686` · test `tests/test_ai_assistant.py:241`
- **FR-BE-677** — L'historique est inséré tel quel entre le prompt système et le message courant. `role` est une chaîne libre (et non une liste fermée comme pour `/chat`) : un tour `system` fourni par l'appelant est transmis. — *implémenté* — preuve `app/api/ai.py:593`, `app/api/ai.py:688-693` · test `tests/test_ai_assistant.py:351`
- **FR-BE-678** — Aucun titre trouvé n'interrompt pas la requête : le LLM est quand même appelé, avec un catalogue vide. — *implémenté* (comportement constaté) — preuve `app/api/ai.py:676`, `app/api/ai.py:696-697` · test : aucun
- **FR-BE-679** — Échec d'Ollama : 503 LLM. — *implémenté* — preuve `app/api/ai.py:696-699` · test `tests/test_ai_assistant.py:294`
- **FR-BE-680** — L'ancrage dans le catalogue n'est demandé qu'au prompt : rien ne vérifie que la réponse ne cite que des titres de `sources`, et `sources` liste les titres fournis, pas ceux effectivement cités. — *partiel* (manque : contrôle de la réponse) — preuve `app/api/ai.py:678-684`, `app/api/ai.py:702-706` · test : aucun

### Synopsis et étiquettes (`/blurb`)

- **FR-BE-681** — `POST /blurb` : `tmdbId`, `mediaType` (`movie` ou `tv`), `lang` (chaîne libre, défaut `fr`), `title`, `overview`, `genres` (chaîne, et non liste comme pour `/describe`), `force` (défaut `false`). Réponse : `tmdbId`, `mediaType`, `lang`, `summary`, `tags[]`, `cached`, `model`. — *implémenté* — preuve `app/api/ai.py:1065-1088` · test `tests/test_ai_blurb.py:136`
- **FR-BE-682** — Sans `force`, une entrée existante pour (`tmdbId`, `mediaType`, `lang`) est renvoyée sans appeler le LLM, avec `cached=true` et le modèle **enregistré** (pas le modèle courant). — *implémenté* — preuve `app/api/ai.py:1105-1125` · test `tests/test_ai_blurb.py:136`
- **FR-BE-683** — `force=true` régénère et remplace l'entrée. — *implémenté* — preuve `app/api/ai.py:1106`, `app/api/ai.py:1174-1193` · test `tests/test_ai_blurb.py:189`
- **FR-BE-684** — Les textes sources viennent de la requête ; si `title` ou `overview` manque, chaque champ vide est complété depuis `ai_tmdb_cache`. Ni titre ni synopsis au final : 422. — *implémenté* — preuve `app/api/ai.py:1127-1151` · test `tests/test_ai_blurb.py:227`, `tests/test_ai_blurb.py:253`
- **FR-BE-685** — `generate_blurb` demande un JSON `{"summary", "tags"}` : synopsis de 1-2 phrases sans spoiler et 3 à 6 étiquettes, dans la langue demandée (« français » pour `fr`, sinon le code `lang` tel quel en majuscules). — *implémenté* — preuve `app/services/recommendation_service.py:428-471` · test `tests/test_ai_blurb.py:136`
- **FR-BE-686** — Analyse tolérante : balises de code Markdown retirées, objet exigé avec `summary` et `tags`, étiquettes vides écartées. Réponse illisible : un second essai avec une consigne plus stricte, puis repli sur les 300 premiers caractères du texte brut (ou le titre) et `tags=[]`, avec un 200. — *implémenté* — preuve `app/services/recommendation_service.py:397-425`, `app/services/recommendation_service.py:473-486` · test `tests/test_ai_blurb.py:269`
- **FR-BE-687** — Une exception Ollama, au premier comme au second essai, donne le 503 LLM. — *implémenté* — preuve `app/api/ai.py:1154-1164` · test `tests/test_ai_blurb.py:293`
- **FR-BE-688** — Le résultat est persisté par suppression puis insertion dans `write_with_retry` (session neuve à chaque essai). Un échec définitif est **avalé sans journal** et le résultat frais est tout de même renvoyé avec `cached=false`. — *implémenté* — preuve `app/api/ai.py:1166-1200`, `app/api/ai.py:1202-1211` · test `tests/test_ai_cache_persist_real_lock.py:150` (verrou réel)
- **FR-BE-689** — `GET /blurb/{tmdb_id}?mediaType=…&lang=…` (`lang` défaut `fr`) lit le cache seul, sans jamais appeler le LLM ; absence : 404 « blurb not found ». — *implémenté* — preuve `app/api/ai.py:1214-1245` · test `tests/test_ai_blurb.py:313`
- **FR-BE-690** — La table `ai_media_blurb` porte `tmdb_id`, `media_type`, `lang` (clé primaire composée), `summary`, `tags` (tableau JSON en texte), `model`, `created_at` (epoch ms). La migration 012 est idempotente et crée `ix_media_blurb_tmdb` et `ix_media_blurb_lang`. — *implémenté* — preuve `app/models/database.py:320-337`, `app/db/migrations.py:468-500` · test `tests/test_ai_blurb.py:375`
- **FR-BE-691** — Une entrée n'expire jamais : aucune purge planifiée, `created_at` n'est jamais relu, et changer `OLLAMA_MODEL` ne l'invalide pas (le modèle n'est pas dans la clé). Seul `force=true` la renouvelle. — *implémenté* (comportement constaté) — preuve `app/api/ai.py:1107-1115`, `app/models/database.py:331-337` · test : aucun

### Client Ollama et réglages

- **FR-BE-692** — Le client offre `generate`, `chat`, `stream_generate` et `is_healthy`. Chaque appel ouvre un `httpx.AsyncClient` éphémère, sans pool partagé et sans nouvel essai. — *implémenté* — preuve `app/services/ollama_service.py:25-73` · test : aucun direct (le service est remplacé par des faux dans `tests/test_ai_assistant.py` et `tests/test_ai_blurb.py`)
- **FR-BE-693** — Délais fixés dans le code : 120 s par appel de génération, 5 s pour la sonde ; aucun réglage d'environnement ne les modifie. — *implémenté* — preuve `app/services/ollama_service.py:21-22` · test : aucun
- **FR-BE-694** — `OLLAMA_URL` (défaut : l'hôte Docker `khoj-ollama`) et `OLLAMA_MODEL` (défaut `gemma4:e4b`) sont lus au démarrage et journalisés au boot ; `model` de chaque réponse fraîche vaut `OLLAMA_MODEL`. — *implémenté* — preuve `app/config.py:206-207`, `app/config.py:483`, `.env.example:61-62` · test : aucune assertion sur la valeur
- **FR-BE-695** — Aucune limite de débit ni de taille n'encadre ces routes : les champs texte (`title`, `overview`, `content`, `message`, `history`) n'ont pas de longueur maximale, le nombre de messages n'est pas borné, et le limiteur n'est pas branché sur `/api/ai`. — *implémenté* (comportement constaté) — preuve `app/api/ai.py:593-603`, `app/api/ai.py:808-812`, `app/api/ai.py:825-833`, `app/api/ai.py:1072-1075` · test : aucun

### Côté app TV (lecture seule)

- **FR-BE-696** — L'app TV n'appelle aucune de ces six routes : son interface Retrofit IA ne déclare que `rank`, `rank-multi`, `embed/status` et `subtitles/translate`, et sa propre carte les classe parmi les routes backend sans appelant. — *mort* (côté produit : aucun client) — preuve `tv:core/network/src/main/java/com/chakir/plexhubtv/core/network/ai/AiRecommendationApi.kt:26-56`, `tv:docs/retro-spec/fonctionnel/00-carte.md:221` · test TV : sans objet

## Règles métier

- **RG-BE-14.1** — Deux motifs de 503 distincts : « LLM unavailable — Ollama unreachable or model not loaded » pour toute exception Ollama sur `describe`, `chat` (sans flux), `assistant` et `blurb` ; « AI model unavailable » pour fastembed sur `assistant`. Le 503 « AI vector storage unavailable » du garde passe avant les deux, et avant le 401. — preuve `app/api/ai.py:852-857`, `app/api/ai.py:650-653`, `app/api/deps.py:81-90`
- **RG-BE-14.2** — En flux SSE, jamais de code d'erreur : `[DONE]` en fin normale, `[ERROR]` sur exception. — preuve `app/api/ai.py:895-898`
- **RG-BE-14.3** — Clé du cache de blurb : (`tmdb_id`, `media_type`, `lang`), sans le modèle. `lang` n'est ni normalisé ni validé : `fr` et `FR` donnent deux entrées. Pas d'expiration. — preuve `app/api/ai.py:1107-1115`, `app/api/ai.py:1072`, `app/models/database.py:331-333`
- **RG-BE-14.4** — Blurb : au plus deux appels au LLM par requête (essai normal, essai strict), donc jusqu'à 240 s sans délai global ; repli sur 300 caractères. Le nombre de 3 à 6 étiquettes et la longueur du synopsis ne sont demandés qu'au prompt, jamais contrôlés. — preuve `app/services/recommendation_service.py:463-486`, `app/services/ollama_service.py:21`
- **RG-BE-14.5** — Assistant : 1 à 10 titres de contexte (défaut 5), synopsis tronqués à 300 caractères ; réponse imposée en français. — preuve `app/api/ai.py:585`, `app/api/ai.py:603`, `app/api/ai.py:678-684`
- **RG-BE-14.6** — Sonde : sain si et seulement si `/api/tags` répond en moins de 5 s et que le nom exact de `OLLAMA_MODEL` est listé. — preuve `app/services/ollama_service.py:62-71`
- **RG-BE-14.7** — Langue : `describe` accepte `fr` ou `en` (liste fermée) ; `blurb` accepte n'importe quelle chaîne ; `assistant` répond toujours en français. — preuve `app/api/ai.py:812`, `app/api/ai.py:1072`, `app/services/recommendation_service.py:452`, `app/api/ai.py:682`

## États, erreurs et cas limites

| Situation | Comportement observé | Preuve |
|---|---|---|
| sqlite-vec non chargé | 503 « AI vector storage unavailable » sur les six routes, y compris `/llm/status`, avant le 401 | `app/api/deps.py:81-85` ; test : aucun |
| `X-API-Key` absente ou invalide | 401 « Invalid API key » | `app/api/deps.py:86-90` ; test `tests/test_ai_assistant.py:200`, `tests/test_ai_blurb.py:357` |
| Ollama injoignable, `describe` / `chat` sans flux | 503 LLM | `app/api/ai.py:876-880`, `app/api/ai.py:902-906` ; test : aucun |
| Ollama injoignable, `chat` en flux | 200 puis `data: [ERROR]` | `app/api/ai.py:896-898` ; test : aucun |
| Fragment de flux contenant un saut de ligne | Écrit tel quel : trame SSE mal formée | `app/api/ai.py:894` ; test : aucun |
| `chat` en flux avec historique | Seul le dernier message est envoyé | `app/api/ai.py:891-893` ; test : aucun |
| `messages` vide | Pas de 422 ; `[]` ou prompt vide transmis à Ollama | `app/api/ai.py:832`, `app/api/ai.py:892` ; test : aucun |
| Modèle non installé | `/llm/status` : `healthy=false`, liste des modèles dans `detail` ; autres routes : 503 | `app/services/ollama_service.py:69-70` ; test : aucun |
| Modèle listé mais génération en échec | `/llm/status` : `healthy=true` (faux positif) ; autres routes : 503 | `app/services/ollama_service.py:66-70` ; test : aucun |
| fastembed indisponible, `assistant` | 503 « AI model unavailable » | `app/api/ai.py:648-653` ; test : aucun |
| `assistant` sans titre proche | LLM appelé avec « (aucun titre disponible) » | `app/api/ai.py:676` ; test : aucun |
| `assistant`, message vide | 422 de validation | `app/api/ai.py:600` ; test `tests/test_ai_assistant.py:209` |
| `blurb` sans texte source ni entrée `ai_tmdb_cache` | 422 | `app/api/ai.py:1147-1151` ; test `tests/test_ai_blurb.py:253` |
| `blurb`, JSON illisible deux fois | 200, synopsis tronqué, `tags=[]` | `app/services/recommendation_service.py:481-484` ; test `tests/test_ai_blurb.py:269` |
| `blurb`, écriture du cache en échec | Résultat frais renvoyé, aucune trace | `app/api/ai.py:1195-1200` ; test : aucun |
| `blurb`, verrou SQLite pendant l'écriture | Nouvel essai en session neuve | `app/api/ai.py:1195-1196` ; test `tests/test_ai_cache_persist_real_lock.py:150` |
| `GET /blurb` sans entrée | 404 « blurb not found » | `app/api/ai.py:1235-1236` ; test `tests/test_ai_blurb.py:313` |
| Changement de `OLLAMA_MODEL` | Les blurbs existants restent servis avec l'ancien `model` | `app/api/ai.py:1116-1125` ; test : aucun |
| Ollama lent (> 120 s) | `httpx` lève un délai dépassé, 503 (ou `[ERROR]` en flux) | `app/services/ollama_service.py:21`, `app/api/ai.py:852-857` ; test : aucun |

## Dépendances

- Autres capacités : `CAP-BE-02` (garde `X-API-Key`, `verify_api_key`) ; `CAP-BE-13` (garde sqlite-vec du routeur, `embed_query` et `semantic_search_with_overview` pour l'assistant, table `ai_tmdb_cache` pour le repli du blurb, et mêmes appels Ollama pour `explain` et la reformulation) ; `CAP-BE-15` (même client `ollama_service.generate` et même `_ollama_503`).
- Autre dépôt : aucun appel `tv:` (FR-BE-696).
- Données : `ai_media_blurb` (migration 012) ; lecture de `ai_tmdb_cache` et `ai_embeddings` (migration 008). Écriture par `write_with_retry` (`app/utils/db_retry.py`). Dépendance d'exécution : Ollama joignable à `OLLAMA_URL`, modèle chargé.

## Écarts relevés

| Écart | Nature | Suite proposée |
|---|---|---|
| **1.** Aucun appelant : ni l'app TV ni un autre client ne consomme ces six routes (FR-BE-696), et aucun parcours Maestro ne les couvre. | code mort (côté produit) | à trancher par Chakir : exposer dans l'app (assistant, blurb sur la fiche) ou geler/retirer |
| **2.** `describe`, `chat` (les deux modes) et `llm/status` n'ont aucun test ; `ollama_service` n'est jamais testé directement ; le 503 fastembed de l'assistant et le 503 sqlite-vec non plus. | test manquant | tests avec `respx` sur `ollama_service` ; tests de route pour `describe`, `chat`, `llm/status` |
| **3.** `/llm/status` déclare sain un Ollama qui ne sait plus générer (FR-BE-671), cas déjà rencontré en juillet 2026 d'après le journal TV. Elle est aussi indisponible (503) quand sqlite-vec manque (FR-BE-658). | risque (diagnostic) | sonde de génération courte ; sortir la sonde du garde sqlite-vec : à trancher par Chakir |
| **4.** Le chat en flux perd l'historique et les consignes `system` (FR-BE-665) : les deux modes d'une même route ne se comportent pas pareil. `docs/architecture/flux-cles.md` §5.7 le décrit, sans le signaler comme limite. | spec ↔ code (incohérence) | utiliser `/api/chat` en flux, ou documenter le flux comme mono-tour |
| **5.** Trame SSE : un fragment contenant `\n` n'est pas découpé (FR-BE-667). | risque (bug client) | émettre une ligne `data:` par ligne, ou encoder en JSON |
| **6.** Aucune limite de débit ni de taille (FR-BE-695) : un porteur de clé par utilisateur peut occuper Ollama 120 s par appel (240 s pour un blurb) sans frein, avec des prompts de taille arbitraire. | risque (coût, disponibilité) | `max_length` sur les champs, borne sur `messages`/`history`, limite de débit par clé sur `/api/ai` |
| **7.** Injection de prompt : `title`, `overview`, `genres`, `message` et `history` sont interpolés sans délimiteur ; l'assistant accepte un tour `system` fourni par l'appelant (FR-BE-677), qui peut annuler la règle d'ancrage. L'impact est borné (le LLM n'a aucun outil, l'appelant est authentifié). | risque (sécurité) | limiter `role` à `user`/`assistant` pour l'historique, délimiter le contenu dans les prompts |
| **8.** `/llm/status` renvoie l'URL interne d'Ollama, la liste des modèles et le texte brut des exceptions (FR-BE-672). | risque (fuite d'information, faible) | ne renvoyer que `healthy`/`model` aux clés par utilisateur |
| **9.** Cache de blurb sans expiration ni invalidation au changement de modèle (FR-BE-691), `lang` non normalisé (RG-BE-14.3), échec d'écriture avalé sans journal (FR-BE-688) ; `ix_media_blurb_tmdb` double le préfixe de la clé primaire et aucune requête vue ne filtre sur `lang` seul. Le cache de sous-titres, lui, a une rétention et une purge (CAP-BE-15). | choix à trancher / dette | rétention + purge, ou modèle dans la clé ; `logger.warning` sur l'échec ; revoir les index |
| **10.** L'ancrage de l'assistant n'est qu'une consigne (FR-BE-680) : `sources` peut contenir des titres non cités, et la réponse des titres absents. | risque (qualité) | contrôle a posteriori des titres cités, ou renommer `sources` en « contexte fourni » |
| **11.** Références de ligne périmées. `docs/architecture/flux-cles.md` §5.7 cite ai.py:863 pour `describe` (réel : `app/api/ai.py:860`), ai.py:886 pour `chat` (réel : `app/api/ai.py:883`), ai.py:912 pour `llm/status` (réel : `app/api/ai.py:909`), ai.py:855-860 pour `_ollama_503` (réel : `app/api/ai.py:852-857`), config.py:60-62 (réel : `app/config.py:206-207`), et deps.py:76-80 pour le garde (réel : `app/api/deps.py:76-90`) ; §5.8 cite ai.py:623 pour l'assistant (décorateur `app/api/ai.py:622`, exact à une ligne), ai.py:1084 et ai.py:1201 pour le blurb (réel : `app/api/ai.py:1091`, `app/api/ai.py:1214`). `CLAUDE.md` §2 cite config.py:60-62 et config.py:84 (réel : `app/config.py:206-207`, `app/config.py:483`), recommendation_service.py:403 pour `generate_blurb` (réel : `app/services/recommendation_service.py:428`), database.py:274 pour `AiMediaBlurb` (réel : `app/models/database.py:320`) ; §9 piège 12 cite ai.py:855 (réel : `app/api/ai.py:852`) ; piège 14 cite ai.py:899-901 (réel : `app/api/ai.py:896-898`) ; piège 13 cite config.py:60-62. | référence périmée | mettre à jour au prochain `/sync-context` |
| **12.** `CLAUDE.md` §9 piège 12 affirme qu'une clé absente ou invalide donne 401 « avant tout » ; le garde sqlite-vec passe avant (FR-BE-658). Déjà relevé par CAP-BE-15, écart 1. | doc périmée | corriger le piège 12 |
| **13.** Le test d'idempotence de la table s'appelle `test_m011_table_exists` et parle de « M011 » alors que la migration est la 012 (`tests/test_ai_blurb.py:371-378`). | test trompeur | renommer |
| **14.** En-tête du client : il annonce « deux primitives » (`generate`, `stream_generate`) alors que le module en a quatre (`app/services/ollama_service.py:3-5`). | doc périmée (code) | aligner le docstring |

## Questions ouvertes

- Le choix d'un chat en flux mono-tour (FR-BE-665) est-il voulu ? Rien dans le code ni la doc ne le justifie. À vérifier par test du comportement attendu, puis à trancher.
- Quand le client coupe un flux SSE en cours, la requête vers Ollama est-elle annulée, ou la génération continue-t-elle côté Ollama jusqu'à son terme ? À vérifier par test.
- Que renvoie Ollama pour `messages=[]` sur `/api/chat` ou un prompt vide sur `/api/generate` : une erreur (donc 503) ou une réponse vide (200 avec `reply` vide) ? À vérifier par test.
- La session de base (`Depends(get_db)`) de `/assistant` et `/blurb` reste-t-elle ouverte pendant tout l'appel au LLM (jusqu'à 120 s, ou 240 s pour un blurb) et occupe-t-elle un slot du pool de l'API ? À vérifier par test.
- Deux `POST /blurb` simultanés sur la même clé : la suppression puis l'insertion sous `write_with_retry` peuvent-elles lever une `IntegrityError`, silencieusement avalée, et laisser les deux clients avec des textes différents dont un seul est persisté ? À vérifier par test.
- Un vrai modèle (gemma4) respecte-t-il le format JSON du blurb et la règle d'ancrage de l'assistant ? Aucun test n'utilise un vrai LLM. À vérifier par test.
- `sources` de l'assistant peut-il contenir des doublons ou des titres sans `title` (libellé `tmdb:<id>`) quand `ai_tmdb_cache` est incomplet ? À vérifier par test.
