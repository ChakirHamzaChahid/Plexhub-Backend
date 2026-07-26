import re
from app.utils.string_normalizer import normalize_for_sorting


def calculate_unification_id(
    title: str,
    year: int | None,
    imdb_id: str | None = None,
    tmdb_id: str | None = None,
) -> str:
    """
    Priority: imdb > tmdb > title_year.
    Must match Android MediaMapper logic exactly.
    """
    if imdb_id:
        # Ensure IMDB ID has 'tt' prefix
        if not imdb_id.startswith("tt"):
            imdb_id = f"tt{imdb_id}"
        return f"imdb://{imdb_id}"
    if tmdb_id:
        return f"tmdb://{tmdb_id}"
    # Fallback: normalized title + year
    if title == "Unknown":
        return ""
    normalized = normalize_for_sorting(title).lower()
    normalized = re.sub(r"\s+", "_", normalized)
    normalized = re.sub(r"[^a-z0-9_]", "", normalized)
    return f"title_{normalized}_{year}" if year else f"title_{normalized}"



def calculate_history_group_key(
    unification_id: str,
    rating_key: str,
    server_id: str,
) -> str:
    return unification_id if unification_id else f"{rating_key}{server_id}"


def calculate_display_rating(
    scraped_rating: float | None,
    audience_rating: float | None,
    rating: float | None,
) -> float:
    """COALESCE(scrapedRating, audienceRating, rating, 0.0) — matches Android.

    Sync-only fallback (ADR 0004 Decision 1, AUDIT-P4-005): `sync_worker` is
    the sole remaining caller of this formula. It is correct ONLY for raw
    Xtream rows that carry no IMDb/TMDB rating yet — in that case
    `app/utils/rating_blend.blend_rating` returns `None` ("nothing to
    write") and this COALESCE is the right repli. Never use this to
    (re)compute `display_rating` for a row that has, or is being given, an
    IMDb or TMDB rating: `rating_blend.blend_rating` /
    `blend_display_rating_case` is the single source of truth for that case
    (see `nfo_import_service._compute_updates`, `enrichment_worker`). Calling
    this formula on such a row is exactly the bug this ADR fixes — two
    writers disagreeing and flip-flopping `display_rating` on every run.
    """
    return scraped_rating or audience_rating or rating or 0.0
