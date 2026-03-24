import asyncio
import hashlib
import json
import logging

from sqlalchemy import select, delete, update
from sqlalchemy.dialects.sqlite import insert as sqlite_upsert

from app.db.database import async_session_factory
from app.models.database import Media, XtreamAccount, EnrichmentQueue, LiveChannel
from app.services.xtream_service import xtream_service
from app.utils.string_normalizer import (
    parse_title_and_year,
    normalize_for_sorting,
    parse_rating,
)
from app.utils.unification import (
    calculate_unification_id,
    calculate_history_group_key,
    calculate_display_rating,
)
from app.utils.time import now_ms

logger = logging.getLogger("plexhub.sync")

# In-memory sync job tracking
_sync_jobs: dict[str, dict] = {}


def _safe_duration(value) -> int | None:
    """Convert episode_run_time (minutes) to milliseconds, or None on failure."""
    if not value:
        return None
    try:
        return int(value) * 60_000
    except (ValueError, TypeError):
        return None


def map_vod_to_media(dto: dict, account_id: str, index: int, vod_info: dict | None = None) -> dict:
    """Map Xtream VOD stream DTO to media row dict."""
    title, year = parse_title_and_year(dto.get("name") or "Unknown")
    ext = (dto.get("container_extension") or "").strip() or None
    stream_id = dto["stream_id"]
    rating_key = f"vod_{stream_id}.{ext}" if ext else f"vod_{stream_id}"
    server_id = f"xtream_{account_id}"
    rating_val = parse_rating(dto.get("rating"))

    # Extract detailed info if available (defensive: handle list responses)
    if vod_info and not isinstance(vod_info, dict):
        logger.warning(f"Unexpected vod_info type for stream {stream_id}: {type(vod_info).__name__}")

    # Extract info and movie_data, ensuring they are dicts
    info = vod_info.get("info", {}) if vod_info and isinstance(vod_info, dict) else {}
    if not isinstance(info, dict):
        # Some Xtream APIs return info as a list - skip if not a dict
        info = {}

    movie_data = vod_info.get("movie_data", {}) if vod_info and isinstance(vod_info, dict) else {}
    if not isinstance(movie_data, dict):
        movie_data = {}

    # Debug logging (first 3 items only to avoid spam)
    if index < 3 and vod_info and isinstance(vod_info, dict):
        logger.info(f"[DEBUG] Stream {stream_id} ({title}):")
        logger.info(f"  vod_info keys: {list(vod_info.keys())}")
        logger.info(f"  info keys: {list(info.keys())}")
        logger.info(f"  info sample: plot={info.get('plot')[:50] if info.get('plot') else None}, "
                   f"genre={info.get('genre')}, duration={info.get('duration')}, "
                   f"duration_secs={info.get('duration_secs')}")

    # Prefer detailed info fields over basic stream fields
    if info:
        if info.get("releasedate"):
            try:
                year = int(info["releasedate"][:4])
            except (ValueError, TypeError):
                pass
        rating_val = parse_rating(info.get("rating")) or rating_val

    # Duration: handle both numeric (seconds) and HH:MM:SS formats
    duration_raw = info.get("duration_secs") or info.get("duration")
    duration_ms = None
    if duration_raw:
        try:
            # Try numeric first (seconds as string or int)
            duration_ms = int(duration_raw) * 1000
        except (ValueError, TypeError):
            # Try HH:MM:SS format
            if isinstance(duration_raw, str) and ":" in duration_raw:
                parts = duration_raw.split(":")
                if len(parts) == 3:
                    try:
                        hours, mins, secs = int(parts[0]), int(parts[1]), int(parts[2])
                        duration_ms = (hours * 3600 + mins * 60 + secs) * 1000
                    except (ValueError, TypeError):
                        pass

    # Genres
    genres = info.get("genre") or None

    # Summary (plot or description)
    summary = info.get("plot") or info.get("description") or None

    # Content rating (age or mpaa_rating)
    content_rating = info.get("mpaa_rating") or info.get("age") or None

    # TMDB ID
    tmdb_id_str = info.get("tmdb_id") or info.get("tmdb")
    tmdb_id_int = int(tmdb_id_str) if tmdb_id_str and str(tmdb_id_str).isdigit() else None

    # Thumb/Art
    thumb_url = info.get("movie_image") or info.get("cover_big") or dto.get("stream_icon")
    art_url = info.get("backdrop_path") or None
    if isinstance(art_url, list) and art_url:
        art_url = art_url[0]

    unification_id = calculate_unification_id(
        title, year, tmdb_id=str(tmdb_id_int) if tmdb_id_int else None
    )

    # Debug logging - show extracted values
    if index < 3:
        logger.info(f"  Extracted: duration_ms={duration_ms}, summary={summary[:50] if summary else None}, "
                   f"genres={genres}, content_rating={content_rating}, tmdb_id={tmdb_id_int}")

    row = {
        "rating_key": rating_key,
        "server_id": server_id,
        "library_section_id": "xtream_vod",
        "title": title,
        "title_sortable": normalize_for_sorting(title).lower(),
        "filter": str(dto.get("category_id", "all")),
        "sort_order": "default",
        "page_offset": index,
        "type": "movie",
        "thumb_url": thumb_url,
        "art_url": art_url,
        "resolved_thumb_url": thumb_url,
        "resolved_art_url": art_url,
        "year": year,
        "duration": duration_ms,
        "summary": summary,
        "genres": genres,
        "content_rating": content_rating,
        "rating": rating_val,
        "display_rating": rating_val or 0.0,
        "tmdb_id": tmdb_id_int,
        "added_at": int(dto.get("added") or 0) * 1000,  # seconds -> ms
        "updated_at": now_ms(),
        "unification_id": unification_id,
        "history_group_key": calculate_history_group_key(
            unification_id, rating_key, server_id
        ),
        "media_parts": "[]",
    }

    return row


