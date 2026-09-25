# Inventaire généré (fastapi)

> ⚙️ **Généré par `tools/retro-spec/inventaire.py` — ne pas éditer à la main.** Base factuelle de la rétro-spécification : chaque ligne porte sa preuve `fichier:ligne`. Régénérer : `python tools/retro-spec/inventaire.py`.

## Endpoints (115)

| Méthode | Chemin | Garde | Fonction | Preuve |
|---|---|---|---|---|
| GET | `/admin` | Basic admin | admin_index | `app/api/admin.py:132` |
| GET | `/admin/` | Basic admin | admin_index | `app/api/admin.py:133` |
| GET | `/admin/catalogue` | Basic admin | admin_catalogue_fragment | `app/api/admin.py:158` |
| GET | `/admin/downloads` | Basic admin | admin_downloads_index | `app/api/admin_downloads.py:156` |
| POST | `/admin/downloads` | Basic admin | admin_downloads_enqueue | `app/api/admin_downloads.py:377` |
| GET | `/admin/downloads/` | Basic admin | admin_downloads_index | `app/api/admin_downloads.py:157` |
| POST | `/admin/downloads/` | Basic admin | admin_downloads_enqueue | `app/api/admin_downloads.py:378` |
| POST | `/admin/downloads/clear-finished` | Basic admin | admin_downloads_clear_finished | `app/api/admin_downloads.py:469` |
| GET | `/admin/downloads/list` | Basic admin | admin_downloads_list_fragment | `app/api/admin_downloads.py:183` |
| GET | `/admin/downloads/queue` | Basic admin | admin_downloads_queue | `app/api/admin_downloads.py:442` |
| POST | `/admin/downloads/{job_id}/cancel` | Basic admin | admin_downloads_cancel | `app/api/admin_downloads.py:449` |
| POST | `/admin/downloads/{job_id}/retry` | Basic admin | admin_downloads_retry | `app/api/admin_downloads.py:459` |
| GET | `/admin/downloads/{type}/{unification_id:path}/episodes` | Basic admin | admin_downloads_episodes | `app/api/admin_downloads.py:288` |
| GET | `/admin/downloads/{type}/{unification_id:path}/versions` | Basic admin | admin_downloads_versions | `app/api/admin_downloads.py:220` |
| GET | `/admin/import-nfo` | Basic admin | admin_import_nfo_form | `app/api/admin.py:798` |
| POST | `/admin/import-nfo` | Basic admin | admin_import_nfo_run | `app/api/admin.py:815` |
| GET | `/admin/keys` | Basic admin | admin_keys | `app/api/admin.py:902` |
| POST | `/admin/keys` | Basic admin | admin_keys_create | `app/api/admin.py:916` |
| GET | `/admin/keys/table` | Basic admin | admin_keys_table | `app/api/admin.py:909` |
| POST | `/admin/keys/{key_id}/revoke` | Basic admin | admin_keys_revoke | `app/api/admin.py:948` |
| POST | `/admin/media/{server_id}/{rating_key}/apply` | Basic admin | admin_media_apply | `app/api/admin.py:299` |
| GET | `/admin/media/{server_id}/{rating_key}/candidates` | Basic admin | admin_media_candidates | `app/api/admin.py:566` |
| POST | `/admin/media/{server_id}/{rating_key}/clear` | Basic admin | admin_media_clear | `app/api/admin.py:440` |
| POST | `/admin/media/{server_id}/{rating_key}/lookup` | Basic admin | admin_media_lookup | `app/api/admin.py:600` |
| GET | `/admin/media/{server_id}/{rating_key}/poster` | Basic admin | admin_media_poster | `app/api/admin.py:492` |
| POST | `/admin/media/{server_id}/{rating_key}/rescrape` | Basic admin | admin_media_rescrape | `app/api/admin.py:462` |
| GET | `/admin/media/{server_id}/{rating_key}/scrape` | Basic admin | admin_media_scrape_panel | `app/api/admin.py:534` |
| POST | `/admin/media/{server_id}/{rating_key}/unlock` | Basic admin | admin_media_unlock | `app/api/admin.py:421` |
| GET | `/admin/movies` | Basic admin | admin_movies_fragment | `app/api/admin.py:176` |
| GET | `/admin/movies/stats` | Basic admin | admin_stats_fragment | `app/api/admin.py:206` |
| POST | `/admin/movies/{rating_key}/ids` | Basic admin | admin_update_ids | `app/api/admin.py:213` |
| POST | `/admin/movies/{rating_key}/rescrape` | Basic admin | admin_rescrape | `app/api/admin.py:261` |
| GET | `/admin/plex-downloads` | Basic admin | admin_plex_downloads_index | `app/api/admin_plex_downloads.py:171` |
| POST | `/admin/plex-downloads` | Basic admin | admin_plex_downloads_enqueue | `app/api/admin_plex_downloads.py:360` |
| GET | `/admin/plex-downloads/` | Basic admin | admin_plex_downloads_index | `app/api/admin_plex_downloads.py:172` |
| POST | `/admin/plex-downloads/` | Basic admin | admin_plex_downloads_enqueue | `app/api/admin_plex_downloads.py:361` |
| GET | `/admin/plex-downloads/list` | Basic admin | admin_plex_downloads_list_fragment | `app/api/admin_plex_downloads.py:204` |
| POST | `/admin/plex-downloads/sync` | Basic admin | admin_plex_downloads_sync | `app/api/admin_plex_downloads.py:422` |
| GET | `/admin/plex-downloads/sync/status` | Basic admin | admin_plex_downloads_sync_status | `app/api/admin_plex_downloads.py:454` |
| GET | `/admin/plex-downloads/{type}/{unification_id:path}/episodes` | Basic admin | admin_plex_downloads_episodes | `app/api/admin_plex_downloads.py:278` |
| GET | `/admin/plex-downloads/{type}/{unification_id:path}/versions` | Basic admin | admin_plex_downloads_versions | `app/api/admin_plex_downloads.py:234` |
| GET | `/admin/review` | Basic admin | admin_review_list | `app/api/admin.py:657` |
| POST | `/admin/review/{server_id}/{rating_key}/apply` | Basic admin | admin_review_apply | `app/api/admin.py:672` |
| POST | `/admin/review/{server_id}/{rating_key}/dismiss` | Basic admin | admin_review_dismiss | `app/api/admin.py:730` |
| POST | `/admin/scrape-batch` | Basic admin | admin_scrape_batch_start | `app/api/admin.py:749` |
| GET | `/admin/scrape-batch/status` | Basic admin | admin_scrape_batch_status | `app/api/admin.py:776` |
| POST | `/admin/scrape-batch/{job_id}/cancel` | Basic admin | admin_scrape_batch_cancel | `app/api/admin.py:787` |
| GET | `/admin/stats` | Basic admin | admin_stats_fragment | `app/api/admin.py:205` |
| GET | `/admin/unified-downloads` | Basic admin | admin_unified_downloads_index | `app/api/admin_unified_downloads.py:97` |
| GET | `/admin/unified-downloads/` | Basic admin | admin_unified_downloads_index | `app/api/admin_unified_downloads.py:98` |
| GET | `/admin/unified-downloads/list` | Basic admin | admin_unified_downloads_list_fragment | `app/api/admin_unified_downloads.py:120` |
| GET | `/api/accounts` | X-API-Key | list_accounts | `app/api/accounts.py:23` |
| POST | `/api/accounts` | X-API-Key | create_account | `app/api/accounts.py:29` |
| DELETE | `/api/accounts/{account_id}` | X-API-Key | delete_account | `app/api/accounts.py:88` |
| PUT | `/api/accounts/{account_id}` | X-API-Key | update_account | `app/api/accounts.py:63` |
| GET | `/api/accounts/{account_id}/categories` | X-API-Key | list_categories | `app/api/categories.py:27` |
| PUT | `/api/accounts/{account_id}/categories` | X-API-Key | update_categories | `app/api/categories.py:50` |
| POST | `/api/accounts/{account_id}/categories/refresh` | X-API-Key | refresh_categories | `app/api/categories.py:82` |
| POST | `/api/accounts/{account_id}/test` | X-API-Key | test_account | `app/api/accounts.py:109` |
| GET | `/api/admin/downloads` | public ? | list_downloads | `app/api/downloads.py:40` |
| GET | `/api/admin/downloads/{job_id}` | public ? | get_download | `app/api/downloads.py:56` |
| GET | `/api/admin/enrichment/jobs/{job_id}` | public ? | get_omdb_backfill_job | `app/api/enrichment.py:104` |
| POST | `/api/admin/enrichment/omdb-backfill` | public ? | start_omdb_backfill | `app/api/enrichment.py:69` |
| GET | `/api/admin/keys` | public ? | list_api_keys | `app/api/api_keys.py:68` |
| POST | `/api/admin/keys` | public ? | create_api_key | `app/api/api_keys.py:74` |
| DELETE | `/api/admin/keys/{key_id}` | public ? | delete_api_key | `app/api/api_keys.py:100` |
| POST | `/api/admin/keys/{key_id}/revoke` | public ? | revoke_api_key | `app/api/api_keys.py:91` |
| GET | `/api/admin/plex-downloads/catalog` | public ? | list_plex_catalog | `app/api/plex_downloads.py:78` |
| GET | `/api/admin/plex-downloads/catalog/{type}/{unification_id:path}` | public ? | get_plex_catalog_group | `app/api/plex_downloads.py:113` |
| GET | `/api/admin/plex-downloads/servers` | public ? | list_plex_servers | `app/api/plex_downloads.py:53` |
| POST | `/api/ai/assistant` | X-API-Key | assistant | `app/api/ai.py:622` |
| POST | `/api/ai/blurb` | X-API-Key | blurb_generate | `app/api/ai.py:1093` |
| GET | `/api/ai/blurb/{tmdb_id}` | X-API-Key | blurb_get | `app/api/ai.py:1216` |
| POST | `/api/ai/chat` | X-API-Key | chat | `app/api/ai.py:883` |
| POST | `/api/ai/describe` | X-API-Key | describe | `app/api/ai.py:860` |
| GET | `/api/ai/embed/jobs/{job_id}` | X-API-Key | embed_job_status | `app/api/ai.py:729` |
| POST | `/api/ai/embed/rebuild` | X-API-Key | embed_rebuild | `app/api/ai.py:713` |
| GET | `/api/ai/embed/status` | X-API-Key | embed_status | `app/api/ai.py:745` |
| GET | `/api/ai/llm/status` | X-API-Key | llm_status | `app/api/ai.py:909` |
| POST | `/api/ai/rank` | X-API-Key | rank | `app/api/ai.py:320` |
| POST | `/api/ai/rank-multi` | X-API-Key | rank_multi | `app/api/ai.py:401` |
| POST | `/api/ai/search` | X-API-Key | search | `app/api/ai.py:499` |
| POST | `/api/ai/subtitles/translate` | X-API-Key | translate_subtitle | `app/api/ai.py:948` |
| GET | `/api/health` | public ? | health_check | `app/api/health.py:14` |
| GET | `/api/live/channels` | X-API-Key | list_channels | `app/api/live.py:28` |
| GET | `/api/live/channels/{stream_id}` | X-API-Key | get_channel | `app/api/live.py:86` |
| GET | `/api/live/channels/{stream_id}/epg` | X-API-Key | get_channel_epg | `app/api/live.py:143` |
| GET | `/api/live/channels/{stream_id}/stream` | X-API-Key | get_channel_stream | `app/api/live.py:105` |
| GET | `/api/live/epg` | X-API-Key | get_epg_now | `app/api/live.py:201` |
| GET | `/api/media/episodes` | X-API-Key | list_episodes | `app/api/media.py:205` |
| GET | `/api/media/episodes/unified` | X-API-Key | list_episodes_unified | `app/api/media.py:394` |
| GET | `/api/media/movies` | X-API-Key | list_movies | `app/api/media.py:128` |
| GET | `/api/media/movies/stats` | X-API-Key | movies_stats | `app/api/media.py:164` |
| GET | `/api/media/movies/unified` | X-API-Key | list_movies_unified | `app/api/media.py:266` |
| GET | `/api/media/shows` | X-API-Key | list_shows | `app/api/media.py:172` |
| GET | `/api/media/shows/unified` | X-API-Key | list_shows_unified | `app/api/media.py:333` |
| GET | `/api/media/trailer/file/{cache_key}` | X-API-Key | get_trailer_file | `app/api/media.py:475` |
| GET | `/api/media/trailer/resolve` | X-API-Key | resolve_trailer | `app/api/media.py:440` |
| GET | `/api/media/{rating_key}` | X-API-Key | get_media | `app/api/media.py:506` |
| PATCH | `/api/media/{rating_key}` | X-API-Key | update_media | `app/api/media.py:518` |
| POST | `/api/media/{rating_key}/rescrape` | X-API-Key | rescrape_media | `app/api/media.py:540` |
| POST | `/api/plex/generate` | X-API-Key | generate_plex_library | `app/api/plex.py:81` |
| GET | `/api/stream/{rating_key}` | X-API-Key | get_stream | `app/api/stream.py:13` |
| DELETE | `/api/sync/cancel/{task_name}` | X-API-Key | cancel_sync | `app/api/sync.py:71` |
| POST | `/api/sync/enrichment` | X-API-Key | trigger_enrichment | `app/api/sync.py:83` |
| POST | `/api/sync/full-pipeline` | X-API-Key | trigger_full_pipeline | `app/api/sync.py:129` |
| GET | `/api/sync/jobs` | X-API-Key | list_sync_jobs | `app/api/sync.py:209` |
| GET | `/api/sync/status/{job_id}` | X-API-Key | get_sync_status | `app/api/sync.py:181` |
| POST | `/api/sync/validate-streams` | X-API-Key | trigger_stream_validation | `app/api/sync.py:106` |
| POST | `/api/sync/xtream` | X-API-Key | trigger_sync | `app/api/sync.py:29` |
| POST | `/api/sync/xtream/all` | X-API-Key | trigger_sync_all | `app/api/sync.py:48` |
| POST | `/api/tv-auth/approve` | public ? | approve | `app/api/tv_auth.py:382` |
| POST | `/api/tv-auth/complete` | public ? | complete | `app/api/tv_auth.py:557` |
| POST | `/api/tv-auth/start` | public ? | start | `app/api/tv_auth.py:222` |
| GET | `/api/tv-auth/status` | public ? | get_status | `app/api/tv_auth.py:451` |

