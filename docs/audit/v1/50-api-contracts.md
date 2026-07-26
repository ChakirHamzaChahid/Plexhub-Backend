# Audit v1 — Phases 5-6 : API / Contrats & État fonctionnel des features

> **Audit 360° indépendant** — branche `develop`, HEAD `9da9d46` (release v1.7.1), 2026-07-26.
> Source de vérité Phase 5 : `GET /openapi.json` du serveur de smoke (Basic Auth, 86 paths, 102 routes au boot) + lecture de code `fichier:ligne` + probes `curl` empiriques (DB vide).
> Périmètre : phases 5 (OpenAPI ⇆ Pydantic, camelCase, contrat Android, motifs 503 IA) et 6 (état fonctionnel des flux). Le statut des findings `CR-*` antérieurs appartient au rapport DELTA (autre agent) — ils sont cités ici uniquement quand un finding les recoupe.

---

## Phase 5 — API & contrats

### AUDIT-P5-001 — Le `jobId` renvoyé par les 5 triggers sync est un handle mort : `GET /api/sync/status/{jobId}` répond toujours `unknown` — **S2**

**Preuve (code)** :
- `app/api/sync.py:29` — `POST /sync/xtream` renvoie `job_id = f"sync_{body.account_id}_{id(task)}"` (identité mémoire de la task asyncio).
- `app/workers/sync_worker.py:1100` — le worker enregistre le job sous une **autre** clé : `job_id = f"sync_{account_id}_{now_ms()}"` (timestamp), via `_record_sync_job` (`sync_worker.py:44-51`).
- Les 4 autres triggers (`/xtream/all` `sync.py:42`, `/enrichment` `sync.py:67`, `/validate-streams` `sync.py:79`, `/full-pipeline` `sync.py:108`) fabriquent des ids `*_{id(task)}` qui ne sont **jamais** enregistrés dans `_sync_jobs` (seul `sync_account` enregistre, et sous sa propre clé).

**Preuve (empirique, serveur de smoke)** :
```
POST /api/sync/xtream/all           -> {"jobId":"sync_all_1744749089920"}
GET  /api/sync/status/sync_all_...  -> {"status":"unknown","progress":null}   (200)
GET  /api/sync/jobs                 -> {"jobs":[]}
```

