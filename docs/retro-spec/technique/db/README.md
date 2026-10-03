> ⚙️ Généré par `tools/retro-spec/schema-db.py` (tbls) — ne pas éditer. Source : schéma de la base locale `data/plexhub.db` (user_version 0), lu dans sqlite_master uniquement — tbls sur Docker distant. Aucune donnée : base reconstruite vide.

> Non recréés (module SQLite absent) : table ai_embeddings (no such module: vec0)

# schema.db

## Tables

| Name                                                              | Columns | Comment | Type  |
| ----------------------------------------------------------------- | ------- | ------- | ----- |
| [ai_embeddings_chunks](ai_embeddings_chunks.md)                   | 4       |         | table |
| [ai_embeddings_info](ai_embeddings_info.md)                       | 2       |         | table |
| [ai_embeddings_rowids](ai_embeddings_rowids.md)                   | 4       |         | table |
| [ai_embeddings_vector_chunks00](ai_embeddings_vector_chunks00.md) | 2       |         | table |
| [ai_media_blurb](ai_media_blurb.md)                               | 7       |         | table |
| [ai_subtitle_cache](ai_subtitle_cache.md)                         | 7       |         | table |
| [ai_tmdb_cache](ai_tmdb_cache.md)                                 | 8       |         | table |
| [api_keys](api_keys.md)                                           | 9       |         | table |
| [download_batch](download_batch.md)                               | 9       |         | table |
| [download_job](download_job.md)                                   | 19      |         | table |
| [enrichment_queue](enrichment_queue.md)                           | 14      |         | table |
| [epg_entries](epg_entries.md)                                     | 10      |         | table |
| [live_channels](live_channels.md)                                 | 17      |         | table |
| [media](media.md)                                                 | 68      |         | table |
| [media_group](media_group.md)                                     | 5       |         | table |
| [media_group_member](media_group_member.md)                       | 4       |         | table |
| [omdb_scrape_cache](omdb_scrape_cache.md)                         | 4       |         | table |
| [plex_media_item](plex_media_item.md)                             | 26      |         | table |
| [plex_server](plex_server.md)                                     | 11      |         | table |
| [plex_sync_status](plex_sync_status.md)                           | 5       |         | table |
| [tmdb_scrape_cache](tmdb_scrape_cache.md)                         | 8       |         | table |
| [tv_auth_sessions](tv_auth_sessions.md)                           | 11      |         | table |
| [xtream_accounts](xtream_accounts.md)                             | 16      |         | table |
| [xtream_categories](xtream_categories.md)                         | 7       |         | table |

## Relations

