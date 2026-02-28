# PlexHub Backend - Documentation Technique & Guide d'Integration Android

## Table des Matieres

1. [Vue d'ensemble](#1-vue-densemble)
2. [Architecture](#2-architecture)
3. [Configuration & Demarrage](#3-configuration--demarrage)
4. [Schema de la Base de Donnees](#4-schema-de-la-base-de-donnees)
5. [Workers (Taches de fond)](#5-workers-taches-de-fond)
6. [API REST - Reference Complete](#6-api-rest---reference-complete)
7. [Guide d'Integration Android](#7-guide-dintegration-android)
8. [Flux de Donnees Complets](#8-flux-de-donnees-complets)

---

## 1. Vue d'ensemble

PlexHub Backend est un serveur **Python/FastAPI** qui :

- Se connecte aux serveurs **Xtream Codes** (IPTV) via leurs API
- Synchronise le catalogue complet (films, series, episodes) dans une base **SQLite** locale
- Enrichit les metadonnees via l'API **TMDB** (IDs IMDB, confiance du match)
- Verifie periodiquement la sante des flux (streams casses)
- Expose une **API REST** en camelCase consommable par l'app Android

**Stack technique :** Python 3.14, FastAPI, SQLAlchemy 2.0 async, aiosqlite, httpx, APScheduler, rapidfuzz

---

## 2. Architecture

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────┐
│  App Android     │────>│  PlexHub Backend  │────>│ Xtream API  │
│  (client HTTP)   │<────│  (FastAPI)        │<────│ (IPTV)      │
└─────────────────┘     │                   │     └─────────────┘
                        │  ┌─────────────┐  │     ┌─────────────┐
                        │  │ SQLite DB   │  │────>│ TMDB API    │
                        │  │ (plexhub.db)│  │<────│ (metadata)  │
                        │  └─────────────┘  │     └─────────────┘
                        └──────────────────┘
```

### Composants internes

| Composant | Role |
|---|---|
| `app/api/` | Endpoints REST (accounts, categories, media, stream, sync, health) |
| `app/services/xtream_service.py` | Client HTTP vers les serveurs Xtream Codes |
| `app/services/tmdb_service.py` | Client HTTP vers l'API TMDB + fuzzy matching |
| `app/services/media_service.py` | Requetes SQLAlchemy pour lire les medias |
| `app/services/stream_service.py` | Construction des URLs de streaming |
| `app/services/category_service.py` | Gestion des categories (CRUD, filtrage whitelist/blacklist) |
| `app/workers/sync_worker.py` | Synchronisation incrementale du catalogue avec filtrage par categories |
| `app/workers/enrichment_worker.py` | Enrichissement TMDB optimise (parallele, IDs partiels) |
| `app/workers/health_check_worker.py` | Verification des flux casses |
| `app/models/database.py` | Schema SQLAlchemy (tables Media, XtreamAccount, XtreamCategory, EnrichmentQueue) |
| `app/models/schemas.py` | Modeles Pydantic (request/response en camelCase) |
| `app/utils/string_normalizer.py` | Parsing titres IPTV, normalisation unicode, extraction annee |
| `app/utils/unification.py` | Calcul des IDs d'unification cross-serveur |

---

## 3. Configuration & Demarrage

### Variables d'environnement

Les variables sont chargees depuis un fichier `.env` a la racine du projet via `python-dotenv` (`load_dotenv()`). Le fichier `.env` est lu **avant** l'initialisation de la classe `Settings`, ce qui garantit que `os.getenv()` retourne les bonnes valeurs.

| Variable | Defaut | Description |
|---|---|---|
| `TMDB_API_KEY` | `""` (vide) | Cle API TMDB. Si vide, l'enrichissement est desactive |
| `DATA_DIR` | `./data` | Repertoire du fichier SQLite (`plexhub.db`) |
| `LOG_DIR` | `./logs` | Repertoire des logs (rotation a 10MB, 5 fichiers) |
| `SYNC_INTERVAL_HOURS` | `6` | Intervalle de re-synchronisation automatique |
| `ENRICHMENT_DAILY_LIMIT` | `50000` | Nombre max d'appels TMDB par cycle d'enrichissement |
| `HEALTH_CHECK_BATCH_SIZE` | `1000` | Nombre de streams verifies par cycle |

### Cycle de vie au demarrage

1. Initialisation de la base SQLite (tables + PRAGMA optimises : WAL, cache 64MB)
2. Election du worker maitre (lock file, un seul processus execute les taches de fond)
3. Le worker maitre lance :
   - Sync initial de tous les comptes Xtream → puis enrichissement TMDB
   - Scheduler recurrent : sync toutes les 6h, enrichissement toutes les 6h, health check a 2h du matin

---

## 4. Schema de la Base de Donnees

### Table `media` (catalogue complet)

**Cle primaire composite :** `(rating_key, server_id, filter, sort_order)`

#### Conventions de `rating_key`

| Type | Format | Exemple |
|---|---|---|
| Film | `vod_{stream_id}.{ext}` | `vod_435071.mp4` |
| Serie | `series_{series_id}` | `series_6581` |
| Saison | `season_{series_id}_{season_num}` | `season_6581_1` |
| Episode | `ep_{episode_id}.{ext}` | `ep_7890.mkv` |

#### Convention de `server_id`

Format : `xtream_{account_id}` ou `account_id` = MD5(`base_url` + `username`)[:8]

#### Colonnes completes

| Colonne | Type | Description |
|---|---|---|
| **Identifiants** | | |
| `rating_key` | TEXT PK | Identifiant unique du media dans le serveur |
| `server_id` | TEXT PK | Identifiant du serveur Xtream |
| `filter` | TEXT PK | Filtre de categorie (defaut: `"all"`) |
| `sort_order` | TEXT PK | Ordre de tri (defaut: `"default"`) |
| **Metadonnees** | | |
| `library_section_id` | TEXT | Groupe de bibliotheque (`"xtream_vod"`, `"xtream_series"`) |
| `title` | TEXT | Titre d'affichage |
| `title_sortable` | TEXT | Titre normalise pour le tri (minuscule, sans articles) |
| `type` | TEXT | `"movie"`, `"show"`, ou `"episode"` |
| `thumb_url` | TEXT | URL de la vignette/poster |
| `art_url` | TEXT | URL du fond/backdrop |
| `year` | INT | Annee de sortie |
| `duration` | INT | Duree en **millisecondes** |
| `summary` | TEXT | Synopsis/description |
| `genres` | TEXT | Genres separes par virgule (ex: `"Action, Drama"`) |
| `content_rating` | TEXT | Classification (PG-13, R, etc.) |
| **Etat de lecture** | | |
| `view_offset` | INT | Position de reprise en millisecondes |
| `view_count` | INT | Nombre de lectures |
| `last_viewed_at` | BIGINT | Derniere lecture (timestamp ms) |
| **Hierarchie (episodes)** | | |
| `parent_title` | TEXT | Nom de la saison |
| `parent_rating_key` | TEXT | `rating_key` de la saison |
| `parent_index` | INT | Numero de saison |
| `grandparent_title` | TEXT | Nom de la serie |
| `grandparent_rating_key` | TEXT | `rating_key` de la serie |
| `index` | INT | Numero d'episode |
| `parent_thumb` | TEXT | Vignette de la saison |
| `grandparent_thumb` | TEXT | Vignette de la serie |
| **IDs externes** | | |
| `guid` | TEXT | GUID Plex (non utilise pour Xtream) |
| `imdb_id` | TEXT | ID IMDB (ex: `"tt1234567"`) |
| `tmdb_id` | TEXT | ID TMDB (ex: `"12345"`) |
| `rating` | FLOAT | Note numerique |
| `audience_rating` | FLOAT | Note audience |
| **Unification** | | |
| `unification_id` | TEXT | Regroupement cross-serveur (`"imdb://tt123"` ou `"tmdb://456"`) |
| `history_group_key` | TEXT | Cle de regroupement d'historique |
| `server_ids` | TEXT | Serveurs possedant ce contenu (separes par virgule) |
| `rating_keys` | TEXT | Rating keys correspondants (separes par virgule) |
| **Timestamps** | | |
| `added_at` | BIGINT | Date d'ajout au serveur (timestamp ms) |
| `updated_at` | BIGINT | Derniere mise a jour (timestamp ms) |
| **Affichage** | | |
| `display_rating` | FLOAT | Note a afficher dans l'UI |
| `scraped_rating` | FLOAT | Note scrappee (alternative) |
| `resolved_thumb_url` | TEXT | URL finale de la vignette |
| `resolved_art_url` | TEXT | URL finale du backdrop |
| `resolved_base_url` | TEXT | URL de base pour streaming |
| `alternative_thumb_urls` | TEXT | URLs de vignettes alternatives (separes par `\|`) |
| **Backend** | | |
| `is_broken` | BOOL | Flux casse (defaut: `false`) |
| `stream_error_count` | INT | Nombre d'erreurs de flux |
| `last_stream_check` | BIGINT | Dernier check de sante (timestamp ms) |
| `tmdb_match_confidence` | FLOAT | Confiance du match TMDB (0.0 a 1.0) |
| `content_hash` | TEXT | MD5 des champs sync (evite les UPDATE inutiles) |
| `dto_hash` | TEXT | MD5 des champs DTO Xtream (sync incrementale) |
| `is_in_allowed_categories` | BOOL | Media dans une categorie autorisee (defaut: `true`) |

### Table `xtream_accounts`

| Colonne | Type | Description |
|---|---|---|
| `id` | TEXT PK | MD5(`base_url` + `username`)[:8] |
| `label` | TEXT | Nom d'affichage du compte |
| `base_url` | TEXT | URL du serveur Xtream |
| `port` | INT | Port de connexion (defaut: 80) |
| `username` | TEXT | Nom d'utilisateur |
| `password` | TEXT | Mot de passe |
| `status` | TEXT | Statut de connexion (defaut: `"Unknown"`) |
| `expiration_date` | BIGINT | Expiration abonnement (timestamp ms) |
| `max_connections` | INT | Connexions simultanees max (defaut: 1) |
| `allowed_formats` | TEXT | Formats autorises (`"ts,mp4,m3u8"`) |
| `server_url` | TEXT | URL serveur alternative |
| `https_port` | INT | Port HTTPS |
| `last_synced_at` | BIGINT | Dernier sync reussi (timestamp ms) |
| `is_active` | BOOL | Compte actif (defaut: `true`) |
| `created_at` | BIGINT | Date de creation (timestamp ms) |
| `category_filter_mode` | TEXT | Mode de filtrage des categories : `"all"`, `"whitelist"`, `"blacklist"` (defaut: `"all"`) |

### Table `xtream_categories` (nouveau)

Stocke les categories Xtream par compte avec leur configuration de filtrage.

**Contrainte unique :** `(account_id, category_id, category_type)`

| Colonne | Type | Description |
|---|---|---|
| `id` | INT PK | Auto-increment |
| `account_id` | TEXT | Reference vers `xtream_accounts.id` |
| `category_id` | TEXT | ID de categorie Xtream (ex: `"1"`, `"42"`) |
| `category_type` | TEXT | `"vod"` ou `"series"` |
| `category_name` | TEXT | Nom affiche de la categorie (ex: `"Action"`, `"Comedies FR"`) |
| `is_allowed` | BOOL | Categorie autorisee au sync (defaut: `true`) |
| `last_fetched_at` | BIGINT | Derniere recuperation depuis Xtream (timestamp ms) |

### Table `enrichment_queue`

| Colonne | Type | Description |
|---|---|---|
| `id` | INT PK | Auto-increment |
| `rating_key` | TEXT | Identifiant du media |
| `server_id` | TEXT | Serveur Xtream |
| `media_type` | TEXT | `"movie"` ou `"show"` |
| `title` | TEXT | Titre (pour recherche TMDB) |
| `year` | INT | Annee (pour matching TMDB) |
| `status` | TEXT | `"pending"` / `"done"` / `"skipped"` / `"failed"` |
| `attempts` | INT | Nombre de tentatives |
| `last_error` | TEXT | Dernier message d'erreur |
| `created_at` | BIGINT | Date d'ajout dans la queue (timestamp ms) |
| `processed_at` | BIGINT | Date de traitement (timestamp ms) |
| `existing_tmdb_id` | TEXT | TMDB ID deja present avant enrichissement (optimise les appels API) |
| `existing_imdb_id` | TEXT | IMDB ID deja present avant enrichissement (optimise les appels API) |

---

## 5. Workers (Taches de fond)

### 5.1 Sync Worker — Synchronisation incrementale avec filtrage par categories

**Frequence :** Toutes les 6 heures (configurable) + au demarrage

**Phase 0a : Auto-refresh des categories depuis Xtream**
1. Appel `get_vod_categories()` et `get_series_categories()` sur le serveur Xtream
2. Upsert dans `xtream_categories` via `on_conflict_do_update` : met a jour `category_name` et `last_fetched_at`, **preserve `is_allowed`** existant
3. Les nouvelles categories sont creees avec `is_allowed = true` par defaut

**Phase 0b : Chargement de la configuration des categories**
1. Charge le `category_filter_mode` du compte (`"all"`, `"whitelist"`, `"blacklist"`)
2. Charge la table `xtream_categories` pour determiner les categories autorisees
3. Le filtrage s'applique individuellement a chaque item selon son `category_id`
4. Les items synchronises sont marques `is_in_allowed_categories = true` en BDD (utilise pour le filtrage API)

**Logique de filtrage :**
| Mode | Comportement |
|---|---|
| `all` | Toutes les categories sont synchronisees (pas de filtrage) |
| `whitelist` | Seules les categories marquees `is_allowed = true` sont synchronisees |
| `blacklist` | Toutes les categories sauf celles marquees `is_allowed = false` sont synchronisees |

**Phase 1 : Films (VOD)**
1. Appel `get_vod_streams(account)` → liste complete des DTOs VOD
2. **Filtrage par categorie** : chaque item est verifie contre la configuration du compte
3. Calcul d'un `dto_hash` (MD5) par DTO sur les champs : `name`, `added`, `stream_icon`, `rating`, `category_id`, `container_extension`
4. Comparaison avec les `dto_hash` existants en BDD
5. **Seuls les items nouveaux ou modifies** declenchent un appel `get_vod_info(vod_id=stream_id)` (appel couteux)
6. Mapping DTO → ligne Media avec extraction titre/annee, duree, genres, TMDB ID si present
7. Upsert en BDD avec `content_hash` : si le contenu n'a pas change, le `UPDATE` est ignore
8. Les films sans `tmdb_id` **OU** sans `imdb_id` sont ajoutes a la queue d'enrichissement (avec les IDs existants sauvegardes)
9. Nettoyage differentiel : suppression des films retires du catalogue Xtream — **uniquement en mode `all`**. En mode `whitelist` ou `blacklist`, le nettoyage est desactive pour eviter la suppression des items appartenant a des categories filtrees

**Phase 2 : Series**
1. Appel `get_series(account)` → liste des series
2. **Filtrage par categorie** identique a la Phase 1
3. Meme logique incrementale avec `_compute_series_dto_hash()` (champs: `name`, `cover`, `plot`, `genre`, `rating`, `category_id`, `backdrop_path`, `episode_run_time`, `last_modified`)
4. Upsert des series modifiees, enrichissement queue pour celles sans TMDB ID ou IMDB ID
5. Nettoyage differentiel : meme regle que Phase 1 — **desactive en mode `whitelist`/`blacklist`**

**Phase 3 : Episodes (uniquement pour les series modifiees)**
1. Appel `get_series_info(series_id=series_id)` **uniquement pour les series detectees comme modifiees**
2. Mapping des episodes avec hierarchie complete (episode → saison → serie)
3. Upsert par batchs de 50 series

**Gestion defensive des reponses Xtream :**
- Les reponses `vod_info` et `series_info` peuvent contenir des types inattendus (listes au lieu de dicts)
- Le worker detecte et gere gracieusement ces cas sans planter le sync
- Les champs `info` et `movie_data` sont valides comme dicts avant acces

**Optimisations :**
- Fetch parallele avec semaphore (10 requetes simultanees)
- Commits intermediaires par batch (100 VOD, 50 series)
- Skip des UPDATE si `content_hash` identique
- Skip des appels `get_vod_info`/`get_series_info` si `dto_hash` identique (sync incrementale)

### 5.2 Enrichment Worker — Enrichissement TMDB optimise

**Frequence :** Apres chaque sync + toutes les 6 heures

**Condition d'ajout a la queue :** Un media est enqueue si `tmdb_id` **OU** `imdb_id` est manquant (pas uniquement si les deux sont absents). Les IDs existants sont sauvegardes dans `existing_tmdb_id` / `existing_imdb_id` pour optimiser les appels.

**Phase 1 : Films**
1. Charge les items `pending` de type `movie` depuis `enrichment_queue`
2. Pour chaque film, 4 scenarios possibles :

| Scenario | TMDB | IMDB | Action | Appels API |
|---|---|---|---|---|
| Les deux presents | oui | oui | Skip (ne devrait pas arriver) | 0 |
| TMDB present, IMDB manquant | oui | non | `get_movie_details(tmdb_id)` (avec `append_to_response=external_ids`) | 1 |
| IMDB present, TMDB manquant | non | oui | Conserver l'IMDB existant | 0 |
| Les deux manquants | non | non | `search_movie()` + `get_movie_details()` | 2 |

3. Seuil de confiance : match accepte si `confidence >= 0.85`
4. Mise a jour Media — **IDs + metadonnees riches** :

| Champ Media | Source TMDB | Condition de mise a jour |
|---|---|---|
| `tmdb_id` | `id` | Toujours |
| `imdb_id` | `external_ids.imdb_id` (prefixe `tt` garanti) | Toujours |
| `unification_id` | `"imdb://tt123"` ou `"tmdb://456"` | Toujours |
| `tmdb_match_confidence` | Score fuzzy (0.85-1.0) | Toujours |
| `summary` | `overview` | Seulement si vide en BDD |
| `genres` | `genres[].name` (virgule-separe) | Seulement si vide en BDD |
| `resolved_thumb_url` | `poster_path` (URL complete w500) | Seulement si vide en BDD |
| `resolved_art_url` | `backdrop_path` (URL complete w1280) | Seulement si vide en BDD |
| `scraped_rating` | `vote_average` (0-10) | Seulement si vide en BDD |
| `display_rating` | `vote_average` | Seulement si = 0.0 |
| `year` | `release_date` / `first_air_date` | Seulement si vide en BDD |

5. Les appels `get_movie_details()` et `get_tv_details()` utilisent `append_to_response=external_ids` pour recuperer les details + les IDs externes en **un seul appel** API

**Phase 2 : Series** — meme logique avec `search_tv()` et `get_tv_details()`

**Parallelisme :** 5 requetes TMDB simultanees, commits par batch de 50

**Matching TMDB (fuzzy) :**
- Normalisation des titres (minuscule, sans articles)
- Similarite via `rapidfuzz.fuzz.ratio()` (0-100, normalise en 0-1)
- Facteur annee : match exact = 1.0, +/-1 an = 0.95, plus = 0.85
- Score final = `title_similarity * year_factor`
- Seuil minimum : 0.85

### 5.3 Health Check Worker — Verification des streams

**Frequence :** Tous les jours a 2h du matin

1. Selectionne aleatoirement `HEALTH_CHECK_BATCH_SIZE` (1000) streams non verifies depuis 7 jours
2. Pour chaque stream : requete HTTP HEAD avec timeout 5s
3. Met a jour `is_broken`, `stream_error_count`, `last_stream_check`

---

## 6. API REST - Reference Complete

**Base URL :** `http://<host>:<port>/api`

**Serialisation :** Toutes les reponses JSON utilisent **camelCase** (ex: `ratingKey`, `serverId`, `thumbUrl`)

**Compression :** GZip active pour les reponses > 1000 octets

**CORS :** Tous les origines autorisees

### 6.1 Health

#### `GET /api/health`

Etat du systeme et statistiques.

**Reponse 200 :**
```json
{
  "status": "ok",
  "version": "1.0.0",
  "accounts": 2,
  "totalMedia": 145000,
  "enrichedMedia": 80000,
  "brokenStreams": 120,
  "lastSyncAt": 1772240521981
}
```

---

### 6.2 Comptes Xtream

#### `GET /api/accounts`

Liste tous les comptes configures.

**Reponse 200 :** `AccountResponse[]`
```json
[
  {
    "id": "05fd75e9",
    "label": "Mon Xtream",
    "baseUrl": "http://example.com",
    "port": 80,
    "username": "user123",
    "status": "Active",
    "expirationDate": 1772240521981,
    "maxConnections": 1,
    "allowedFormats": "ts,mp4,m3u8",
    "serverUrl": null,
    "httpsPort": null,
    "lastSyncedAt": 1772240521981,
    "isActive": true,
    "createdAt": 1772240521981
  }
]
```

#### `POST /api/accounts`

Ajoute un nouveau compte. Authentifie automatiquement aupres du serveur Xtream et declenche un sync initial en arriere-plan.

**Request Body :**
```json
{
  "label": "Mon Xtream",
  "baseUrl": "http://example.com",
  "port": 80,
  "username": "user123",
  "password": "pass456"
}
```

**Reponse 201 :** `AccountResponse` (le compte cree avec les infos du serveur)

**Erreurs :**
- `409` : Le compte existe deja (meme `base_url` + `username`)
- `400` : Echec d'authentification aupres du serveur Xtream

#### `PUT /api/accounts/{account_id}`

Met a jour un compte (partiel ou complet).

**Request Body :** (tous les champs sont optionnels)
```json
{
  "label": "Nouveau nom",
  "baseUrl": "http://new-server.com",
  "port": 8080,
  "username": "newuser",
  "password": "newpass",
  "isActive": false
}
```

**Reponse 200 :** `AccountResponse`

**Erreurs :** `404` : Compte non trouve

#### `DELETE /api/accounts/{account_id}`

Supprime un compte **et tous ses medias et entrees d'enrichissement associes**.

**Reponse 204 :** Pas de contenu

**Erreurs :** `404` : Compte non trouve

#### `POST /api/accounts/{account_id}/test`

Teste la connexion a un serveur Xtream.

**Reponse 200 :**
```json
{
  "status": "Active",
  "expirationDate": 1772240521981,
  "maxConnections": 2,
  "allowedFormats": "ts,mp4,m3u8"
}
```

**Erreurs :**
- `404` : Compte non trouve
- `400` : Echec de connexion

---

### 6.3 Media

#### `GET /api/media/movies`

Liste paginee des films.

**Parametres query :**

| Parametre | Type | Defaut | Description |
|---|---|---|---|
| `limit` | int | 500 | Items par page (1-5000) |
| `offset` | int | 0 | Decalage de pagination |
| `sort` | string | `"added_desc"` | Tri (voir options ci-dessous) |
| `server_id` | string | null | Filtrer par serveur Xtream |

**Options de tri :** `added_desc`, `added_asc`, `title_asc`, `title_desc`, `rating_desc`, `year_desc`

**Reponse 200 :**
```json
{
  "items": [
    {
      "ratingKey": "vod_435071.mp4",
      "serverId": "xtream_05fd75e9",
      "librarySectionId": "xtream_vod",
      "title": "Inception",
      "titleSortable": "inception",
      "filter": "all",
      "sortOrder": "default",
      "pageOffset": 0,
      "type": "movie",
      "thumbUrl": "http://image.tmdb.org/poster.jpg",
      "artUrl": null,
      "year": 2010,
      "duration": 8880000,
      "summary": "A thief who steals corporate secrets...",
      "genres": "Action, Science Fiction",
      "contentRating": "PG-13",
      "viewOffset": 0,
      "viewCount": 0,
      "lastViewedAt": 0,
      "parentTitle": null,
      "parentRatingKey": null,
      "parentIndex": null,
      "grandparentTitle": null,
      "grandparentRatingKey": null,
      "index": null,
      "parentThumb": null,
      "grandparentThumb": null,
      "mediaParts": "[]",
      "guid": null,
      "imdbId": "tt1375666",
      "tmdbId": "27205",
      "rating": 8.8,
      "audienceRating": null,
      "unificationId": "imdb://tt1375666",
      "historyGroupKey": "imdb://tt1375666",
      "serverIds": null,
      "ratingKeys": null,
      "addedAt": 1672531200000,
      "updatedAt": 1772240521981,
      "displayRating": 8.8,
      "scrapedRating": null,
      "resolvedThumbUrl": null,
      "resolvedArtUrl": null,
      "resolvedBaseUrl": null,
      "alternativeThumbUrls": null,
      "isBroken": false,
      "tmdbMatchConfidence": 0.98
    }
  ],
  "total": 144000,
  "hasMore": true
}
```

#### `GET /api/media/shows`

Liste paginee des series. Memes parametres que `/movies`.

**Reponse 200 :** Meme structure. Les items ont `type: "show"`.

#### `GET /api/media/episodes`

Liste des episodes d'une serie.

**Parametres query :**

| Parametre | Type | Requis | Description |
|---|---|---|---|
| `parent_rating_key` | string | **Oui** | `rating_key` de la serie (ex: `series_6581`) |
| `limit` | int | Non | Items par page (defaut 500) |
| `offset` | int | Non | Decalage (defaut 0) |
| `server_id` | string | Non | Filtrer par serveur |

**Reponse 200 :** Meme structure. Les items ont `type: "episode"` avec les champs de hierarchie remplis :
```json
{
  "items": [
    {
      "ratingKey": "ep_7890.mkv",
      "serverId": "xtream_05fd75e9",
      "type": "episode",
      "title": "Pilot",
      "index": 1,
      "parentIndex": 1,
      "parentTitle": "Season 1",
      "parentRatingKey": "season_6581_1",
      "grandparentTitle": "Breaking Bad",
      "grandparentRatingKey": "series_6581",
      "grandparentThumb": "http://...",
      "duration": 3540000,
      "..."
    }
  ],
  "total": 62,
  "hasMore": false
}
```

#### `GET /api/media/{rating_key}?server_id={server_id}`

Detail d'un media unique.

**Parametres :**
- `rating_key` (path) : ex. `vod_435071.mp4`
- `server_id` (query, **requis**) : ex. `xtream_05fd75e9`

**Reponse 200 :** `MediaResponse` (un seul objet)

**Erreurs :** `404` : Media non trouve

---

### 6.4 Streaming

#### `GET /api/stream/{rating_key}?server_id={server_id}`

Obtient l'URL de streaming directe pour un media.

**Parametres :**
- `rating_key` (path) : ex. `vod_435071.mp4` ou `ep_7890.mkv`
- `server_id` (query, **requis**) : format `xtream_{account_id}`

**Reponse 200 :**
```json
{
  "url": "http://example.com:80/movie/user123/pass456/435071.mp4",
  "expiresAt": null
}
```

**Construction des URLs :**

| Type | Format URL |
|---|---|
| Film | `http://{base}:{port}/movie/{username}/{password}/{stream_id}.{ext}` |
| Episode | `http://{base}:{port}/series/{username}/{password}/{episode_id}.{ext}` |

> **Note :** L'extension par defaut est `"ts"` si non specifiee dans le `rating_key`.

**Erreurs :**
- `400` : Format `server_id` invalide ou impossible de construire l'URL
- `404` : Compte non trouve

---

### 6.5 Synchronisation

#### `POST /api/sync/xtream`

Declenche la synchronisation d'un compte en arriere-plan.

**Request Body :**
```json
{
  "accountId": "05fd75e9",
  "force": false
}
```

**Reponse 202 :**
```json
{
  "jobId": "sync_05fd75e9_1772240521981"
}
```

#### `POST /api/sync/xtream/all`

Declenche la synchronisation de **tous les comptes actifs**.

**Reponse 202 :**
```json
{
  "jobId": "sync_all_1772240521981"
}
```

#### `GET /api/sync/status/{job_id}`

Suivi de la progression d'un job de sync.

**Reponse 200 :**
```json
{
  "status": "processing",
  "progress": {}
}
```

**Valeurs possibles de `status` :** `"pending"`, `"processing"`, `"completed"`, `"failed"`, `"unknown"`

---

### 6.6 Categories

Les categories permettent de filtrer le contenu synchronise depuis Xtream. Chaque compte peut etre configure en mode `all` (tout synchroniser), `whitelist` (uniquement les categories autorisees) ou `blacklist` (tout sauf les categories bloquees).

#### `GET /api/accounts/{account_id}/categories`

Liste toutes les categories d'un compte avec le mode de filtrage actuel.

**Reponse 200 :**
```json
{
  "items": [
    {
      "categoryId": "1",
      "categoryName": "Action",
      "categoryType": "vod",
      "isAllowed": true,
      "lastFetchedAt": 1772240521981
    },
    {
      "categoryId": "42",
      "categoryName": "Comedies FR",
      "categoryType": "series",
      "isAllowed": false,
      "lastFetchedAt": 1772240521981
    }
  ],
  "filterMode": "whitelist"
}
```

#### `PUT /api/accounts/{account_id}/categories`

Met a jour la configuration de filtrage d'un compte.

**Comportement important selon le mode :**
- **Mode `whitelist`** : les categories listees dans le body recoivent leur valeur `isAllowed`. Toutes les categories **non listees** sont automatiquement mises a `is_allowed = false`.
- **Mode `blacklist`** : les categories listees dans le body recoivent leur valeur `isAllowed`. Toutes les categories **non listees** sont automatiquement mises a `is_allowed = true`.
- **Mode `all`** : le filtrage est desactive, toutes les categories sont synchronisees.

En mode `whitelist`, il suffit donc d'envoyer **uniquement les categories a autoriser** (avec `isAllowed: true`). Le backend desactive automatiquement tout le reste.

**Request Body :**
```json
{
  "filterMode": "whitelist",
  "categories": [
    {
      "categoryId": "94",
      "categoryType": "vod",
      "isAllowed": true
    },
    {
      "categoryId": "68",
      "categoryType": "series",
      "isAllowed": true
    }
  ]
}
```

> **Attention :** Les `categoryId` doivent correspondre aux IDs reels retournes par `GET .../categories`. Ces IDs sont specifiques au fournisseur Xtream et peuvent changer. Toujours recuperer les IDs actuels via le GET avant de construire le PUT.

**Reponse 200 :**
```json
{
  "message": "Category configuration updated successfully"
}
```

**Erreurs :**
- `400` : Mode de filtrage invalide ou donnees incorrectes

#### `POST /api/accounts/{account_id}/categories/refresh`

Force la recuperation des categories depuis le serveur Xtream. Preserve les configurations `isAllowed` existantes. Les nouvelles categories sont autorisees par defaut.

**Reponse 200 :**
```json
{
  "message": "Categories refreshed successfully",
  "vod_count": 45,
  "series_count": 30,
  "total": 75
}
```

**Erreurs :**
- `404` : Compte non trouve
- `500` : Erreur de connexion au serveur Xtream

---

## 7. Guide d'Integration Android

### 7.1 Configuration du client HTTP

```
Base URL : http://<ip-backend>:<port>/api
Content-Type : application/json
Encodage : UTF-8
```

Toutes les reponses sont en **camelCase**. Configurer le deserializer JSON (Gson/Moshi/Kotlinx) en consequence.

La compression GZip est active. Ajouter le header `Accept-Encoding: gzip` pour beneficier de reponses compressees.

### 7.2 Flux d'initialisation recommande

```
1. GET /api/health
   → Verifier que le backend est accessible (status == "ok")

2. GET /api/accounts
   → Charger la liste des comptes configures
   → Si vide, rediriger vers l'ecran d'ajout de compte

3. POST /api/accounts  (si premier lancement)
   → Ajouter un compte Xtream
   → Le backend synchronise automatiquement en arriere-plan

4. GET /api/media/movies?limit=50&sort=added_desc
   → Charger la premiere page de films (les plus recents)

5. GET /api/media/shows?limit=50&sort=added_desc
   → Charger la premiere page de series
```

### 7.3 Navigation dans le catalogue

#### Films

```
Listing :  GET /api/media/movies?limit=50&offset=0&sort=added_desc
Page 2  :  GET /api/media/movies?limit=50&offset=50
Detail  :  GET /api/media/{ratingKey}?server_id={serverId}
Stream  :  GET /api/stream/{ratingKey}?server_id={serverId}
```

#### Series → Episodes

```
Listing series :  GET /api/media/shows?limit=50&offset=0
Episodes       :  GET /api/media/episodes?parent_rating_key={seriesRatingKey}&server_id={serverId}
Stream episode :  GET /api/stream/{episodeRatingKey}?server_id={serverId}
```

**Hierarchie des episodes :**

```
Serie (type: "show")
  └─ ratingKey: "series_6581"

Episode (type: "episode")
  ├─ ratingKey: "ep_7890.mkv"
  ├─ parentRatingKey: "season_6581_1"     ← saison
  ├─ parentIndex: 1                        ← numero de saison
  ├─ grandparentRatingKey: "series_6581"  ← serie
  ├─ grandparentTitle: "Breaking Bad"
  └─ index: 1                              ← numero d'episode
```

Pour obtenir les episodes d'une serie, utiliser `parent_rating_key` avec la valeur `grandparentRatingKey` de l'episode **OU** le `ratingKey` de la serie. Les episodes sont lies via le champ `grandparentRatingKey`.

### 7.4 Lecture d'un stream

```kotlin
// 1. Obtenir l'URL de streaming
val response = api.getStream(ratingKey, serverId)
val streamUrl = response.url  // "http://server/movie/user/pass/12345.mp4"

// 2. Passer l'URL au lecteur video (ExoPlayer, VLC, etc.)
val mediaItem = MediaItem.fromUri(streamUrl)
exoPlayer.setMediaItem(mediaItem)
exoPlayer.prepare()
exoPlayer.play()
```

**Formats de stream :** Les URLs contiennent l'extension du fichier (`.mp4`, `.mkv`, `.ts`, `.avi`, etc.). Utiliser un lecteur compatible multi-format comme ExoPlayer ou VLC.

### 7.5 Gestion des images

Les champs d'images dans `MediaResponse` :

| Champ | Usage |
|---|---|
| `thumbUrl` | Poster/vignette principale du media |
| `artUrl` | Image de fond/backdrop |
| `resolvedThumbUrl` | URL finale resolue (si disponible) |
| `resolvedArtUrl` | URL finale resolue (si disponible) |
| `alternativeThumbUrls` | URLs alternatives separees par `\|` |
| `parentThumb` | Vignette de la saison (pour episodes) |
| `grandparentThumb` | Vignette de la serie (pour episodes) |

**Strategie recommandee :** Utiliser `resolvedThumbUrl` en priorite, puis `thumbUrl` en fallback. Pour les episodes, utiliser `grandparentThumb` comme poster de la serie.

### 7.6 Modeles de donnees Kotlin recommandes

```kotlin
data class MediaItem(
    val ratingKey: String,
    val serverId: String,
    val librarySectionId: String,
    val title: String,
    val titleSortable: String = "",
    val type: String,                    // "movie", "show", "episode"
    val thumbUrl: String? = null,
    val artUrl: String? = null,
    val year: Int? = null,
    val duration: Int? = null,           // en millisecondes
    val summary: String? = null,
    val genres: String? = null,          // separes par virgule
    val contentRating: String? = null,

    // Hierarchie (episodes)
    val parentTitle: String? = null,
    val parentRatingKey: String? = null,
    val parentIndex: Int? = null,        // numero de saison
    val grandparentTitle: String? = null,
    val grandparentRatingKey: String? = null,
    val index: Int? = null,              // numero d'episode
    val parentThumb: String? = null,
    val grandparentThumb: String? = null,

    // IDs externes
    val imdbId: String? = null,
    val tmdbId: String? = null,
    val rating: Float? = null,

    // Unification
    val unificationId: String = "",
    val historyGroupKey: String = "",

    // Timestamps (millisecondes)
    val addedAt: Long = 0,
    val updatedAt: Long = 0,

    // Affichage
    val displayRating: Float = 0f,
    val resolvedThumbUrl: String? = null,
    val resolvedArtUrl: String? = null,
    val alternativeThumbUrls: String? = null,

    // Backend
    val isBroken: Boolean = false,
    val tmdbMatchConfidence: Float? = null
)

data class MediaListResponse(
    val items: List<MediaItem>,
    val total: Int,
    val hasMore: Boolean
)

data class StreamResponse(
    val url: String,
    val expiresAt: Long? = null
)

data class AccountResponse(
    val id: String,
    val label: String,
    val baseUrl: String,
    val port: Int,
    val username: String,
    val status: String,
    val expirationDate: Long? = null,
    val maxConnections: Int = 1,
    val allowedFormats: String = "",
    val lastSyncedAt: Long = 0,
    val isActive: Boolean = true,
    val createdAt: Long = 0
)

data class AccountCreate(
    val label: String,
    val baseUrl: String,
    val port: Int = 80,
    val username: String,
    val password: String
)

data class HealthResponse(
    val status: String,
    val version: String,
    val accounts: Int,
    val totalMedia: Int,
    val enrichedMedia: Int,
    val brokenStreams: Int,
    val lastSyncAt: Long? = null
)

// --- Categories ---

data class CategoryItem(
    val categoryId: String,
    val categoryName: String,
    val categoryType: String,       // "vod" ou "series"
    val isAllowed: Boolean,
    val lastFetchedAt: Long
)

data class CategoryListResponse(
    val items: List<CategoryItem>,
    val filterMode: String           // "all", "whitelist", "blacklist"
)

data class CategoryUpdate(
    val categoryId: String,
    val categoryType: String,
    val isAllowed: Boolean
)

data class CategoryUpdateRequest(
    val filterMode: String,          // "all", "whitelist", "blacklist"
    val categories: List<CategoryUpdate>
)
```

### 7.7 API Retrofit (exemple)

```kotlin
interface PlexHubApi {

    // Health
    @GET("health")
    suspend fun getHealth(): HealthResponse

    // Accounts
    @GET("accounts")
    suspend fun getAccounts(): List<AccountResponse>

    @POST("accounts")
    suspend fun createAccount(@Body account: AccountCreate): AccountResponse

    @DELETE("accounts/{accountId}")
    suspend fun deleteAccount(@Path("accountId") accountId: String)

    @POST("accounts/{accountId}/test")
    suspend fun testAccount(@Path("accountId") accountId: String): AccountTestResponse

    // Categories
    @GET("accounts/{accountId}/categories")
    suspend fun getCategories(
        @Path("accountId") accountId: String
    ): CategoryListResponse

    @PUT("accounts/{accountId}/categories")
    suspend fun updateCategories(
        @Path("accountId") accountId: String,
        @Body request: CategoryUpdateRequest
    )

    @POST("accounts/{accountId}/categories/refresh")
    suspend fun refreshCategories(
        @Path("accountId") accountId: String
    )

    // Media
    @GET("media/movies")
    suspend fun getMovies(
        @Query("limit") limit: Int = 50,
        @Query("offset") offset: Int = 0,
        @Query("sort") sort: String = "added_desc",
        @Query("server_id") serverId: String? = null
    ): MediaListResponse

    @GET("media/shows")
    suspend fun getShows(
        @Query("limit") limit: Int = 50,
        @Query("offset") offset: Int = 0,
        @Query("sort") sort: String = "added_desc",
        @Query("server_id") serverId: String? = null
    ): MediaListResponse

    @GET("media/episodes")
    suspend fun getEpisodes(
        @Query("parent_rating_key") parentRatingKey: String,
        @Query("limit") limit: Int = 500,
        @Query("offset") offset: Int = 0,
        @Query("server_id") serverId: String? = null
    ): MediaListResponse

    @GET("media/{ratingKey}")
    suspend fun getMediaDetail(
        @Path("ratingKey") ratingKey: String,
        @Query("server_id") serverId: String
    ): MediaItem

    // Stream
    @GET("stream/{ratingKey}")
    suspend fun getStreamUrl(
        @Path("ratingKey") ratingKey: String,
        @Query("server_id") serverId: String
    ): StreamResponse

    // Sync
    @POST("sync/xtream")
    suspend fun triggerSync(@Body request: SyncRequest): SyncJobResponse

    @POST("sync/xtream/all")
    suspend fun triggerSyncAll(): SyncJobResponse

    @GET("sync/status/{jobId}")
    suspend fun getSyncStatus(@Path("jobId") jobId: String): SyncStatusResponse
}
```

### 7.8 Points importants pour l'integration

1. **Timestamps en millisecondes :** `addedAt`, `updatedAt`, `lastViewedAt`, `expirationDate`, `lastSyncedAt` sont tous en **millisecondes** Unix (pas en secondes).

2. **Durees en millisecondes :** `duration` et `viewOffset` sont en millisecondes. Pour afficher : `duration / 1000 / 60` = minutes.

3. **Filtrage des streams casses :** Utiliser le champ `isBroken` pour griser ou masquer les contenus avec des flux invalides.

4. **Genres :** Chaine separee par virgules. Splitter avec `genres?.split(",")?.map { it.trim() }`.

5. **Pagination :** Le champ `hasMore` indique s'il y a plus d'items. Incrementer `offset` de `limit` pour la page suivante.

6. **Sync asynchrone :** Les endpoints `POST /api/sync/*` retournent un `jobId` immediatement (HTTP 202). Interroger `GET /api/sync/status/{jobId}` pour suivre la progression.

7. **Unification cross-serveur :** Le champ `unificationId` permet de regrouper le meme contenu provenant de differents comptes Xtream. Format : `"imdb://tt1234567"` ou `"tmdb://12345"`.

8. **Rating key comme identifiant :** Le `ratingKey` est l'identifiant principal d'un media. Il encode le type et l'ID Xtream :
   - `vod_435071.mp4` → film, stream_id=435071, extension=mp4
   - `series_6581` → serie, series_id=6581
   - `ep_7890.mkv` → episode, episode_id=7890, extension=mkv

9. **server_id obligatoire :** Pour les endpoints `GET /api/media/{ratingKey}` et `GET /api/stream/{ratingKey}`, le `server_id` est **obligatoire** en query param car le `ratingKey` n'est unique que dans le contexte d'un serveur.

10. **Filtrage par categories :** Le backend supporte le filtrage du contenu synchronise par categories Xtream. Trois modes sont disponibles :
    - `all` : tout le contenu est synchronise (defaut)
    - `whitelist` : seules les categories marquees `isAllowed: true` sont synchronisees
    - `blacklist` : tout est synchronise sauf les categories marquees `isAllowed: false`

    **Workflow recommande :**
    1. `GET .../categories` pour recuperer les IDs reels des categories
    2. `PUT .../categories` avec `filterMode: "whitelist"` et les categories a autoriser (les non listees sont auto-desactivees)
    3. `POST /api/sync/xtream` pour declencher un sync avec le filtrage actif

    **Important :** Les categories sont automatiquement rafraichies depuis le serveur Xtream au debut de chaque sync (les `is_allowed` existants sont preserves). Le `POST .../categories/refresh` est disponible pour un rafraichissement manuel hors sync.

11. **Filtrage API automatique :** L'API `/api/media/*` filtre automatiquement les medias dont `is_in_allowed_categories = false`. Seuls les medias appartenant a des categories autorisees sont retournes. Ce filtrage est transparent pour l'app Android — aucune logique supplementaire n'est necessaire cote client.

12. **IDs IMDB prefixes :** Tous les `imdbId` sont garantis avec le prefixe `"tt"` (ex: `"tt1375666"`). Le backend ajoute automatiquement le prefixe si la source TMDB ne le fournit pas.

13. **Enrichissement optimise :** Le backend n'enqueue pour enrichissement que les medias ou il manque `tmdb_id` OU `imdb_id`. Si un des deux est deja present (typiquement le `tmdb_id` fourni par Xtream), il ne fait qu'un seul appel API au lieu de deux.

---

## 8. Flux de Donnees Complets

### 8.1 Ajout d'un compte et premier chargement

```
Android                          Backend                         Xtream Server
   │                                │                                │
   │ POST /api/accounts             │                                │
   │ {label, baseUrl, username...}  │                                │
   │ ──────────────────────────────>│                                │
   │                                │  authenticate(account)         │
   │                                │ ──────────────────────────────>│
   │                                │ <──────────────── account info │
   │ <──── 201 AccountResponse      │                                │
   │                                │                                │
   │                                │  [arriere-plan]                │
   │                                │  _refresh_categories()         │
   │                                │ ──────────────────────────────>│
   │                                │ <──── categories VOD+Series    │
   │                                │  UPSERT xtream_categories      │
   │                                │                                │
   │                                │  get_vod_streams()             │
   │                                │ ──────────────────────────────>│
   │                                │ <──────── 144K VOD DTOs        │
   │                                │  get_vod_info() x N (changed)  │
   │                                │ ──────────────────────────────>│
   │                                │ <──────── metadata detaillee   │
   │                                │  INSERT/UPDATE media           │
   │                                │                                │
   │                                │  get_series()                  │
   │                                │ ──────────────────────────────>│
   │                                │  get_series_info() x N         │
   │                                │ ──────────────────────────────>│
   │                                │  INSERT/UPDATE media           │
   │                                │                                │
   │                                │  [enrichissement TMDB]         │
   │                                │  search_movie(title, year)     │
   │                                │ ──────────────────> TMDB API   │
   │                                │  UPDATE tmdb_id, imdb_id       │
   │                                │                                │
   │ GET /api/media/movies          │                                │
   │ ──────────────────────────────>│                                │
   │ <──── MediaListResponse        │                                │
```

### 8.2 Lecture d'un film

```
Android                          Backend                         Xtream Server
   │                                │                                │
   │ GET /api/stream/vod_435071.mp4 │                                │
   │    ?server_id=xtream_05fd75e9  │                                │
   │ ──────────────────────────────>│                                │
   │                                │  parse rating_key              │
   │                                │  build URL avec credentials    │
   │ <──── StreamResponse           │                                │
   │  {url: "http://srv/movie/      │                                │
   │   user/pass/435071.mp4"}       │                                │
   │                                │                                │
   │ ════ Lecture directe via ExoPlayer ═══════════════════════════> │
   │ <════════════════════════════ Stream video ════════════════════ │
```

### 8.3 Navigation Series → Episodes → Lecture

```
Android                          Backend
   │                                │
   │ GET /api/media/shows           │
   │    ?limit=50&sort=added_desc   │
   │ ──────────────────────────────>│
   │ <──── shows list               │
   │                                │
   │  [user clique sur une serie]   │
   │                                │
   │ GET /api/media/episodes        │
   │    ?parent_rating_key=         │
   │     series_6581                │
   │ ──────────────────────────────>│
   │ <──── episodes list            │
   │                                │
   │  [user clique S01E01]          │
   │                                │
   │ GET /api/stream/ep_7890.mkv    │
   │    ?server_id=xtream_05fd75e9  │
   │ ──────────────────────────────>│
   │ <──── StreamResponse {url}     │
   │                                │
   │  [lance ExoPlayer avec l'URL]  │
```

### 8.4 Gestion des categories

**Note :** Les categories sont auto-rafraichies au debut de chaque sync (pas besoin d'appeler `refresh` manuellement). Le `POST .../categories/refresh` reste disponible pour forcer un rafraichissement hors sync.

```
Android                          Backend                         Xtream Server
   │                                │                                │
   │  [premier affichage ou         │                                │
   │   apres un sync]               │                                │
   │                                │                                │
   │ GET /api/accounts/{id}/        │                                │
   │    categories                  │                                │
   │ ──────────────────────────────>│                                │
   │ <──── CategoryListResponse     │                                │
   │  {filterMode, items[]}         │  (categories deja peuplees     │
   │                                │   par le sync automatique)     │
   │                                │                                │
   │  [user configure whitelist :   │                                │
   │   selectionne les categories   │                                │
   │   a autoriser]                 │                                │
   │                                │                                │
   │ PUT /api/accounts/{id}/        │                                │
   │    categories                  │                                │
   │  {filterMode: "whitelist",     │                                │
   │   categories: [celles a        │                                │
   │   autoriser avec isAllowed:    │                                │
   │   true]}                       │                                │
   │ ──────────────────────────────>│                                │
   │                                │  UPDATE category_filter_mode   │
   │                                │  UPDATE listed → is_allowed    │
   │                                │  UPDATE unlisted → false       │
   │ <──── 200 OK                   │  (auto en mode whitelist)      │
   │                                │                                │
   │ POST /api/sync/xtream          │                                │
   │  {accountId: "..."}            │                                │
   │ ──────────────────────────────>│                                │
   │                                │  _refresh_categories()         │
   │                                │ ──────────────────────────────>│
   │                                │ <── categories (preserve       │
   │                                │     is_allowed existants)      │
   │                                │  [sync avec filtrage actif]    │
   │                                │  → skip categories bloquees    │
   │ <──── 202 {jobId}             │                                │
```