def map_series_to_media(dto: dict, account_id: str, index: int) -> dict:
    """Map Xtream series DTO to media row dict."""
    title, year = parse_title_and_year(dto.get("name") or "Unknown")
    series_id = dto["series_id"]
    rating_key = f"series_{series_id}"
    server_id = f"xtream_{account_id}"
    rating_val = parse_rating(dto.get("rating"))
    backdrop = dto.get("backdrop_path")

    unification_id = calculate_unification_id(title, year)
    return {
        "rating_key": rating_key,
        "server_id": server_id,
        "library_section_id": "xtream_series",
        "title": title,
        "title_sortable": normalize_for_sorting(title).lower(),
        "filter": str(dto.get("category_id", "all")),
        "sort_order": "default",
        "page_offset": index,
        "type": "show",
        "thumb_url": dto.get("cover"),
        "art_url": backdrop[0] if isinstance(backdrop, list) and backdrop else None,
        "resolved_thumb_url": dto.get("cover"),
        "resolved_art_url": backdrop[0] if isinstance(backdrop, list) and backdrop else None,
        "year": year,
        "summary": dto.get("plot"),
        "genres": dto.get("genre"),
        "duration": _safe_duration(dto.get("episode_run_time")),
        "rating": rating_val,
        "display_rating": rating_val or 0.0,
        "added_at": now_ms(),
        "updated_at": now_ms(),
        "unification_id": unification_id,
        "history_group_key": calculate_history_group_key(
            unification_id, rating_key, server_id
        ),
        "media_parts": "[]",
    }


def _build_media_parts(rating_key: str, ext: str | None, info: dict) -> str:
    """Build mediaParts JSON from Xtream episode/VOD info (video + audio streams)."""
    video = info.get("video")
    audio = info.get("audio")

    if not video and not audio:
        return "[]"

    streams = []

    if isinstance(video, dict):
        streams.append({
            "type": "VideoStream",
            "id": str(video.get("index", 0)),
            "index": video.get("index", 0),
            "codec": video.get("codec_name"),
            "width": video.get("width"),
            "height": video.get("height"),
            "bitrate": int(video["bit_rate"]) if video.get("bit_rate") else None,
            "selected": True,
            "hasHDR": False,
        })

    if isinstance(audio, dict):
        tags = audio.get("tags") or {}
        lang_code = tags.get("language") if isinstance(tags, dict) else None
        channels = audio.get("channels")
        channel_layout = audio.get("channel_layout")
        streams.append({
            "type": "AudioStream",
            "id": str(audio.get("index", 1)),
            "index": audio.get("index", 1),
            "codec": audio.get("codec_name"),
            "channels": channels,
            "language": lang_code,
            "languageCode": lang_code,
            "title": tags.get("title") if isinstance(tags, dict) else None,
            "displayTitle": f"{audio.get('codec_name', '').upper()} {channel_layout or ''}"
                .strip() if audio.get("codec_name") else None,
            "selected": True,
        })

    duration_ms = None
    if info.get("duration_secs"):
        try:
            duration_ms = int(info["duration_secs"]) * 1000
        except (ValueError, TypeError):
            pass

    part = {
        "id": rating_key,
        "key": f"/stream/{rating_key}",
        "duration": duration_ms,
        "file": None,
        "size": None,
        "container": ext,
        "streams": streams,
    }

    return json.dumps([part])


