import re
import unicodedata


_LEADING_PIPE_RE = re.compile(r"^\|[^|]+\|\s*")
_LEADING_BRACK_RE = re.compile(r"^\[[^\]]+\]\s*")
# Country/language prefix: exactly 2 uppercase letters followed by " - " (FR, NF, SC, EN, ...)
_COUNTRY_PREFIX_RE = re.compile(r"^[A-Z]{2}\s*-\s*")
# Trailing brackets carry quality/audio info: "[FHD MULTi-SUBAR]", "[VOSTFR]", "[4K]", ...
_TRAIL_BRACKET_RE = re.compile(r"\s*\[[^\]]*\]\s*$")
# Trailing quality keyword as a separate word: " LQ", " HQ", " FHD", ...
_TRAIL_QUALITY_RE = re.compile(
    r"\s+(?:LQ|HQ|FHD|UHD|HD|SD|4K|VF|VFF|VFQ|VOSTFR|VOST|MULTI)\s*$",
    re.IGNORECASE,
)
_YEAR_RE = re.compile(r"\((\d{4})\)\s*$")
# Year in parentheses anywhere in the string (used by parse_title_year_and_suffix
# where qualifier suffixes may sit AFTER the year, e.g. "Les Experts (2000) (US)").
_YEAR_ANYWHERE_RE = re.compile(r"\((\d{4})\)")
# Any single-level parenthesized fragment — for extracting qualifier suffixes
# once the year has been pulled out.
_PAREN_FRAGMENT_RE = re.compile(r"\(([^()]+)\)")


def _strip_trailing_junk(title: str) -> str:
    """Strip trailing brackets and quality keywords until stable."""
    prev = None
    while prev != title:
        prev = title
        title = _TRAIL_BRACKET_RE.sub("", title).rstrip()
        title = _TRAIL_QUALITY_RE.sub("", title).rstrip()
    return title


def parse_title_and_year(raw: str) -> tuple[str, int | None]:
    """
    Parse IPTV title, stripping prefixes/suffixes and extracting year.

    Strips:
      - Leading IPTV markers: "|VM|", "[XX]"
      - Leading country/language prefix: "FR - ", "NF - ", "SC - "
      - Trailing brackets:    "[FHD MULTi-SUBAR]", "[VOSTFR]", "[4K]"
      - Trailing quality:     " LQ", " HQ", " FHD", " UHD"

    Examples:
      "|VM| Le Monde apres nous (2023)"          -> ("Le Monde apres nous", 2023)
      "FR - Better Man (2024)"                    -> ("Better Man", 2024)
      "FR - Aquaman (2023) LQ"                    -> ("Aquaman", 2023)
      "Black Widow (2021) [FHD MULTi-SUBAR]"      -> ("Black Widow", 2021)
    """
    title = _LEADING_PIPE_RE.sub("", raw)
    title = _LEADING_BRACK_RE.sub("", title)
    title = _COUNTRY_PREFIX_RE.sub("", title)

    # Trailing junk can sit before AND after the year, so strip both sides of it.
    title = _strip_trailing_junk(title)

    year_match = _YEAR_RE.search(title)
    year = int(year_match.group(1)) if year_match else None
    if year_match:
        title = title[: year_match.start()].rstrip()

    title = _strip_trailing_junk(title)

    return title.strip() or "Unknown", year


def parse_title_year_and_suffix(raw: str) -> tuple[str, int | None, str | None]:
    """Like parse_title_and_year, but also extracts a qualifier suffix.

    The Xtream catalog often distinguishes versions of the same media via a
    qualifier in parentheses — e.g. "Les Experts (2000) (US)" vs
    "Les Experts (2000) (HD)". The Jellyfin convention is "Title (Year)" with
    nothing else, so we strip everything by default but return the qualifier
    so the caller can re-attach it on collisions.

    Returns:
        (clean_title, year, suffix) — suffix is None if no non-year qualifier
        was found, otherwise a single string joining all extracted qualifiers
        with " " (e.g. "US", "HD", "VOSTFR FR", ...).

    Examples:
        "Les Experts (2000) (US)"        -> ("Les Experts", 2000, "US")
        "Les Experts (2000) (HD)"        -> ("Les Experts", 2000, "HD")
        "Les Experts (2000)"             -> ("Les Experts", 2000, None)
        "FR - Les Experts (2000) (US)"   -> ("Les Experts", 2000, "US")
        "Better Man (2024) [FHD]"        -> ("Better Man", 2024, None)
        "Foo (US)"                       -> ("Foo", None, "US")
    """
    title = _LEADING_PIPE_RE.sub("", raw)
    title = _LEADING_BRACK_RE.sub("", title)
    title = _COUNTRY_PREFIX_RE.sub("", title)
    title = _strip_trailing_junk(title)

    year: int | None = None
    year_match = _YEAR_ANYWHERE_RE.search(title)
    if year_match:
        year = int(year_match.group(1))
        title = (title[: year_match.start()] + title[year_match.end():])

    # Extract every remaining (non-year) parenthesized fragment as a qualifier.
    suffixes: list[str] = []
    while True:
        m = _PAREN_FRAGMENT_RE.search(title)
        if not m:
            break
        content = m.group(1).strip()
        title = (title[: m.start()] + title[m.end():])
        if content and not _YEAR_ANYWHERE_RE.fullmatch(f"({content})"):
            suffixes.append(content)

    title = _strip_trailing_junk(title)
    title = re.sub(r"\s+", " ", title).strip()
    suffix = " ".join(suffixes) if suffixes else None
    return (title or "Unknown", year, suffix)


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