**Impact** : le handshake 202+`jobId` → poll `GET /status/{jobId}` — le pattern documenté de toute l'API de déclenchement — est **cassé de bout en bout**. Tout client (app Android, script d'automation, UI admin) qui poll le `jobId` retourné voit éternellement `unknown` et ne peut pas distinguer « en cours », « terminé » et « id inexistant » (le 200 `unknown` masque tout). La seule voie fonctionnelle est `GET /api/sync/jobs` (liste), qui ne couvre que les syncs par-compte — jamais enrichment/validation/pipeline. Un client Android qui afficherait une progression de sync sur la foi du `jobId` est cassé **silencieusement** (pas de 404, pas d'erreur).

**Effort** : faible — soit passer le `job_id` du trigger au worker (paramètre de `sync_account`/`run_all_accounts`), soit renvoyer depuis le trigger la clé que le worker fabriquera, soit enregistrer les jobs `all`/`enrichment`/`validation`/`pipeline` dans le même tracker. Ajouter un test de bout-en-bout trigger→status (aucun n'existe, sinon ce bug serait rouge).

---

### AUDIT-P5-002 — Résidus CR-C03 : 3 endpoints JSON `/api/*` hors OpenAPI (dict brut / union non déclarée) — **S3**

Balayage exhaustif du spec (86 paths) : les schémas 2xx vides restants sur `/api/*` sont exactement :

| Endpoint | Preuve | Détail |
|---|---|---|
| `POST /api/media/{rating_key}/rescrape` | `app/api/media.py:432-442` | `return {"status": "queued"}` dict brut, aucun `response_model` — le résidu `media.py` documenté (CR-C03). 202 sans schéma dans le spec. |
| `PUT /api/accounts/{account_id}/categories` | `app/api/categories.py:50-74` | `return {"message": "Category configuration updated successfully"}` dict brut, aucun `response_model`. |
| `POST /api/ai/chat` | `app/api/ai.py:888-904` | Retour union `StreamingResponse` \| `ChatResponse` sans `response_model` → le mode non-stream (`ChatResponse`, typé et camelCase dans le code) est **absent d'OpenAPI**. |

Tous les autres schémas vides sont l'UI admin HTMX (`/admin/*`, réponses HTML — acceptable) et `DELETE /api/accounts/{account_id}` (204 sans corps — correct). `sync.py` est désormais **entièrement typé** (`JobIdResponse`/`MessageResponse`/`SyncJobListResponse`, `app/api/sync.py:18-126`) — vérifié dans le spec (`SyncStatusResponse`, `SyncJobListResponse` référencés).

**Impact** : ces 3 formes de réponse sont invisibles pour un client généré depuis OpenAPI et échappent au filet camelCase (les clés actuelles sont des mots simples, donc pas de fuite snake_case *aujourd'hui* — mais rien ne l'empêche à la prochaine clé ajoutée). **Effort** : trivial (3 petits modèles Pydantic ; pour `/chat`, `response_model=ChatResponse` + `responses={...}` pour documenter le SSE).

---

### AUDIT-P5-003 — La convention « 100 % camelCase » ne couvre pas les paramètres de requête : ~40 params snake_case sur `/api/*` — **S3 (convention à acter, pas à migrer)**

**Preuve (spec)** : extraction exhaustive des paramètres `query`/`path` contenant `_` sur `/api/*` : `server_id` (12 endpoints dont `GET /api/media/{rating_key}`, `/api/stream/{rating_key}`, `/api/live/*`), `parent_rating_key`, `unification_id` (×4), `category_id`, `missing_imdb`/`missing_tmdb`, `device_code`… Au total ~40 occurrences — la **totalité** des paramètres de requête de l'API est snake_case, alors que corps de requête et réponses sont camelCase (`to_camel`, vérifié sur tous les modèles de réponse du spec : **zéro** propriété snake_case dans les schémas de réponse ; seuls les `Body_admin_*` des formulaires HTMX en portent, ce qui est interne à l'UI).

**Analyse** : ce n'est pas une « fuite » ponctuelle mais la convention de fait. Le cas historiquement pointé (CR-F06, `device_code` de tv-auth) a été traité en ajoutant un alias `deviceCode` **prioritaire** avec conservation du legacy (`app/api/tv_auth.py:304-318`, vérifié empiriquement : les deux formes passent la validation) — ce qui crée une **incohérence interne** : tv-auth accepte camelCase+snake, tout le reste n'accepte que snake. Un développeur Android qui généralise `deviceCode` → `serverId` obtient un 400/422.

**Impact Android** : risque de confusion plus que de rupture (les params existants marchent). **Recommandation** : acter la convention dans le contrat (« query/path = snake_case, corps = camelCase ») OU généraliser le pattern double-alias de tv-auth — mais ne pas laisser l'ambiguïté actuelle. **Effort** : documentation = nul ; double-alias généralisé = moyen.

---

### AUDIT-P5-004 — Breaking change livré : `GET /api/media/episodes` exige `server_id` (400 sinon) — rupture client non versionnée — **S2 (risque de coordination app)**

**Preuve** : `app/api/media.py:192-222` — `server_id` reste déclaré `Optional` mais un garde explicite renvoie **400** s'il est absent (choix délibéré pour un message propre plutôt qu'un 422 générique). Empirique : `GET /api/media/episodes?parent_rating_key=series_1` → `400 {"detail":"server_id is required to list episodes: …"}`.

**Analyse** : le correctif est **justifié** (collision cross-comptes des `parent_rating_key`, bug prod MAO/Treadstone) — le fond n'est pas contesté. Le problème est le **mode de livraison** : c'est une rupture de contrat sur un endpoint consommé par l'app Android PlexHubTV, livrée sans versionnement d'API, sans période de dépréciation, sans capacité de détection côté serveur (aucune métrique/log dédié comptant les 400 sur ce garde). **Toute version de l'app antérieure au correctif qui liste des épisodes via l'endpoint brut casse en production** (écran épisodes vide/erreur). Si l'app ne consomme que `/episodes/unified` (qui exige `unification_id`, inchangé), le risque est nul — cette vérification côté app doit être faite explicitement (hors périmètre backend, à tracer au board).

**Effort** : vérification app = faible ; ajout d'un log WARN dédié sur le 400 (mesurer si des clients legacy tapent encore) = trivial.

---

### AUDIT-P5-005 — Changements de wire déjà livrés sur des clés existantes : `vod_count`→`vodCount`, `job_id`→`jobId` — **S3 (rupture assumée, à confirmer côté consommateurs)**

**Preuve** :
- `app/models/schemas.py:427-439` — `CategoryRefreshResponse` : le docstring acte que `POST /accounts/{id}/categories/refresh` renvoyait `vod_count`/`series_count` bruts et renvoie désormais `vodCount`/`seriesCount` (CR-C02).
- `app/models/schemas.py:373-382` — `SyncJobListResponse` : chaque entrée de `GET /api/sync/jobs` passe de `job_id` à `jobId` (CR-C03).