## Tâches planifiées (APScheduler, 7)

| Fonction | Id | Déclencheur | Preuve |
|---|---|---|---|
| `scheduled_sync_enrich_generate` | sync_enrich_generate | interval | `app/main.py:357` |
| `track_job` |  |  | `app/main.py:366` |
| `track_job` |  |  | `app/main.py:375` |
| `track_job` |  |  | `app/main.py:390` |
| `track_job` |  |  | `app/main.py:410` |
| `track_job` |  |  | `app/main.py:427` |
| `track_job` |  |  | `app/main.py:459` |

## Workers

`app/workers/download_worker.py` · `app/workers/embedding_worker.py` · `app/workers/enrichment_backfill_worker.py` · `app/workers/enrichment_worker.py` · `app/workers/health_check_worker.py` · `app/workers/manual_scrape_batch_worker.py` · `app/workers/sync_worker.py`

## Services (33)

`account_outage_service` · `account_service` · `aggregation_service` · `api_key_service` · `category_service` · `download_nfo` · `download_service` · `embedding_service` · `job_registry` · `live_service` · `manual_scrape_service` · `media_identity_writer` · `media_service` · `nfo_import_service` · `ollama_service` · `omdb_scrape_cache_service` · `omdb_service` · `plex_api_service` · `plex_catalog_service` · `plex_download_service` · `plex_generation_service` · `plex_sync_service` · `poster_match_service` · `recommendation_service` · `scrape_cache_service` · `stream_service` · `subtitle_service` · `tmdb_service` · `trailer_service` · `unified_download_catalog_service` · `unified_group_service` · `xtream_credentials` · `xtream_service`

