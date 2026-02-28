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
    """COALESCE(scrapedRating, audienceRating, rating, 0.0) — matches Android."""
    return scraped_rating or audience_rating or rating or 0.0