def map_episode_to_media(
    episode: dict, series_dto: dict, account_id: str, season_num: int,
) -> dict:
    """Map Xtream episode DTO to media row dict."""
    series_id = series_dto["series_id"]
    ep_id = str(episode.get("id", ""))
    ext = (episode.get("container_extension") or "").strip() or None
    rating_key = f"ep_{ep_id}.{ext}" if ext else f"ep_{ep_id}"
    server_id = f"xtream_{account_id}"
    ep_num = episode.get("episode_num")
    info = episode.get("info") or {}

    rating_val = parse_rating(info.get("rating"))

    return {
        "rating_key": rating_key,
        "server_id": server_id,
        "library_section_id": "xtream_series",
        "title": episode.get("title") or f"Episode {ep_num}",
        "title_sortable": (episode.get("title") or f"Episode {ep_num}").lower(),
        "filter": "all",
        "sort_order": "default",
        "page_offset": int(ep_id) if ep_id.isdigit() else (ep_num or 0),
        "type": "episode",
        "thumb_url": info.get("movie_image"),
        "resolved_thumb_url": info.get("movie_image"),
        "year": None,
        "summary": info.get("plot"),
        "duration": (int(info["duration_secs"]) * 1000)
        if info.get("duration_secs")
        else None,
        "parent_rating_key": f"season_{series_id}_{season_num}",
        "parent_title": f"Season {season_num}",
        "parent_index": season_num,
        "grandparent_rating_key": f"series_{series_id}",
        "grandparent_title": series_dto.get("name"),
        "index": ep_num,
        "rating": rating_val,
        "display_rating": rating_val or 0.0,
        "added_at": now_ms(),
        "updated_at": now_ms(),
        "unification_id": "",
        "history_group_key": f"{rating_key}{server_id}",
        "media_parts": _build_media_parts(rating_key, ext, info),
    }


def map_live_stream_to_channel(dto: dict, account_id: str) -> dict:
    """Map Xtream live stream DTO to live_channels row dict."""
    from app.utils.string_normalizer import normalize_for_sorting

    stream_id = dto["stream_id"]
    name = dto.get("name") or "Unknown"
    server_id = f"xtream_{account_id}"

    return {
        "stream_id": stream_id,
        "server_id": server_id,
        "name": name,
        "name_sortable": normalize_for_sorting(name).lower(),
        "stream_icon": dto.get("stream_icon"),
        "epg_channel_id": dto.get("epg_channel_id") or None,
        "category_id": str(dto.get("category_id", "")),
        "container_extension": (dto.get("container_extension") or "ts").strip() or "ts",
        "custom_sid": dto.get("custom_sid") or None,
        "tv_archive": bool(dto.get("tv_archive", 0)),
        "tv_archive_duration": int(dto.get("tv_archive_duration") or 0),
        "is_adult": bool(dto.get("is_adult", 0)),
        "is_active": True,
        "is_in_allowed_categories": True,
        "added_at": int(dto.get("added") or 0) * 1000,
        "updated_at": now_ms(),
    }


def _compute_live_dto_hash(dto: dict) -> str:
    """Hash basic live stream DTO fields to detect changes."""
    fields = {k: dto.get(k) for k in (
        "name", "stream_icon", "epg_channel_id", "category_id",
        "tv_archive", "tv_archive_duration", "is_adult",
        "container_extension", "custom_sid",
    )}
    return hashlib.md5(json.dumps(fields, sort_keys=True, default=str).encode()).hexdigest()


async def upsert_live_channels_batch(db, rows: list[dict]):
    """Bulk upsert live channel rows."""
    if not rows:
        return
    for row in rows:
        stmt = sqlite_upsert(LiveChannel).values(**row)
        update_fields = {
            k: v for k, v in row.items()
            if k not in ("stream_id", "server_id")
        }
        stmt = stmt.on_conflict_do_update(
            index_elements=["stream_id", "server_id"],
            set_=update_fields,
            where=LiveChannel.dto_hash != row.get("dto_hash"),
        )
        await db.execute(stmt)


async def differential_cleanup_live(
    db, server_id: str, api_stream_ids: set[int],
):
    """Remove DB live channels not present in the API response."""
    result = await db.execute(
        select(LiveChannel.stream_id).where(
            LiveChannel.server_id == server_id,
        )
    )
    existing_ids = {row[0] for row in result}
    stale_ids = existing_ids - api_stream_ids

    if stale_ids:
        stale_list = list(stale_ids)
        chunk_size = 500
        for i in range(0, len(stale_list), chunk_size):
            chunk = stale_list[i : i + chunk_size]
            await db.execute(
                delete(LiveChannel).where(
                    LiveChannel.stream_id.in_(chunk),
                    LiveChannel.server_id == server_id,
                )
            )
        logger.info(f"Removed {len(stale_ids)} stale live channels from {server_id}")