## Tables (37 déclarations)

| Table | Source | Preuve |
|---|---|---|
| `xtream_categories` | migrations | `app/db/migrations.py:92` |
| `live_channels` | migrations | `app/db/migrations.py:219` |
| `epg_entries` | migrations | `app/db/migrations.py:257` |
| `ai_tmdb_cache` | migrations | `app/db/migrations.py:326` |
| `tv_auth_sessions` | migrations | `app/db/migrations.py:358` |
| `tmdb_scrape_cache` | migrations | `app/db/migrations.py:413` |
| `ai_subtitle_cache` | migrations | `app/db/migrations.py:445` |
| `ai_media_blurb` | migrations | `app/db/migrations.py:479` |
| `media_group` | migrations | `app/db/migrations.py:746` |
| `media_group_member` | migrations | `app/db/migrations.py:756` |
| `download_batch` | migrations | `app/db/migrations.py:807` |
| `download_job` | migrations | `app/db/migrations.py:821` |
| `plex_server` | migrations | `app/db/migrations.py:888` |
| `plex_media_item` | migrations | `app/db/migrations.py:904` |
| `plex_sync_status` | migrations | `app/db/migrations.py:947` |
| `omdb_scrape_cache` | migrations | `app/db/migrations.py:1050` |
| `scrape_review` | migrations | `app/db/migrations.py:1293` |
| `media` | ORM | `app/models/database.py:15` |
| `xtream_accounts` | ORM | `app/models/database.py:146` |
| `xtream_categories` | ORM | `app/models/database.py:179` |
| `live_channels` | ORM | `app/models/database.py:197` |
| `epg_entries` | ORM | `app/models/database.py:242` |
| `tv_auth_sessions` | ORM | `app/models/database.py:279` |
| `ai_subtitle_cache` | ORM | `app/models/database.py:309` |
| `ai_media_blurb` | ORM | `app/models/database.py:329` |
| `enrichment_queue` | ORM | `app/models/database.py:341` |
| `tmdb_scrape_cache` | ORM | `app/models/database.py:373` |
| `omdb_scrape_cache` | ORM | `app/models/database.py:399` |
| `api_keys` | ORM | `app/models/database.py:427` |
| `media_group` | ORM | `app/models/database.py:463` |
| `media_group_member` | ORM | `app/models/database.py:492` |
| `download_batch` | ORM | `app/models/database.py:511` |
| `download_job` | ORM | `app/models/database.py:543` |
| `plex_server` | ORM | `app/models/database.py:596` |
| `plex_media_item` | ORM | `app/models/database.py:629` |
| `plex_sync_status` | ORM | `app/models/database.py:692` |
| `scrape_review` | ORM | `app/models/database.py:722` |