```mermaid
erDiagram


"ai_embeddings_chunks" {
  INTEGER chunk_id
  INTEGER size
  BLOB validity
  BLOB rowids
}
"ai_embeddings_info" {
  TEXT key PK
  ANY value
}
"ai_embeddings_rowids" {
  INTEGER rowid
   id
  INTEGER chunk_id
  INTEGER chunk_offset
}
"ai_embeddings_vector_chunks00" {
   rowid PK
  BLOB vectors
}
"ai_media_blurb" {
  INTEGER tmdb_id PK
  TEXT media_type PK
  TEXT lang PK
  TEXT summary
  TEXT tags
  TEXT model
  BIGINT created_at
}
"ai_subtitle_cache" {
  TEXT cache_key PK
  TEXT target_lang
  TEXT model
  TEXT source_format
  INTEGER cue_count
  TEXT translated_content
  BIGINT created_at
}
"ai_tmdb_cache" {
  INTEGER tmdb_id
  TEXT imdb_id
  TEXT media_type
  TEXT title
  TEXT overview
  TEXT genres
  INTEGER fetched_at
  INTEGER embedded_at
}
"api_keys" {
  TEXT id PK
  TEXT key_hash
  TEXT key_prefix
  TEXT label
  BIGINT created_at
  BIGINT expires_at
  BIGINT revoked_at
  BIGINT last_used_at
  TEXT last_used_ip
}
"download_batch" {
  TEXT id PK
  TEXT media_type
  TEXT unification_id
  TEXT title
  TEXT server_id
  TEXT rating_key
  TEXT scope
  INTEGER total_jobs
  BIGINT created_at
}
"download_job" {
  TEXT id PK
  TEXT batch_id
  TEXT server_id
  TEXT rating_key
  TEXT media_type
  TEXT unification_id
  TEXT title
  INTEGER season
  INTEGER episode
  TEXT dest_path
  TEXT state
  BIGINT bytes_total
  BIGINT bytes_done
  TEXT error
  INTEGER attempts
  BIGINT created_at
  BIGINT updated_at
  BIGINT started_at
  BIGINT finished_at
}
"enrichment_queue" {
  INTEGER id
  TEXT rating_key
  TEXT server_id
  TEXT media_type
  TEXT title
  INTEGER year
  TEXT status
  INTEGER attempts
  TEXT last_error
  BIGINT created_at
  BIGINT processed_at
  TEXT existing_tmdb_id
  TEXT existing_imdb_id
  TEXT existing_summary
}
"epg_entries" {
  INTEGER id
  TEXT server_id
  TEXT epg_channel_id
  INTEGER stream_id
  TEXT title
  TEXT description
  BIGINT start_time
  BIGINT end_time
  TEXT lang
  BIGINT fetched_at
}
"live_channels" {
  INTEGER stream_id PK
  TEXT server_id PK
  TEXT name
  TEXT name_sortable
  TEXT stream_icon
  TEXT epg_channel_id
  TEXT category_id
  TEXT container_extension
  TEXT custom_sid
  BOOLEAN tv_archive
  INTEGER tv_archive_duration
  BOOLEAN is_adult
  BOOLEAN is_active
  BOOLEAN is_in_allowed_categories
  BIGINT added_at
  BIGINT updated_at
  TEXT dto_hash
}
"media" {
  TEXT rating_key PK
  TEXT server_id PK
  TEXT filter PK
  TEXT sort_order PK
  TEXT library_section_id
  TEXT title
  TEXT title_sortable
  INTEGER page_offset
  TEXT type
  TEXT thumb_url
  TEXT art_url
  INTEGER year
  INTEGER duration
  TEXT summary
  TEXT genres
  TEXT content_rating
  INTEGER view_offset
  INTEGER view_count
  BIGINT last_viewed_at
  TEXT parent_title
  TEXT parent_rating_key
  INTEGER parent_index
  TEXT grandparent_title
  TEXT grandparent_rating_key
  INTEGER index
  TEXT parent_thumb
  TEXT grandparent_thumb
  TEXT media_parts
  TEXT guid
  TEXT imdb_id
  TEXT tmdb_id
  FLOAT rating
  FLOAT audience_rating
  TEXT unification_id
  TEXT history_group_key
  TEXT server_ids
  TEXT rating_keys
  BIGINT added_at
  BIGINT updated_at
  FLOAT display_rating
  FLOAT scraped_rating
  TEXT resolved_thumb_url
  TEXT resolved_art_url
  TEXT resolved_base_url
  TEXT alternative_thumb_urls
  INTEGER stream_error_count
  BIGINT last_stream_check
  BOOLEAN is_broken
  FLOAT tmdb_match_confidence
  TEXT content_hash
  TEXT dto_hash
  INTEGER is_in_allowed_categories
  TEXT cast
  INTEGER is_adult
  TEXT original_title
  TEXT tagline
  TEXT premiered
  TEXT status
  TEXT studio
  TEXT country
  TEXT tvdb_id
  TEXT wikidata_id
  REAL imdb_rating
  INTEGER imdb_votes
  REAL tmdb_rating
  INTEGER tmdb_votes
  TEXT cast_json
  BIGINT file_size
}
"media_group" {
  TEXT media_type PK
  TEXT group_key PK
  BIGINT sort_added_at
  INTEGER version_count
  BIGINT built_at
}
"media_group_member" {
  TEXT media_type PK
  TEXT group_key PK
  TEXT server_id PK
  TEXT rating_key PK
}
"omdb_scrape_cache" {
  TEXT imdb_id PK
  TEXT result
  TEXT payload
  BIGINT fetched_at
}
"plex_media_item" {
  TEXT server_id PK
  TEXT rating_key PK
  TEXT type
  TEXT title
  INTEGER year
  TEXT parent_rating_key
  TEXT grandparent_rating_key
  INTEGER parent_index
  INTEGER index
  TEXT imdb_id
  TEXT tmdb_id
  TEXT tvdb_id
  TEXT unification_id
  TEXT genres
  TEXT thumb_url
  BIGINT added_at
  INTEGER height
  INTEGER width
  TEXT video_codec
  TEXT audio_codec
  TEXT container
  INTEGER bitrate
  TEXT part_key
  BIGINT part_size
  BIGINT duration_ms
  BIGINT synced_at
}
"plex_server" {
  TEXT client_identifier PK
  TEXT name
  TEXT owner_title
  BOOLEAN owned
  TEXT access_token
  TEXT base_uri
  BOOLEAN is_reachable
  BIGINT last_synced_at
  TEXT last_sync_error
  BIGINT created_at
  BIGINT updated_at
}
"plex_sync_status" {
  INTEGER id
  TEXT state
  BIGINT started_at
  BIGINT finished_at
  TEXT error
}
"tmdb_scrape_cache" {
  TEXT cache_key PK
  TEXT media_type
  TEXT result
  TEXT tmdb_id
  TEXT imdb_id
  FLOAT confidence
  TEXT payload
  BIGINT fetched_at
}
"tv_auth_sessions" {
  TEXT id PK
  TEXT device_code
  TEXT user_code
  TEXT status
  TEXT payload_encrypted
  BOOLEAN payload_delivered
  TEXT device_name
  BIGINT created_at
  BIGINT expires_at
  BIGINT approved_at
  BIGINT completed_at
}
"xtream_accounts" {
  TEXT id PK
  TEXT label
  TEXT base_url
  INTEGER port
  TEXT username
  TEXT password
  TEXT status
  BIGINT expiration_date
  INTEGER max_connections
  TEXT allowed_formats
  TEXT server_url
  INTEGER https_port
  BIGINT last_synced_at
  BOOLEAN is_active
  BIGINT created_at
  TEXT category_filter_mode
}
"xtream_categories" {
  INTEGER id
  TEXT account_id
  TEXT category_id
  TEXT category_type
  TEXT category_name
  BOOLEAN is_allowed
  BIGINT last_fetched_at
}
```

---

> Generated by [tbls](https://github.com/k1LoW/tbls)
