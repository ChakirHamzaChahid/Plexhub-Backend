# Plexhub Backend

FastAPI backend for the PlexHub stack: Plex/Xtream library mirroring, NFO ingestion,
TMDB metadata enrichment, and AI-powered media recommendations.

## AI Recommendations

The backend exposes a stateless ranking API over media identifiers (`tmdb_id` and/or `imdb_id`), consumed by the PlexHubTV Android app. The backend stores no user history: it caches TMDB metadata and embeddings only, keyed by `tmdb_id`.

### Endpoints (all require `X-API-Key`)

- `POST /api/ai/rank` — item-to-item ranking ("you might also like X")
- `POST /api/ai/rank-multi` — centroid-to-item ranking ("for you", weighted history)
- `GET /api/ai/embed/status` — diagnostic snapshot (counts, model loaded, RSS)
- `POST /api/ai/embed/rebuild` — trigger background re-embedding of pending rows (202 + jobId)
- `GET /api/ai/embed/jobs/{jobId}` — poll a rebuild job

### Configuration

| Env var | Default | Purpose |
|---|---|---|
| `AI_API_KEY` | (empty) | Required to enable AI endpoints. Empty -> all routes return 503. |
| `TMDB_API_KEY` | (empty) | Reused for the imdb->tmdb resolution via `/find`. |

### Three 503 motifs

| Detail | Cause |
|---|---|
| `AI service not configured` | `AI_API_KEY` is empty |
| `AI vector storage unavailable` | `sqlite-vec` extension failed to load |
| `AI model unavailable` | fastembed model failed to download or initialize |

### Operational notes

- **Cold start ~30 s on first `/rank`**: fastembed downloads ~150 MB of ONNX weights (`intfloat/multilingual-e5-small`, 384 dim). Subsequent calls are fast.
- **The rebuild never auto-runs at boot**. It only executes via `POST /api/ai/embed/rebuild` (R5).
- **Container memory**: bumped from 1 G to 2 G to fit the model + ONNX runtime.
- **Episodes are not supported (C.4)**: the service ranks at the show level (`tv`) or movie level (`movie`) only. IMDb ids that resolve to episodes, seasons, or persons are counted in `resolutionFailed` and dropped. The Android app must rank at the parent show and play episodes locally via `parentRatingKey`/`grandparentRatingKey`.
- **Cap on cache misses per request**: at most 20 fresh TMDB fetches per `/rank` call. The remainder are reported in `cacheMissesDropped`; the client can re-call once the cache warms.
- **Rebuild is idempotent**: scans `ai_tmdb_cache WHERE embedded_at IS NULL` with cursor pagination (`tmdb_id > :cursor`), DELETE-then-INSERT on the `vec0` virtual table.

### Migration M008

Idempotent (every DDL uses `IF NOT EXISTS`). Adds two tables:
- `ai_embeddings` — `vec0` virtual table, `FLOAT[384]` keyed by `tmdb_id`
- `ai_tmdb_cache` — `tmdb_id` (PK), `imdb_id`, `media_type` (CHECK movie|tv), `title`, `overview`, `genres`, `fetched_at`, `embedded_at`