**Impact** : tout consommateur qui parsait les anciennes clés lit désormais `undefined`. Ces deux endpoints sont plutôt côté admin/outillage que app TV, donc l'exposition est faible — mais le pattern est le même que P5-004 : rupture livrée sans mécanisme de détection. À croiser avec le code de PlexHubTV (si l'app appelle `categories/refresh`, elle est cassée). **Effort** : audit des call-sites Android = faible.

---

### AUDIT-P5-006 — `verify_api_key` évalue le 503 sqlite-vec **avant** l'authentification : fuite d'état interne pré-auth sur `/api/ai` — **S3**

**Preuve** : `app/api/deps.py:73-87` — l'ordre est : (1) `if not _VEC_LOADED.get("ok") → 503`, (2) `_authenticate(...) → 401`. Un appelant **sans aucune clé** qui tape n'importe quel endpoint `/api/ai/*` apprend donc si l'extension vectorielle du serveur est chargée (503) ou non (401) — un bit d'état interne servi pré-auth, et une inversion du principe « authentifier d'abord, diagnostiquer ensuite » appliqué partout ailleurs (fail-closed).

**Impact** : faible (un bit de fingerprinting), mais gratuit à corriger et incohérent avec la posture fail-closed revendiquée. ⚠️ Correctif = changement contractuel : les clients qui distinguent aujourd'hui 503-avant-401 (aucun connu) verraient l'ordre s'inverser. **Effort** : trivial (inverser les deux blocs) + MAJ du piège §9.2 de CLAUDE.md qui documente l'ordre actuel.

---

### AUDIT-P5-007 — Motifs 503/422/413 de `/api/ai` : conformes à la doc (vérifié) — **info**

Vérification exhaustive des trois motifs contractuels :
- **503 « AI vector storage unavailable »** au niveau routeur : `deps.py:78-82`, dépendance module-level `ai.py` → s'applique bien à **tous** les endpoints `/api/ai`, y compris LLM purs (`/describe`/`/chat`/`/llm/status`/`/subtitles`) — le couplage documenté (piège §9.12) tient.
- **503 « AI model unavailable »** (fastembed) : `EmbeddingUnavailableError` attrapé sur `/rank` (`ai.py:351`), `/rank-multi` (`ai.py:450`), `/search` (`ai.py:541-543`), `/assistant` (`ai.py:641-645`).
- **503 LLM distinct** `_ollama_503` (`ai.py:852-860`) : `/describe` (`ai.py:880`), `/chat` non-stream (`ai.py:906`), `/assistant` (`ai.py:699`), `/blurb` (`ai.py:1154`), `/subtitles/translate` (`ai.py:1011,1016`). Mode SSE : `data: [ERROR]` sans code HTTP (`ai.py:896-898`), conforme.
- **422/413 sous-titres** : `SubtitleFormatError → 422` (`ai.py:999-1001`), `SubtitleTooLargeError → 413` (`ai.py:1004-1006`).

Dette résiduelle recoupée : la constante dépréciée `HTTP_422_UNPROCESSABLE_ENTITY` est toujours utilisée en prod (`ai.py:330,365,416,423,463,1139` — CR-C09, statut au DELTA).

---

### AUDIT-P5-008 — `GET /api/sync/status/{job_id}` renvoie 200 `unknown` pour un id inexistant — **S3**

**Preuve** : `app/api/sync.py:111-118` — `if not job: return SyncStatusResponse(status="unknown")`. Un id inexistant, mal formé ou expiré (éviction FIFO `_MAX_SYNC_JOBS`, `sync_worker.py:47-51`) est indistinguable d'un état réel. Combiné à P5-001, un client ne peut jamais détecter qu'il poll un handle mort. **Recommandation** : 404 sur id inconnu (rupture mineure à coordonner) ou au minimum un champ `known: false`. **Effort** : trivial.

---

## Phase 6 — État fonctionnel des flux (lecture de code + probes)

### AUDIT-P6-001 — Relay WebDAV `/dav` : le déblocage du scan Plex est un pansement opérationnel hors-code, non vérifiable par le backend — **S2 (risque opérationnel), feature no-op par défaut**