## Configuration (`app/config.py`, noms seuls — aucune valeur)

`TMDB_API_KEY` · `OMDB_API_KEY` · `OMDB_DAILY_LIMIT` · `AI_API_KEY` · `ADMIN_USERNAME` · `ADMIN_PASSWORD` · `METRICS_USERNAME` · `METRICS_PASSWORD` · `METRICS_PUBLIC` · `TV_AUTH_ENCRYPTION_KEY` · `TV_AUTH_TTL_SECONDS` · `TV_AUTH_START_RATE_LIMIT_MAX` · `TV_AUTH_START_RATE_LIMIT_WINDOW_SECONDS` · `TV_AUTH_PENDING_SESSIONS_CAP_PER_IP` · `TV_AUTH_PENDING_SESSIONS_CAP` · `XTREAM_ENCRYPTION_KEY` · `AI_EMBED_CACHE_DIR` · `AI_EMBED_MODEL` · `DATA_DIR` · `LOG_DIR` · `DB_PATH` · `SYNC_INTERVAL_HOURS` · `ENRICHMENT_DAILY_LIMIT` · `HEALTH_CHECK_BATCH_SIZE` · `STREAM_VALIDATION_ENABLED` · `STREAM_VALIDATION_CONCURRENCY` · `STREAM_VALIDATION_TIMEOUT` · `STREAM_BROKEN_THRESHOLD` · `STREAM_VALIDATION_RECHECK_HOURS` · `STREAM_FILTER_BROKEN` · `ACCOUNT_OUTAGE_STRIKES` · `ACCOUNT_OUTAGE_MASK_ENABLED` · `PLEX_LIBRARY_DIR` · `PLEX_FORCE_REFRESH_METADATA` · `BACKUP_ENABLED` · `BACKUP_DIR` · `BACKUP_RETENTION_DAYS` · `BACKUP_HOUR` · `CORS_ORIGINS` · `TMDB_LANGUAGE` · `OLLAMA_URL` · `OLLAMA_MODEL` · `SUBTITLE_CHUNK_CUES` · `SUBTITLE_CONCURRENCY` · `SUBTITLE_MAX_CUES` · `SUBTITLE_MAX_BYTES` · `SUBTITLE_PER_CHUNK_TIMEOUT` · `SUBTITLE_TOTAL_TIMEOUT` · `SUBTITLE_CACHE_RETENTION_DAYS` · `XTREAM_BASE_URL` · `XTREAM_PORT` · `XTREAM_USERNAME` · `XTREAM_PASSWORD` · `XTREAM_USER_AGENT` · `ADULT_CONTENT_RATING` · `ADULT_CATEGORY_KEYWORDS` · `ADULT_CATEGORY_IDS` · `DOWNLOAD_DIR` · `DOWNLOAD_CONCURRENCY` · `DOWNLOAD_CHUNK_BYTES` · `DOWNLOAD_MAX_RETRIES` · `DOWNLOAD_MIN_FREE_DISK_MB` · `DOWNLOAD_POLL_INTERVAL` · `DOWNLOAD_CONNECT_TIMEOUT` · `DOWNLOAD_READ_TIMEOUT` · `DOWNLOAD_MAX_REDIRECTS` · `UNIFIED_DOWNLOAD_MERGE_CAP` · `PLEX_ACCOUNT_TOKEN` · `PLEX_PROBE_TIMEOUT` · `PLEX_SYNC_INTERVAL_HOURS` · `PLEX_CLIENT_IDENTIFIER` · `TRAILER_CACHE_DIR` · `TRAILER_CACHE_MAX_MB` · `TRAILER_MIN_FREE_DISK_MB` · `DAV_ENABLED` · `DAV_USERNAME` · `DAV_PASSWORD` · `DAV_MOVIE_LIMIT` · `DAV_SERIES_LIMIT` · `DAV_ACCOUNT_IDS` · `DAV_INCLUDE_ADULT` · `DAV_SINGLE_VERSION` · `DAV_REQUIRE_KNOWN_SIZE` · `DAV_TREE_TTL_MINUTES` · `DAV_UPSTREAM_PER_ACCOUNT` · `DAV_QUEUE_TIMEOUT_SECONDS` · `DAV_RANGE_SHIM` · `DAV_CONNECT_TIMEOUT` · `DAV_READ_TIMEOUT` · `SCRAPE_REBUILD_DEBOUNCE_SECONDS` · `SCRAPE_INTERACTIVE_POSTER_CANDIDATES` · `SCRAPE_BATCH_POSTER_CANDIDATES` · `SCRAPE_BATCH_TMDB_LIMIT` · `SCRAPE_BATCH_MAX_ITEMS` · `SCRAPE_BATCH_CONCURRENCY` · `SCRAPE_BATCH_POSTER_ONLY_AUTO` · `POSTER_PHASH_IDENTICAL` · `POSTER_DHASH_IDENTICAL` · `POSTER_PHASH_CLOSE` · `POSTER_MAX_VARIANTS` · `POSTER_MAX_BYTES` · `POSTER_MAX_PIXELS` · `POSTER_FETCH_TIMEOUT` · `POSTER_CONCURRENCY` · `POSTER_HASH_CONCURRENCY` · `POSTER_PER_HOST_CONCURRENCY` · `POSTER_GENERIC_STDDEV` · `POSTER_GENERIC_MIN_REPEAT` · `SSRF_ALLOW_PRIVATE_HOSTS` · `SSRF_DNS_CACHE_SECONDS`


