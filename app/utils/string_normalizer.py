import re
import unicodedata


def parse_title_and_year(raw: str) -> tuple[str, int | None]:
    """
    Parse IPTV title, stripping prefixes and extracting year.

    Input:  "|VM| Le Monde apres nous (2023)"
    Output: ("Le Monde apres nous", 2023)
    """
    # Strip IPTV prefixes: |XX|, |XX XX|, [XX], etc.
    title = re.sub(r"^\|[^|]+\|\s*", "", raw)
    title = re.sub(r"^\[[^\]]+\]\s*", "", title)

    # Extract year from (YYYY) at end of title
    year_match = re.search(r"\((\d{4})\)\s*$", title)
    year = int(year_match.group(1)) if year_match else None
    if year_match:
        title = title[: year_match.start()].strip()

    return title.strip() or "Unknown", year


def normalize_for_sorting(title: str) -> str:
    """
    Match Android StringNormalizer.normalizeForSorting().
    Strips leading articles and removes diacritics.
    """
    # Remove leading articles
    lower = title.lower()
    for article in [
        "the ", "a ", "an ",
        "le ", "la ", "les ", "l'",
        "un ", "une ",
    ]:
        if lower.startswith(article):
            title = title[len(article):]
            break

    # Normalize unicode (remove accents)
    nfkd = unicodedata.normalize("NFKD", title)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def parse_rating(value) -> float | None:
    """Safely parse a rating value to float."""
    if value is None:
        return None
    try:
        val = float(value)
        return val if val > 0 else None
    except (ValueError, TypeError):
        return None