**Contexte vérifié** : la chaîne DAV elle-même est saine au niveau contrat — `dav_dispatch` (`app/api/dav.py:277-320`) : 503 si `DAV_ENABLED=false` (`dav.py:288`, confirmé empiriquement `/dav/` → 503), méthodes limitées OPTIONS/PROPFIND/HEAD/GET (écritures → 405 par routing Starlette avant même l'auth, `dav.py:283-286,319-320`), Basic Auth dédiée fail-closed (`deps.py:148-182`), throttle par compte avec 503+`Retry-After` (`app/dav/throttle.py:31-32`), HEAD servi 0-upstream.

**Le problème** : le blocage connu à l'intégration (scan Plex à froid → transaction SQLite tenue pendant l'I/O amont lente → cascade `database is locked`, 0 item indexé — retour device 2026-07) est « corrigé » par `38aeb5a` **uniquement côté opérations** :
1. Le correctif est une **séquence manuelle en 4 étapes** (rebuild d'arbre → `rclone rc vfs/refresh` → `scripts/prewarm-dav-cache.sh` → scan, runbook §5.1) sans aucune automatisation ni enforcement — un seul oubli humain (ou un scan périodique Plex déclenché seul) rejoue la cascade.
2. Le script suppose des flags rclone (`--vfs-cache-mode full`, `--vfs-cache-max-age 720h`, `--vfs-cache-max-size` dimensionné) qu'il **ne vérifie pas** (`scripts/prewarm-dav-cache.sh:21-28` — prérequis en commentaire seulement) ; si `max-age` est resté au défaut 1 h, le préchauffage est silencieusement évincé avant le scan.
3. Bugs mineurs du script en mode concurrent (`CONCURRENCY>1`, hors défaut) : `wait -n` avalé par `|| true` sur bash < 4.3 → cap de concurrence non tenu (`prewarm-dav-cache.sh:109-115`), et le compteur `failed` n'est pas incrémenté en mode parallèle (`:109`). Le mode série (défaut) est correct.
4. La piste pérenne (cache header+tail intégré au relais) est explicitement **non implémentée** (runbook §9, commit message `38aeb5a`).

**Verdict** : `38aeb5a` est un contournement documenté et honnête, pas un correctif — la feature reste **fragile à l'exploitation** (chaque rebuild d'arbre ré-expose au risque). Atténuants réels : `DAV_ENABLED=false` par défaut, caps 25/5 (`DAV_MOVIE_LIMIT`/`DAV_SERIES_LIMIT`), périmètre single-host. **Effort** correctif pérenne : moyen-fort (cache header/tail côté relay) ; enforcement minimal (préflight du script vérifiant `rclone rc options/get` + un flag « dernier prewarm » consultable) : faible.

---

### AUDIT-P6-002 — Pipeline download unifié Xtream+Plex : câblage vérifié cohérent — **info (vérifié sain)**

- Routage mono-worker confirmé : `app/workers/download_worker.py:224,451-461` — `is_plex_server_id(job.server_id)` dispatch vers `plex_download_service.resolve_job_urls` (couple download+direct-play), `fallback_urls=plex_urls[1:]` passé à `download_to_disk` (`:512`) → fallback 403 « download désactivé » câblé comme documenté (§5.9) ; `fallback_urls=[]` côté Xtream (chemin inchangé).
- Miroirs JSON admin : `GET /api/admin/downloads`, `/api/admin/plex-downloads/{servers,catalog,catalog/{type}/{unification_id}}`, `/api/admin/enrichment/*` présents au spec, tous gardés `verify_master_key` module-level, schémas camelCase (`PlexServerResponse`… `schemas.py:585-646`).
- Backfill OMDb : contrat 202+`jobId` / 409 mono-run / 404 job inconnu conforme (`app/api/enrichment.py:69-113`), schémas camelCase inline, garde master-only justifiée (budget OMDb + mutation catalogue).

Dette UI connue non re-contestée ici : UDL-01 (total du panneau file écrasant le total catalogue sur `admin_downloads`/`admin_plex_downloads`, board `docs/31-board.md`).

---

### AUDIT-P6-003 — tv-auth device-flow : contrat vérifié, aliases fonctionnels — **info (vérifié sain)**

- `GET /status` accepte `deviceCode` (prioritaire) **et** `device_code` legacy (`tv_auth.py:304-318`) ; empirique : les deux formes passent la validation (404 « Unknown device code » sur code inconnu, pas 422). Le 422 n'apparaît que si les deux sont absents (`tv_auth.py:319-323`).
- Livraison one-shot : claim atomique par UPDATE conditionnel `payload_delivered IS FALSE` + `rowcount == 1` (`tv_auth.py:350-369`) — la course du double-poll est fermée côté code (statut CR-F07 au DELTA).
- Writers request-path sous `commit_with_retry` (`tv_auth.py:294,367`).
- Codes : 201 start / 404 code inconnu / 409 déjà approuvé / 410 expiré / 503 payload indéchiffrable (`tv_auth.py:282-297,344-348`) — cohérents.

