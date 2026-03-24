# Category Filtering Feature

## Overview

The PlexHub Backend supports category-based filtering for Xtream IPTV content. This allows users to select which **VOD**, **Series**, and **Live TV** categories to sync, reducing database pollution and improving performance.

## Key Features

1. **Flexible Filter Modes**
   - `all`: Sync all categories (default)
   - `whitelist`: Only sync explicitly allowed categories
   - `blacklist`: Sync all except explicitly blocked categories

2. **Per-Account Configuration**
   - Each Xtream account has independent category settings
   - Categories are discovered automatically from the Xtream provider
   - Settings persist across syncs

3. **Conservation Strategy**
   - Filtered items are marked but not deleted (`is_in_allowed_categories=False`)
   - Easy to restore content by changing filter configuration
   - Differential cleanup handles delisted content separately

4. **Optimized Enrichment**
   - Enrichment triggers when TMDB **OR** IMDB is missing (not both)
   - Minimizes TMDB API calls by only fetching missing data
   - Guaranteed 'tt' prefix for all IMDB IDs

## API Endpoints

### Get Categories
```http
GET /api/accounts/{account_id}/categories
```

Response:
```json
{
  "items": [
    {
      "categoryId": "123",
      "categoryType": "vod",
      "categoryName": "Movies",
      "isAllowed": true,
      "lastFetchedAt": 1709251200000
    }
  ],
  "filterMode": "whitelist"
}
```

### Update Category Configuration
```http
PUT /api/accounts/{account_id}/categories
```

Body:
```json
{
  "filterMode": "whitelist",
  "categories": [
    {
      "categoryId": "123",
      "categoryType": "vod",
      "isAllowed": true
    }
  ]
}
```

### Refresh Categories from Provider
```http
POST /api/accounts/{account_id}/categories/refresh
```

Fetches current categories from Xtream provider and updates the database.

## Database Schema Changes

### New Table: `xtream_categories`
- `account_id`: Account identifier
- `category_id`: Category ID from Xtream
- `category_type`: "vod", "series", or "live"
- `category_name`: Human-readable name
- `is_allowed`: Boolean flag for whitelist/blacklist
- `last_fetched_at`: Timestamp of last fetch

### Updated Tables

**`xtream_accounts`**
- Added `category_filter_mode`: "all", "whitelist", or "blacklist"

**`media`**
- Added `is_in_allowed_categories`: Boolean flag for visibility

**`live_channels`** (new table)
- `is_in_allowed_categories`: Boolean flag for visibility (same logic as media)

**`enrichment_queue`**
- Added `existing_tmdb_id`: TMDB ID present before enrichment
- Added `existing_imdb_id`: IMDB ID present before enrichment

## Sync Behavior

### VOD Sync
1. Load category configuration
2. Fetch VOD streams from Xtream
3. Skip streams from disallowed categories
4. Mark synced items as `is_in_allowed_categories=True`
5. Enqueue for enrichment if TMDB or IMDB missing

### Series Sync
1. Load category configuration
2. Fetch series from Xtream
3. Skip series from disallowed categories
4. Mark series and episodes as `is_in_allowed_categories=True`
5. Episodes inherit parent series' category status

### Live Channel Sync
1. Load category configuration (mode + allowed live categories)
2. Fetch live streams from Xtream (`get_live_streams`)
3. Skip channels from disallowed categories
4. Hash-based incremental sync (skip unchanged channels)
5. Mark synced channels as `is_in_allowed_categories=True`
6. Differential cleanup removes delisted channels (mode `all` only)

## Enrichment Optimization

The enrichment worker now handles 4 scenarios:

1. **Both IDs present**: Skip (filtered by enqueue)
2. **TMDB present, IMDB absent**: Fetch external_ids only (1 API call)
3. **IMDB present, TMDB absent**: Keep IMDB, skip TMDB search
4. **Both absent**: Full search + external_ids (2 API calls)

This reduces TMDB API usage by up to 50% for partially enriched content.

## Series-Episode Fix

The media service now correctly handles series-episode queries:

```
GET /api/media/episodes?parent_rating_key=series_6336
```

Auto-detects series queries (prefix: "series_") and filters by `grandparent_rating_key` instead of `parent_rating_key`, fixing the episode hierarchy.

## IMDB ID Format

All IMDB IDs now guaranteed to have 'tt' prefix in:
- `unification_id` field (format: `imdb://tt12345`)
- TMDB API responses
- Enrichment results

This ensures consistent cross-platform identification with Android app.

## Migration Notes

- Migrations run automatically on startup
- Existing media defaults to `is_in_allowed_categories=True`
- Existing accounts default to `category_filter_mode='all'`
- No data loss - all existing content remains visible

## Performance Impact

- **Sync**: Faster due to category filtering (skip unwanted content)
- **API**: Reduced TMDB calls through optimized enrichment
- **Database**: No significant overhead (indexed columns)
- **Android**: Faster queries via `is_in_allowed_categories` index
