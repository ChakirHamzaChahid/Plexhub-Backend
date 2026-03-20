# PlexHub Backend Design — Python FastAPI (Xtream Only)

**Date:** 2026-02-27
**Status:** Approved

## Context

PlexHub TV Android handles Plex autonomously (server scan, metadata, playback). This backend is an optional module dedicated exclusively to Xtream content. Users without Xtream accounts don't need it.

**Backend objectives:**
- Synchronize Xtream catalogs (VOD + Series) and store them locally
- Enrich each media with TMDB/IMDb IDs (budget 50K requests/day) for aggregation with Plex on Android via `unificationId`
- Probe stream URLs nightly to detect broken streams
- Serve pre-enriched media to Android via REST API
- Manage Xtream accounts via CRUD API (add/remove from Android Settings)

**Deployment:** Docker single-container on Raspberry Pi / NUC / home server. No auth (LAN-only). SQLite WAL (no PostgreSQL).

## Architecture

```
PlexHub TV Android
├── Module Plex (always active) — scan, metadata, direct playback
└── Module Backend (optional) — GET /api/media/*, GET /api/stream/*, aggregation via unificationId
    │
    ▼
plexhub-backend (LAN, optional)
├── FastAPI + SQLite WAL
├── Xtream sync (catalog → DB)
├── TMDB enrichment (tmdb_id, imdb_id → unificationId)
├── Health check (nightly stream probing)
├── REST API (media, stream, accounts, sync, health)
└── Accounts CRUD
```

### User Profiles

| Profile | Plex | Backend | Aggregation |
|---------|------|---------|-------------|
| Plex only | Direct Android | Not needed | Plex only |
| Plex + Xtream | Direct Android | LAN backend | Fusion via unificationId |
| Xtream only | No | LAN backend | Xtream only |

## Project Structure

```
plexhub-backend/
├── app/
│   ├── __init__.py
│   ├── main.py                        # FastAPI app, lifespan, CORS, scheduler
│   ├── config.py                      # Settings (env vars, TMDB key, DB path)
│   ├── api/
│   │   ├── __init__.py
│   │   ├── media.py                   # GET /api/media/*
│   │   ├── sync.py                    # POST /api/sync/*, GET /api/sync/status/*
│   │   ├── stream.py                  # GET /api/stream/{ratingKey}
│   │   ├── accounts.py                # CRUD /api/accounts
│   │   └── health.py                  # GET /api/health
│   ├── services/
│   │   ├── __init__.py
│   │   ├── xtream_service.py          # Xtream API client (player_api.php)
│   │   ├── tmdb_service.py            # TMDB API client (search + details)
│   │   ├── media_service.py           # Business logic: CRUD, pagination, filtering
│   │   └── stream_service.py          # Build Xtream stream URLs
│   ├── workers/
│   │   ├── __init__.py
│   │   ├── sync_worker.py             # Background Xtream catalog sync
│   │   ├── enrichment_worker.py       # Background TMDB enrichment
│   │   └── health_check_worker.py     # Background stream health probing
│   ├── models/
│   │   ├── __init__.py
│   │   ├── database.py                # SQLite ORM models (SQLAlchemy)
│   │   └── schemas.py                 # Pydantic request/response schemas
│   ├── db/
│   │   ├── __init__.py
│   │   └── database.py                # SQLAlchemy engine + session factory (aiosqlite)
│   └── utils/
│       ├── __init__.py
│       ├── string_normalizer.py       # Title normalization
│       ├── unification.py             # unificationId calculation
│       └── cache.py                   # SharedSqliteCache
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
└── .env.example
```

## Database Schema (SQLite WAL)

### Table `media` — Mirror of Android MediaEntity (schema 33)

Flat schema (no JSON blobs) for efficient SQL queries. Aligned exactly with PlexHub TV Android Room MediaEntity.

**Composite Primary Key:** `(rating_key, server_id, filter, sort_order)`