## Templates admin (27)

`.claude/templates/prd-template.md` · `app/templates/admin/_downloads_episodes.html` · `app/templates/admin/_downloads_list.html` · `app/templates/admin/_downloads_queue.html` · `app/templates/admin/_downloads_versions.html` · `app/templates/admin/_key_created.html` · `app/templates/admin/_keys_table.html` · `app/templates/admin/_media_row.html` · `app/templates/admin/_media_table.html` · `app/templates/admin/_plex_downloads_episodes.html` · `app/templates/admin/_plex_downloads_list.html` · `app/templates/admin/_plex_downloads_versions.html` · `app/templates/admin/_plex_sync_status.html` · `app/templates/admin/_review_list.html` · `app/templates/admin/_review_row_result.html` · `app/templates/admin/_scrape_batch_status.html` · `app/templates/admin/_scrape_candidates.html` · `app/templates/admin/_scrape_panel.html` · `app/templates/admin/_stats.html` · `app/templates/admin/_unified_downloads_list.html` · `app/templates/admin/base.html` · `app/templates/admin/downloads.html` · `app/templates/admin/import_nfo.html` · `app/templates/admin/index.html` · `app/templates/admin/keys.html` · `app/templates/admin/plex_downloads.html` · `app/templates/admin/unified_downloads.html`


Fichiers de test : 150.