---

### AUDIT-P6-004 — `POST /api/plex/generate` : `outputDir` désormais confiné à `PLEX_LIBRARY_DIR` (contrat 400 explicite) — **info (constat de code, statut CR-S01 au DELTA)**

`app/api/plex.py:35-73` — `_resolve_confined_output_dir` : chemin client accepté seulement s'il résout **dans** `PLEX_LIBRARY_DIR` (`resolve()` + `Path.parents`, pas de préfixe-string naïf) ; 400 si hors-base ou si `PLEX_LIBRARY_DIR` non configuré. Le contrat de l'endpoint change donc pour les appelants qui passaient un `outputDir` arbitraire (avant : accepté ; maintenant : 400) — rupture **souhaitable** mais à connaître. `GenerateResponse` typé camelCase (`plex.py:24-32`).

---

### AUDIT-P6-005 — Pipeline sync→enrichment→validation→génération : ordre vérifié, mais son observabilité de contrat dépend de P5-001 — **S3**

`POST /api/sync/full-pipeline` (`sync.py:86-108`) enchaîne bien les 4 phases en série et délègue la génération à `plex_generation_service.generate_plex_library_auto` (plus d'import du symbole privé de `app.main` — commentaire CR-A02 en place, `sync.py:99-104`). Mais le seul retour observable est le `jobId` mort de P5-001 : l'opérateur qui déclenche un pipeline complet via l'API n'a **aucun moyen contractuel** d'en connaître l'issue (succès/échec/durée) autrement qu'en lisant les logs serveur. Les erreurs de `_full_pipeline` partent dans `create_background_task` sans trace côté API. **Effort** : couvert par le fix P5-001 s'il enregistre aussi les jobs pipeline.

---

## Récapitulatif Phases 5-6

| ID | Sévérité | Titre | Preuve clé |
|---|---|---|---|
| AUDIT-P5-001 | **S2** | `jobId` des 5 triggers sync = handle mort, status toujours `unknown` | `sync.py:29` vs `sync_worker.py:1100` + curl 200 `unknown` |
| AUDIT-P5-002 | S3 | 3 endpoints JSON hors OpenAPI (rescrape, PUT categories, /ai/chat) | `media.py:432-442`, `categories.py:50-74`, `ai.py:888-904` |
| AUDIT-P5-003 | S3 | Params de requête 100 % snake_case vs réponses camelCase — convention non actée | extraction openapi.json (~40 params) |
| AUDIT-P5-004 | **S2** | Breaking livré : `GET /api/media/episodes` → 400 sans `server_id` (app Android legacy casse) | `media.py:192-222` + curl 400 |
| AUDIT-P5-005 | S3 | Wire changes livrés `vod_count`→`vodCount`, `job_id`→`jobId` | `schemas.py:427-439,373-382` |
| AUDIT-P5-006 | S3 | 503 sqlite-vec évalué **avant** l'auth sur `/api/ai` (fuite d'état pré-auth) | `deps.py:73-87` |
| AUDIT-P5-007 | info | Motifs 503/422/413 IA conformes à la doc (vérifié) | `deps.py:78-82`, `ai.py:351,450,852-860,999-1006` |
| AUDIT-P5-008 | S3 | `GET /sync/status/{job_id}` : 200 `unknown` pour id inexistant (pas de 404) | `sync.py:111-118` |
| AUDIT-P6-001 | **S2** | DAV : déblocage scan Plex = séquence manuelle non vérifiée par le code (prérequis rclone non checkés, fix pérenne absent) | `38aeb5a`, `scripts/prewarm-dav-cache.sh:21-28,109-115`, runbook §9 |
| AUDIT-P6-002 | info | Download unifié Xtream+Plex : routage/fallback/miroirs vérifiés sains | `download_worker.py:224,451-461,512` |
| AUDIT-P6-003 | info | tv-auth : aliases deviceCode/device_code + claim atomique vérifiés | `tv_auth.py:304-369` + curl |
| AUDIT-P6-004 | info | `/plex/generate` : `outputDir` confiné, contrat 400 explicite | `plex.py:35-73` |
| AUDIT-P6-005 | S3 | Full-pipeline : issue du run non observable par contrat (dépend de P5-001) | `sync.py:86-108` |