_HASH_EXCLUDE = {
    "rating_key", "server_id", "filter", "sort_order",
    "view_offset", "view_count", "last_viewed_at",
    "content_hash", "dto_hash", "updated_at",
}


def _compute_dto_hash(dto: dict) -> str:
    """Hash basic VOD DTO fields to detect changes without calling get_vod_info."""
    fields = {k: dto.get(k) for k in (
        "name", "added", "stream_icon", "rating",
        "category_id", "container_extension",
    )}
    return hashlib.md5(json.dumps(fields, sort_keys=True, default=str).encode()).hexdigest()


def _compute_series_dto_hash(dto: dict) -> str:
    """Hash basic series DTO fields to detect changes without calling get_series_info."""
    fields = {k: dto.get(k) for k in (
        "name", "cover", "plot", "genre", "rating",
        "category_id", "backdrop_path", "episode_run_time", "last_modified",
    )}
    return hashlib.md5(json.dumps(fields, sort_keys=True, default=str).encode()).hexdigest()


def _compute_content_hash(row: dict) -> str:
    """MD5 hash of sync-provided fields to detect changes."""
    hashable = {k: v for k, v in sorted(row.items()) if k not in _HASH_EXCLUDE}
    return hashlib.md5(json.dumps(hashable, default=str).encode()).hexdigest()


async def upsert_media_batch(db, rows: list[dict]):
    """Bulk upsert media rows, skipping UPDATE when content unchanged."""
    if not rows:
        return

    # Compute content hashes
    for row in rows:
        row["content_hash"] = _compute_content_hash(row)

    logger.debug(f"Upserting {len(rows)} media items to database")
    for row in rows:
        # Evict any existing row occupying the same pagination slot
        # but with a different rating_key (content shifted position)
        await db.execute(
            delete(Media).where(
                Media.server_id == row["server_id"],
                Media.library_section_id == row["library_section_id"],
                Media.filter == row["filter"],
                Media.sort_order == row["sort_order"],
                Media.page_offset == row["page_offset"],
                Media.rating_key != row["rating_key"],
            )
        )
        stmt = sqlite_upsert(Media).values(**row)
        update_fields = {
            k: v for k, v in row.items()
            if k not in ("rating_key", "server_id", "filter", "sort_order",
                         "view_offset", "view_count", "last_viewed_at")
        }
        stmt = stmt.on_conflict_do_update(
            index_elements=["rating_key", "server_id", "filter", "sort_order"],
            set_=update_fields,
            where=Media.content_hash != row["content_hash"],
        )
        await db.execute(stmt)


async def enqueue_for_enrichment(db, rows: list[dict]):
    """Insert items into enrichment_queue if either tmdb_id or imdb_id is missing.

    Saves existing IDs to enable optimized enrichment that only fetches missing data.
    """
    for row in rows:
        if row["type"] not in ("movie", "show"):
            continue
        
        # Get existing IDs
        existing_tmdb = row.get("tmdb_id")
        existing_imdb = row.get("imdb_id")
        
        # Skip only if BOTH IDs are present
        if existing_tmdb and existing_imdb:
            continue
        
        # Enqueue with existing IDs saved
        stmt = sqlite_upsert(EnrichmentQueue).values(
            rating_key=row["rating_key"],
            server_id=row["server_id"],
            media_type=row["type"],
            title=row["title"],
            year=row.get("year"),
            status="pending",
            attempts=0,
            created_at=now_ms(),
            existing_tmdb_id=existing_tmdb,
            existing_imdb_id=existing_imdb,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["rating_key", "server_id"],
            set_={
                "status": "pending",
                "existing_tmdb_id": existing_tmdb,
                "existing_imdb_id": existing_imdb,
            }
        )
        await db.execute(stmt)

async def differential_cleanup(
    db, server_id: str, filter_val: str, api_rating_keys: set[str],
    media_type: str = None,
):
    """Remove DB items not present in the API response (delisted content).

    When filter_val is "all", compares ALL items for this server_id/media_type
    regardless of their individual filter (category_id) value.
    Otherwise, only compares items with the specific filter value.
    """
    query = select(Media.rating_key).where(
        Media.server_id == server_id,
    )
    # Only filter by category when not syncing all categories
    if filter_val != "all":
        query = query.where(Media.filter == filter_val)
    if media_type:
        query = query.where(Media.type == media_type)

    result = await db.execute(query)
    existing_keys = {row[0] for row in result}
    stale_keys = existing_keys - api_rating_keys

    if stale_keys:
        stale_list = list(stale_keys)
        chunk_size = 500  # SQLite limit is 999 bind variables
        for i in range(0, len(stale_list), chunk_size):
            chunk = stale_list[i : i + chunk_size]
            await db.execute(
                delete(Media).where(
                    Media.rating_key.in_(chunk),
                    Media.server_id == server_id,
                )
            )
        logger.info(
            f"Removed {len(stale_keys)} stale items from {server_id}/{filter_val}"
            f" (type={media_type or 'all'})"
        )




