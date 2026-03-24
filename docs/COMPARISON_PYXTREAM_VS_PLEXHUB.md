# Comparaison : pyxtream vs Plexhub-Backend

## Vue d'ensemble

| Critère | pyxtream | Plexhub-Backend |
|---|---|---|
| **Type** | Bibliothèque Python (client) | Application backend complète (serveur) |
| **Objectif** | Client générique pour l'API Xtream Codes | Pont Xtream → Plex avec enrichissement TMDB |
| **Licence** | GPL-3.0 | — |
| **Framework** | Aucun (library standalone) | FastAPI |
| **HTTP** | `requests` (synchrone) | `httpx` (asynchrone) |
| **Base de données** | Aucune (cache fichier JSON) | SQLite (SQLAlchemy async) |
| **Architecture** | Classe monolithique `XTream` | Services modulaires (API / Workers / Services) |

---

## 1. Périmètre fonctionnel

### pyxtream
- Client **bas niveau** pour l'API Xtream Codes
- Charge tout le contenu en mémoire (live, VOD, séries)
- Recherche regex dans les flux
- Téléchargement de vidéos avec reprise
- EPG (guide de programmes) complet
- Cache fichier local avec TTL (8h par défaut)
- Filtrage du contenu adulte
- API REST optionnelle via Flask (basique)

### Plexhub-Backend
- Application **haut niveau** orientée Plex
- Synchronisation incrémentale avec détection de changements (hash)
- Enrichissement TMDB (métadonnées, posters, casting, notes)
- Génération de bibliothèque Plex (.strm + .nfo + images)
- Filtrage par catégories (whitelist/blacklist)
- API REST complète (comptes, médias, catégories, sync, streaming)
- Vérification de santé des flux (health check)
- Multi-comptes Xtream
- Planification de tâches (APScheduler)

---

## 2. Gestion de l'API Xtream Codes

### Endpoints utilisés