Key columns:
- Core metadata: title, type (movie/show/episode), year, duration, summary, genres
- Playback state: view_offset, view_count, last_viewed_at
- Hierarchy: parent_title, parent_rating_key, grandparent_title, grandparent_rating_key
- External IDs: guid, imdb_id, tmdb_id
- Unification: unification_id, history_group_key
- Display: display_rating, resolved_thumb_url, resolved_art_url
- Backend-specific: stream_error_count, last_stream_check, is_broken, tmdb_match_confidence

### Table `xtream_accounts`

**Primary Key:** `id` (MD5(baseUrl+username)[:8])

Stores Xtream credentials, connection info, sync state, and account status.

### Table `enrichment_queue`

Tracks TMDB enrichment progress per media item with status (pending/processing/done/failed/skipped), attempt count, and error tracking.

## ID Generation Rules (must match Android XtreamMediaMapper)

| Type | ratingKey Format | Example |
|------|-----------------|---------|
| Movie | `vod_{streamId}.{ext}` or `vod_{streamId}` | `vod_435071.mp4` |
| Series | `series_{seriesId}` | `series_6581` |
| Episode | `ep_{episodeId}.{ext}` or `ep_{episodeId}` | `ep_7890.mkv` |
| Season | `season_{seriesId}_{seasonNum}` | `season_6581_1` |

- **serverId:** `xtream_{accountId}` (e.g., `xtream_a1b2c3d4`)
- **librarySectionId:** `xtream_vod` for movies, `xtream_series` for series/episodes

## unificationId — Critical for Plex/Xtream Aggregation

Priority: `imdb://` > `tmdb://` > `title_{normalized}_{year}`

Must match Android MediaMapper logic exactly for cross-source aggregation to work.

## Sync Worker

- Runs every 6 hours (matches Android LibrarySyncWorker)
- Full sync: fetch categories + streams from Xtream API → map to media rows → differential cleanup
- Differential cleanup: delete DB rows absent from API response (delisted content)
- Inserts unenriched items into enrichment_queue

## Enrichment Worker

- Runs every 6 hours (after catalog sync)
- Budget: 50K TMDB requests/day
- Phase 1 (VOD): Try Xtream `get_vod_info` first (free, may contain tmdb_id), fallback to TMDB Search
- Phase 2 (Series): TMDB Search only (Xtream doesn't provide tmdb_id for series)
- Fuzzy title matching via rapidfuzz (threshold >= 0.85)
- Updates unification_id to `imdb://` or `tmdb://` format

## Health Check Worker

- Nightly at 2 AM
- HTTP HEAD probe on stream URLs
- 1000 streams per run (full catalog covered in ~20 days)
- Marks broken streams (`is_broken = true`)

## REST API (camelCase JSON responses)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/media/movies` | GET | List movies (paginated) |
| `/api/media/shows` | GET | List shows (paginated) |
| `/api/media/episodes` | GET | List episodes by parent |
| `/api/media/{ratingKey}` | GET | Single media item |
| `/api/stream/{ratingKey}` | GET | Get stream URL |
| `/api/accounts` | GET/POST | List/add accounts |
| `/api/accounts/{id}` | PUT/DELETE | Update/delete account |
| `/api/accounts/{id}/test` | POST | Test connection |
| `/api/sync/xtream` | POST | Trigger sync for account |
| `/api/sync/xtream/all` | POST | Trigger sync for all |
| `/api/sync/status/{jobId}` | GET | Check sync progress |
| `/api/health` | GET | Backend health status |

## Docker Deployment

Single container, SQLite in `/app/data/plexhub.db`, 512MB memory limit, 1 CPU. Uvicorn with master worker election via lock file.

## Implementation Order

1. Scaffold (FastAPI, config, Docker, SQLite, lifespan)
2. DB models (SQLAlchemy ORM)
3. Xtream service (API client)
4. Sync worker (catalog → DB)
5. API endpoints (media, stream, accounts, health)
6. TMDB service (search + external IDs)
7. Enrichment worker (batch TMDB)
8. Health check worker (stream probing)
9. Scheduling (APScheduler integration)