async def _load_category_config(db, account_id: str) -> tuple[str, dict, dict]:
    """
    Load category filtering configuration for an account.
    
    Returns:
        Tuple of (filter_mode, allowed_vod_categories, allowed_series_categories)
        where allowed_*_categories are dicts mapping category_id -> is_allowed
    """
    from app.models.database import XtreamAccount, XtreamCategory
    
    # Get filter mode
    result = await db.execute(
        select(XtreamAccount.category_filter_mode).where(
            XtreamAccount.id == account_id
        )
    )
    filter_mode = result.scalar_one_or_none() or "all"
    
    # Get category configuration
    result = await db.execute(
        select(XtreamCategory).where(XtreamCategory.account_id == account_id)
    )
    categories = result.scalars().all()
    
    allowed_vod = {}
    allowed_series = {}
    
    for cat in categories:
        if cat.category_type == "vod":
            allowed_vod[cat.category_id] = cat.is_allowed
        elif cat.category_type == "series":
            allowed_series[cat.category_id] = cat.is_allowed
    
    return filter_mode, allowed_vod, allowed_series


def _should_sync_category(
    category_id: str,
    filter_mode: str,
    allowed_categories: dict,
) -> bool:
    """
    Check if a category should be synced based on filter configuration.
    
    Args:
        category_id: Category ID from Xtream
        filter_mode: "all", "whitelist", or "blacklist"
        allowed_categories: Dict mapping category_id -> is_allowed
    
    Returns:
        True if category should be synced, False otherwise
    """
    if filter_mode == "all":
        return True
    
    # If category not in config, default behavior depends on mode
    is_allowed = allowed_categories.get(category_id)
    
    if filter_mode == "whitelist":
        # Only sync if explicitly allowed
        return is_allowed is True
    elif filter_mode == "blacklist":
        # Sync unless explicitly blocked
        return is_allowed is not False
    
    return True  # Default: sync
async def _refresh_categories(db, account, account_id: str):
    """Fetch categories from Xtream and upsert into xtream_categories table."""
    from app.models.database import XtreamCategory
    from sqlalchemy.dialects.sqlite import insert as sqlite_upsert

    try:
        vod_cats = await xtream_service.get_vod_categories(account)
        series_cats = await xtream_service.get_series_categories(account)
        live_cats = await xtream_service.get_live_categories(account)
    except Exception as e:
        logger.warning(f"Failed to fetch categories for account {account_id}: {e}")
        return

    now = now_ms()
    count = 0

    for cat in live_cats:
        if not isinstance(cat, dict):
            continue
        cat_id = str(cat.get("category_id", ""))
        cat_name = cat.get("category_name", "Unknown")
        if not cat_id:
            continue
        stmt = sqlite_upsert(XtreamCategory).values(
            account_id=account_id,
            category_id=cat_id,
            category_type="live",
            category_name=cat_name,
            is_allowed=True,
            last_fetched_at=now,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["account_id", "category_id", "category_type"],
            set_={"category_name": cat_name, "last_fetched_at": now},
        )
        await db.execute(stmt)
        count += 1

    for cat in vod_cats:
        if not isinstance(cat, dict):
            continue
        cat_id = str(cat.get("category_id", ""))
        cat_name = cat.get("category_name", "Unknown")
        if not cat_id:
            continue
        stmt = sqlite_upsert(XtreamCategory).values(
            account_id=account_id,
            category_id=cat_id,
            category_type="vod",
            category_name=cat_name,
            is_allowed=True,
            last_fetched_at=now,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["account_id", "category_id", "category_type"],
            set_={"category_name": cat_name, "last_fetched_at": now},
        )
        await db.execute(stmt)
        count += 1

    for cat in series_cats:
        if not isinstance(cat, dict):
            continue
        cat_id = str(cat.get("category_id", ""))
        cat_name = cat.get("category_name", "Unknown")
        if not cat_id:
            continue
        stmt = sqlite_upsert(XtreamCategory).values(
            account_id=account_id,
            category_id=cat_id,
            category_type="series",
            category_name=cat_name,
            is_allowed=True,
            last_fetched_at=now,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["account_id", "category_id", "category_type"],
            set_={"category_name": cat_name, "last_fetched_at": now},
        )
        await db.execute(stmt)
        count += 1

    await db.commit()
    logger.info(f"Refreshed {count} categories for account {account_id} "
                f"({len(live_cats)} Live, {len(vod_cats)} VOD, {len(series_cats)} Series)")