| Endpoint Xtream | pyxtream | Plexhub-Backend |
|---|:---:|:---:|
| Authentification | ✅ | ✅ |
| `get_live_categories` | ✅ | ✅ |
| `get_live_streams` | ✅ | ✅ (incrémental, hash-based) |
| `get_vod_categories` | ✅ | ✅ |
| `get_vod_streams` | ✅ (tout d'un coup) | ✅ (par catégorie) |
| `get_vod_info` | ✅ | ✅ (si hash changé) |
| `get_series_categories` | ✅ | ✅ |
| `get_series` | ✅ (tout d'un coup) | ✅ (par catégorie) |
| `get_series_info` | ✅ (lazy) | ✅ |
| EPG (short/full/XML) | ✅ | ✅ (short EPG + XMLTV) |

**Différence clé** : Plexhub gère désormais le **live TV** (chaînes, catégories, EPG) en plus du VOD et des séries. Son implémentation est asynchrone et incrémentale, contrairement à pyxtream qui charge tout en mémoire d'un coup.

### Stratégie de chargement

| Aspect | pyxtream | Plexhub-Backend |
|---|---|---|
| Méthode | `load_iptv()` — charge tout | Sync incrémental par catégorie |
| Détail séries | Lazy (appel séparé par série) | Systématique lors du sync |
| Détection de changements | Aucune (recharge tout) | Hash-based (évite les appels inutiles) |
| Requêtes par catégorie | Optionnel | Par défaut (réduit la charge) |

---

## 3. Architecture

### pyxtream — Monolithique

```
XTream (classe unique)
├── authenticate()
├── load_iptv()
│   ├── _load_categories_from_provider()
│   ├── _load_streams_from_provider()
│   └── _load_streams_by_category_from_provider()
├── search_stream()
├── download_video()
├── vodInfoByID()
├── get_series_info_by_id()
├── liveEpgByStream()
└── allEpg()

Modèles : Channel, Group, Serie, Season, Episode
Cache : fichiers JSON locaux
```

### Plexhub-Backend — Modulaire

```
FastAPI Application
├── API Layer (REST endpoints)
│   ├── accounts, media, stream, categories
│   ├── live (chaînes + EPG)
│   ├── sync, plex, health
│
├── Services Layer (logique métier)
│   ├── xtream_service (client API)
│   ├── tmdb_service (enrichissement)
│   ├── media_service (requêtes DB)
│   ├── stream_service (URLs de streaming)
│   └── category_service (filtrage)
│
├── Workers (tâches de fond)
│   ├── sync_worker (synchronisation Xtream)
│   ├── enrichment_worker (enrichissement TMDB)
│   └── health_check_worker (vérification flux)
│
├── Plex Generator
│   ├── generator, source, storage
│   ├── naming, nfo_builder, mapping
│
└── Data Layer
    ├── SQLAlchemy models (Media, LiveChannel, EpgEntry, Account, Category, EnrichmentQueue)
    └── SQLite database
```

---

## 4. Modèle de données

### pyxtream
- `Channel` : id, name, logo, url, group_title, is_adult, raw JSON
- `Group` : name, group_id, group_type, channels[], series[]
- `Serie` : name, series_id, logo, plot, genre
- `Season` / `Episode` : containers basiques

→ Tout est **en mémoire**, pas de persistance structurée.

### Plexhub-Backend
- `Media` : rating_key, title, year, type, thumb_url, summary, genres, duration, rating, guid, imdb_id, tmdb_id, unification_id, is_in_allowed_categories, is_broken…
- `LiveChannel` : stream_id, name, stream_icon, epg_channel_id, category_id, tv_archive, is_adult, dto_hash…
- `EpgEntry` : epg_channel_id, stream_id, title, description, start_time, end_time, lang
- `XtreamAccount` : credentials, status, expiration, config
- `XtreamCategory` : account_id, category_id, category_type ("vod"/"series"/"live"), is_allowed
- `EnrichmentQueue` : status pipeline (pending → processing → done/failed)

→ Tout est **persisté en base** avec des index optimisés.

---

## 5. Performance et scalabilité

| Aspect | pyxtream | Plexhub-Backend |
|---|---|---|
| **I/O** | Synchrone (`requests`) | Asynchrone (`httpx` + `asyncio`) |
| **Concurrence** | Aucune | Sémaphore + workers concurrents |
| **Cache** | Fichier JSON (TTL 8h) | Base SQLite + hash de changement |
| **Mémoire** | Tout en RAM | Streaming + pagination DB |
| **Multi-comptes** | Non (1 instance = 1 serveur) | Oui (N comptes en parallèle) |
| **Scalabilité** | Limité (mono-thread) | Master-worker election, horizontal |

---

## 6. Fonctionnalités exclusives

### Uniquement dans pyxtream
- **Téléchargement vidéo** avec reprise
- **Recherche regex** dans les flux
- **Validation JSON Schema** des réponses API
- **Filtrage adulte** intégré

### Uniquement dans Plexhub-Backend
- **Live TV** avec sync incrémental, EPG on-demand, et filtrage par catégories
- **EPG** (guide de programmes) avec cache DB et fetch short EPG + XMLTV
- **Enrichissement TMDB** (métadonnées, posters, casting, notes)
- **Génération Plex** (.strm, .nfo, poster.jpg, fanart.jpg)
- **Filtrage par catégories** (whitelist/blacklist)
- **Sync incrémental** avec hash-based change detection
- **Multi-comptes** Xtream
- **Health check** des flux
- **API REST complète** pour gestion et navigation
- **Unification de contenu** (même film de plusieurs sources)
- **Parsing intelligent** des titres IPTV (nettoyage préfixes, extraction année)
- **Docker** ready avec docker-compose

---

## 7. Dépendances

### pyxtream
```
requests
jsonschema
flask (optionnel)
```

### Plexhub-Backend
```
fastapi, uvicorn          # Framework web
sqlalchemy, aiosqlite     # Base de données
httpx                     # Client HTTP async
pydantic, pydantic-settings
apscheduler               # Planification
rapidfuzz                 # Matching flou (TMDB)
python-dotenv, typer
```

---

## 8. Limitations comparées

### pyxtream
1. **Synchrone** — bloque pendant le chargement
2. **Pas de persistance** — tout doit être rechargé
3. **Pas de sync incrémental** — recharge tout à chaque fois
4. **Pas d'enrichissement** — uniquement les données du fournisseur
5. **Mono-compte** — une instance par serveur
6. **Bug potentiel** avec les class-level mutable defaults
7. **Petite communauté** (~113 téléchargements/semaine)

### Plexhub-Backend
1. **Pas de téléchargement** — streaming uniquement (.strm)
4. **SQLite uniquement** — pas de PostgreSQL/MySQL
5. **Pas d'authentification API** — conçu pour usage local

---

## 9. Conclusion

| | pyxtream | Plexhub-Backend |
|---|---|---|
| **Utilisation idéale** | Script/app qui a besoin d'un client Xtream simple | Serveur qui transforme Xtream en bibliothèque Plex enrichie |
| **Complexité** | Faible (1 fichier principal) | Élevée (architecture complète) |
| **Valeur ajoutée** | Abstraction propre de l'API Xtream | Pipeline complet : sync → enrichissement → Plex |

**pyxtream** est une **bibliothèque cliente** : elle encapsule l'API Xtream Codes dans des objets Python. C'est un outil de base.

**Plexhub-Backend** est une **application complète** qui va bien au-delà : il synchronise, enrichit, filtre, et génère une bibliothèque Plex. Son client Xtream (`xtream_service.py`) remplit un rôle similaire à pyxtream mais de manière asynchrone, incrémentale, et intégrée dans un pipeline plus large.

En résumé, pyxtream pourrait techniquement être utilisé *à la place* de `xtream_service.py`, mais Plexhub-Backend n'en a pas besoin car :
- Il utilise `httpx` (async) vs `requests` (sync)
- Il fait du sync incrémental (pyxtream recharge tout)
- Il couvre désormais **live TV + EPG** en plus du VOD/séries
- Son service est intégré au reste de l'architecture (DB, workers, enrichissement)

Le support Live IPTV a été ajouté en s'inspirant des fonctionnalités de pyxtream (endpoints Xtream live, modèle Channel, EPG), mais avec l'architecture asynchrone, le sync incrémental et le filtrage par catégories propres à Plexhub-Backend.