async def sync_account(account_id: str):
    """Full sync for a single Xtream account."""
    job_id = f"sync_{account_id}_{now_ms()}"
    _sync_jobs[job_id] = {"status": "processing", "progress": {}}

    try:
        async with async_session_factory() as db:
            # Load account
            result = await db.execute(
                select(XtreamAccount).where(
                    XtreamAccount.id == account_id,
                    XtreamAccount.is_active == True,
                )
            )
            account = result.scalars().first()
            if not account:
                logger.warning(f"Account {account_id} not found or inactive")
                _sync_jobs[job_id]["status"] = "failed"
                return job_id

            server_id = f"xtream_{account_id}"
            total_synced = 0

            # --- Fetch and store categories from Xtream ---
            await _refresh_categories(db, account, account_id)

            # Load category configuration
            filter_mode, allowed_vod, allowed_series = await _load_category_config(db, account_id)
            vod_allowed_count = sum(1 for v in allowed_vod.values() if v)
            series_allowed_count = sum(1 for v in allowed_series.values() if v)
            logger.info(f"Category filter mode: {filter_mode} "
                       f"(VOD: {vod_allowed_count}/{len(allowed_vod)} allowed, "
                       f"Series: {series_allowed_count}/{len(allowed_series)} allowed)")


            # --- VOD Sync (incremental with detailed metadata) ---
            logger.info(f"Syncing VOD for account {account_id}")
            try:
                vod_streams = await xtream_service.get_vod_streams(account)
            except Exception as e:
                logger.error(f"Failed to fetch VOD streams: {e}")
                vod_streams = []

            # Load existing dto_hashes for incremental sync
            hash_result = await db.execute(
                select(Media.rating_key, Media.dto_hash).where(
                    Media.server_id == server_id,
                    Media.type == "movie",
                )
            )
            existing_vod_hashes = {row[0]: row[1] for row in hash_result}

            # Determine which items need full API fetch
            all_vod_keys = set()
            items_to_fetch = []  # (dto, original_index, dto_hash)

            for i, dto in enumerate(vod_streams):
                if not isinstance(dto, dict):
                    continue
                ext = (dto.get("container_extension") or "").strip() or None
                stream_id = dto.get("stream_id")
                # Check category filtering
                category_id = str(dto.get("category_id", ""))
                if not _should_sync_category(category_id, filter_mode, allowed_vod):
                    continue  # Skip disallowed category
                if not stream_id:
                    continue
                rating_key = f"vod_{stream_id}.{ext}" if ext else f"vod_{stream_id}"
                all_vod_keys.add(rating_key)

                dto_hash = _compute_dto_hash(dto)
                if existing_vod_hashes.get(rating_key) == dto_hash:
                    continue  # Unchanged — skip expensive API call
                items_to_fetch.append((dto, i, dto_hash))

            skipped_vod = len(all_vod_keys) - len(items_to_fetch)
            logger.info(f"VOD: {len(items_to_fetch)} new/changed, {skipped_vod} unchanged (skipping API calls)")

            # Parallel fetch with semaphore
            semaphore = asyncio.Semaphore(10)

            async def fetch_vod_info_safe(dto, index, dto_hash):
                """Fetch vod_info with concurrency control."""
                async with semaphore:
                    try:
                        vod_info = None
                        try:
                            vod_info = await xtream_service.get_vod_info(account, vod_id=dto["stream_id"])
                            await asyncio.sleep(0.05)
                        except Exception as e:
                            logger.warning(f"Failed to fetch vod_info for stream {dto.get('stream_id')}: {e}")
                        row = map_vod_to_media(dto, account_id, index, vod_info)
                        row["dto_hash"] = dto_hash
                        row["is_in_allowed_categories"] = True
                        return row
                    except Exception as e:
                        logger.error(f"Failed to process VOD item {index}: {e}", exc_info=True)
                        return None

            # Process only changed items in batches
            vod_rows = []
            batch_size = 100

            for batch_start in range(0, len(items_to_fetch), batch_size):
                batch_end = min(batch_start + batch_size, len(items_to_fetch))
                batch = items_to_fetch[batch_start:batch_end]

                batch_fetch_start = now_ms()
                tasks = [fetch_vod_info_safe(dto, idx, dh) for dto, idx, dh in batch]
                batch_rows = [r for r in await asyncio.gather(*tasks) if r is not None]
                vod_rows.extend(batch_rows)
                batch_fetch_time = now_ms() - batch_fetch_start

                await upsert_media_batch(db, batch_rows)
                await db.commit()

                logger.info(f"VOD batch {batch_end}/{len(items_to_fetch)} synced "
                           f"({len(batch_rows)} items in {batch_fetch_time}ms)")

            # Cleanup: only remove stale items when syncing ALL categories
            # In whitelist/blacklist mode, we only synced a subset — don't delete the rest
            if filter_mode == "all" and all_vod_keys:
                await differential_cleanup(db, server_id, "all", all_vod_keys, media_type="movie")
            if vod_rows:
                await enqueue_for_enrichment(db, vod_rows)
            await db.commit()
            total_synced += len(all_vod_keys)
            logger.info(f"VOD sync: {len(vod_rows)} updated, {skipped_vod} unchanged, {len(all_vod_keys)} total")

            # --- Series Sync (incremental) ---
            logger.info(f"Syncing Series for account {account_id}")
            try:
                series_list = await xtream_service.get_series(account)
            except Exception as e:
                logger.error(f"Failed to fetch series: {e}")
                series_list = []

            # Load existing series dto_hashes
            hash_result = await db.execute(
                select(Media.rating_key, Media.dto_hash).where(
                    Media.server_id == server_id,
                    Media.type == "show",
                )
            )
            existing_series_hashes = {row[0]: row[1] for row in hash_result}

            all_series_keys = set()
            changed_series = []  # (dto, index, dto_hash)
            unchanged_count = 0

            # Debug: log first few series category_ids to verify matching
            if series_list and filter_mode != "all":
                sample_cat_ids = set()
                for s in series_list[:50]:
                    if isinstance(s, dict):
                        sample_cat_ids.add(str(s.get("category_id", "")))
                logger.debug(f"Series sample category_ids from API: {sorted(sample_cat_ids)}")
                logger.debug(f"Allowed series category_ids: "
                           f"{sorted(k for k, v in allowed_series.items() if v)}")

            filtered_out_series = 0
            for i, dto in enumerate(series_list):
                if not isinstance(dto, dict):
                    continue
                series_id = dto.get("series_id")
                # Check category filtering
                category_id = str(dto.get("category_id", ""))
                if not _should_sync_category(category_id, filter_mode, allowed_series):
                    filtered_out_series += 1
                    continue  # Skip disallowed category
                if not series_id:
                    continue
                rating_key = f"series_{series_id}"
                all_series_keys.add(rating_key)

                dto_hash = _compute_series_dto_hash(dto)
                if existing_series_hashes.get(rating_key) == dto_hash:
                    unchanged_count += 1
                    continue
                changed_series.append((dto, i, dto_hash))

            logger.info(f"Series: {len(changed_series)} new/changed, {unchanged_count} unchanged, "
                       f"{filtered_out_series} filtered out by category")

            # Map and upsert only changed series
            series_rows = []
            for dto, i, dto_hash in changed_series:
                try:
                    row = map_series_to_media(dto, account_id, i)
                    row["dto_hash"] = dto_hash
                    row["is_in_allowed_categories"] = True
                    series_rows.append(row)
                except Exception as e:
                    logger.error(f"Failed to process series item {i}: {e}", exc_info=True)
            if series_rows:
                await upsert_media_batch(db, series_rows)
                await enqueue_for_enrichment(db, series_rows)
            if filter_mode == "all" and all_series_keys:
                await differential_cleanup(db, server_id, "all", all_series_keys, media_type="show")
            await db.commit()
            total_synced += len(all_series_keys)
            logger.info(f"Series sync: {len(series_rows)} updated, {unchanged_count} unchanged")

            # --- Episodes Sync (only for changed series) ---
            changed_series_dtos = [dto for dto, _, _ in changed_series]
            logger.info(f"Fetching episodes for {len(changed_series_dtos)} changed series "
                       f"(skipping {unchanged_count} unchanged)")

            async def fetch_series_episodes(series_dto):
                """Fetch series info and map episodes."""
                async with semaphore:
                    try:
                        if not isinstance(series_dto, dict):
                            return []
                        series_info = await xtream_service.get_series_info(
                            account, series_id=series_dto["series_id"]
                        )
                        episodes_data = series_info.get("episodes") or {} if isinstance(series_info, dict) else {}
                        if not isinstance(episodes_data, dict):
                            logger.warning(f"Unexpected episodes_data type for series {series_dto.get('series_id')}: {type(episodes_data).__name__}")
                            return []
                        rows = []
                        for season_str, episodes in episodes_data.items():
                            try:
                                season_num = int(season_str)
                            except (ValueError, TypeError):
                                season_num = 0
                            for ep in episodes:
                                if isinstance(ep, dict):
                                    ep_row = map_episode_to_media(
                                        ep, series_dto, account_id, season_num
                                    )
                                    ep_row["is_in_allowed_categories"] = True
                                    rows.append(ep_row)
                        return rows
                    except Exception as e:
                        sid = series_dto.get('series_id') if isinstance(series_dto, dict) else '?'
                        logger.error(f"Failed to sync series {sid}: {e}", exc_info=True)
                        return []

            episode_count = 0
            batch_size = 50
            for batch_start in range(0, len(changed_series_dtos), batch_size):
                batch_end = min(batch_start + batch_size, len(changed_series_dtos))
                batch_series = changed_series_dtos[batch_start:batch_end]

                tasks = [fetch_series_episodes(s) for s in batch_series]
                batch_results = await asyncio.gather(*tasks)

                episode_batch = [ep for result in batch_results for ep in result]
                if episode_batch:
                    await upsert_media_batch(db, episode_batch)
                    await db.commit()
                    episode_count += len(episode_batch)
                    logger.info(f"Synced {episode_count} episodes ({batch_end}/{len(changed_series_dtos)} changed series)")

            total_synced += episode_count
            logger.info(f"Synced {episode_count} episodes")

            # --- Live Channels Sync (incremental) ---
            logger.info(f"Syncing Live channels for account {account_id}")
            try:
                live_streams = await xtream_service.get_live_streams(account)
            except Exception as e:
                logger.error(f"Failed to fetch live streams: {e}")
                live_streams = []

            # Load category config for live
            filter_mode_live, _, _ = await _load_category_config(db, account_id)
            # Load live-specific allowed categories
            from app.models.database import XtreamCategory
            live_cat_result = await db.execute(
                select(XtreamCategory).where(
                    XtreamCategory.account_id == account_id,
                    XtreamCategory.category_type == "live",
                )
            )
            allowed_live = {
                cat.category_id: cat.is_allowed
                for cat in live_cat_result.scalars().all()
            }

            # Load existing live dto_hashes for incremental sync
            hash_result = await db.execute(
                select(LiveChannel.stream_id, LiveChannel.dto_hash).where(
                    LiveChannel.server_id == server_id,
                )
            )
            existing_live_hashes = {row[0]: row[1] for row in hash_result}

            all_live_ids: set[int] = set()
            live_rows = []
            live_skipped = 0

            for dto in live_streams:
                if not isinstance(dto, dict):
                    continue
                stream_id = dto.get("stream_id")
                if not stream_id:
                    continue

                category_id = str(dto.get("category_id", ""))
                if not _should_sync_category(category_id, filter_mode_live, allowed_live):
                    continue

                all_live_ids.add(stream_id)

                dto_hash = _compute_live_dto_hash(dto)
                if existing_live_hashes.get(stream_id) == dto_hash:
                    live_skipped += 1
                    continue

                row = map_live_stream_to_channel(dto, account_id)
                row["dto_hash"] = dto_hash
                live_rows.append(row)

            if live_rows:
                batch_size = 200
                for batch_start in range(0, len(live_rows), batch_size):
                    batch = live_rows[batch_start:batch_start + batch_size]
                    await upsert_live_channels_batch(db, batch)
                    await db.commit()

            if filter_mode_live == "all" and all_live_ids:
                await differential_cleanup_live(db, server_id, all_live_ids)

            await db.commit()
            total_synced += len(all_live_ids)
            logger.info(f"Live sync: {len(live_rows)} updated, {live_skipped} unchanged, {len(all_live_ids)} total")

            # Recalculate visibility for ALL media based on category config
            from app.services.category_service import update_media_category_visibility
            await update_media_category_visibility(db, account_id)

            # Update account last_synced_at
            await db.execute(
                update(XtreamAccount)
                .where(XtreamAccount.id == account_id)
                .values(last_synced_at=now_ms())
            )

            await db.commit()

            _sync_jobs[job_id] = {
                "status": "completed",
                "progress": {"total": total_synced, "synced": total_synced},
            }
            logger.info(
                f"Sync complete for account {account_id}: {total_synced} items"
            )

    except Exception as e:
        logger.error(f"Sync failed for account {account_id}: {e}", exc_info=True)
        _sync_jobs[job_id] = {"status": "failed", "progress": {"error": str(e)}}

    return job_id


async def run_all_accounts():
    """Sync all active accounts."""
    logger.info("Starting catalog sync for all accounts")
    async with async_session_factory() as db:
        result = await db.execute(
            select(XtreamAccount).where(XtreamAccount.is_active == True)
        )
        accounts = result.scalars().all()

    for account in accounts:
        await sync_account(account.id)

    logger.info("All accounts sync complete")


def get_sync_job(job_id: str) -> dict | None:
    return _sync_jobs.get(job_id)
