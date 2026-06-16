import re
import unicodedata


_LEADING_PIPE_RE = re.compile(r"^\|[^|]+\|\s*")
_LEADING_BRACK_RE = re.compile(r"^\[[^\]]+\]\s*")
# Country/language prefix: exactly 2 uppercase letters followed by " - " (FR, NF, SC, EN, ...)
_COUNTRY_PREFIX_RE = re.compile(r"^[A-Z]{2}\s*-\s*")
# Trailing brackets carry quality/audio info: "[FHD MULTi-SUBAR]", "[VOSTFR]", "[4K]", ...
_TRAIL_BRACKET_RE = re.compile(r"\s*\[[^\]]*\]\s*$")
# Trailing quality keyword as a separate word: " LQ", " HQ", " FHD", ...
# VFR/VFI/VFB cover French restored / Internet / Belgian dubs that the older
# IPTV providers add as a trailing tag.
_TRAIL_QUALITY_RE = re.compile(
    r"\s+(?:LQ|HQ|FHD|UHD|HD|SD|4K|VF|VFF|VFQ|VFR|VFI|VFB|VOSTFR|VOST|MULTI)\s*$",
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


def normalize_for_sorting(title: str) -> str:
    """
    Match Android StringNormalizer.normalizeForSorting().
    Strips leading articles, removes diacritics, strips punctuation, collapses
    whitespace and lowercases — so fuzzy matching compares on letters/digits only.
    """
    # Remove leading articles
    lower = title.lower()
    for article in [
        "the ", "a ", "an ",
        "le ", "la ", "les ", "l'",
        "un ", "une ", "des ",
    ]:
        if lower.startswith(article):
            title = title[len(article):]
            break

    # Normalize unicode (remove accents)
    nfkd = unicodedata.normalize("NFKD", title)
    stripped = "".join(c for c in nfkd if not unicodedata.combining(c))

    # NEW: drop everything but letters/digits/space (matches Kotlin [^\p{L}\p{N}\s])
    s = stripped.lower().replace("_", " ")
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s).strip()
    return s


# ── Hardened title cleaner (sync entry point) ──────────────────────────────
# Quality / language / release tags stripped ANYWHERE (case-insensitive, whole
# word). Order matters in the regex: longer alternatives first (WEB-DL > WEB).
_QUALITY_TAGS = (
    "2160P", "1080P", "720P", "480P", "4K", "UHD", "HDLIGHT", "HDR", "HD", "SD",
    "HQ", "LQ", "X264", "X265", "H264", "H265", "HEVC", "WEB-DL", "WEBRIP", "WEB",
    "BLURAY", "BRRIP", "BDRIP", "DVDRIP", "AC3", "DTS", "MULTI",
    "TRUEFRENCH", "SUBFRENCH", "VOSTFR", "VOST", "VFF", "VFQ", "VFI", "VFB",
    "VFF2", "VF2", "VF", "VO", "FANSUB",
)
_QUALITY_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(t) for t in _QUALITY_TAGS) + r")\b",
    re.IGNORECASE,
)
# Leading language/country prefix followed by a separator: "Fr - ", "FRA - ",
# "VOSTFR - ", "VF | ", "NF: " … (case-insensitive, may be stacked).
_LANG_PREFIX_TOKENS = (
    "VOSTFR", "VOST", "MULTI", "TRUEFRENCH", "VFF", "VFQ", "VFI", "VFB", "VF",
    "VO", "FR", "FRA", "EN", "US", "UK", "NF", "SC", "AR", "PL", "DE", "ES", "IT",
)
_LANG_PREFIX_RE = re.compile(
    r"^\s*(?:" + "|".join(_LANG_PREFIX_TOKENS) + r")\s*[-|:]\s+",
    re.IGNORECASE,
)
# Generic all-caps 2-4 letter code prefix ("DE - ", "ZX | ") — case-sensitive
# so it never eats a real lowercase title word.
_CAPS_PREFIX_RE = re.compile(r"^\s*[A-Z]{2,4}\s*[-|:]\s+")
_LEADING_PIPE_MARK_RE = re.compile(r"^\s*\|[^|]*\|\s*")
_LEADING_BRACKET_MARK_RE = re.compile(r"^\s*\[[^\]]*\]\s*")
_YEAR_TOKEN_RE = re.compile(r"(?<!\d)(19|20)\d{2}(?!\d)")
_PARENS_YEAR_RE = re.compile(r"[(\[]\s*((?:19|20)\d{2})\s*[)\]]")
_ANY_BRACKET_RE = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")
_ORPHAN_EDGE_RE = re.compile(r"^[\s\-|:._]+|[\s\-|:._]+$")


def clean_title(raw: str) -> tuple[str, int | None]:
    """Hardened cleaner for Xtream titles → (clean_title, year).

    Strips stacked language/country prefixes, scene separators, the year (in
    parentheses OR bare), quality/release tags anywhere, and leftover brackets —
    while PRESERVING meaningful punctuation inside the title (e.g. the ``-`` in
    "Spider-Man" or "Skarb narodow-Ksiega tajemnic"). Returns the raw string
    untouched if cleaning would empty it (avoids "Unknown" collisions).
    """
    if not raw:
        return ("Unknown", None)
    title = raw

    # 1) Leading markers + stacked language/country prefixes (loop until stable).
    prev = None
    while prev != title:
        prev = title
        title = _LEADING_PIPE_MARK_RE.sub("", title)
        title = _LEADING_BRACKET_MARK_RE.sub("", title)
        title = _LANG_PREFIX_RE.sub("", title)
        title = _CAPS_PREFIX_RE.sub("", title)

    # 2) Scene separators: dotted/underscored names with no spaces → spaces.
    if " " not in title.strip() and ("." in title or "_" in title):
        title = title.replace(".", " ").replace("_", " ")

    # 3) Year — prefer a parenthesized year, else the last bare plausible year.
    year: int | None = None
    pm = list(_PARENS_YEAR_RE.finditer(title))
    if pm:
        year = int(pm[-1].group(1))
        title = title[: pm[-1].start()] + " " + title[pm[-1].end():]
    else:
        ym = list(_YEAR_TOKEN_RE.finditer(title))
        if ym:
            m = ym[-1]
            year = int(m.group(0))
            title = title[: m.start()] + " " + title[m.end():]

    # 4) Quality/release/language tags anywhere.
    title = _QUALITY_RE.sub(" ", title)

    # 5) Leftover brackets/braces/parens (non-year), in a loop.
    prev = None
    while prev != title:
        prev = title
        title = _ANY_BRACKET_RE.sub(" ", title)

    # 6) Final tidy: collapse spaces, strip orphan edge separators.
    title = re.sub(r"\s+", " ", title).strip()
    title = _ORPHAN_EDGE_RE.sub("", title)
    title = re.sub(r"\s+", " ", title).strip()

    if not title:
        return (raw.strip() or "Unknown", year)
    return (title, year)


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

    if not title:
        # The source title was nothing but year and/or qualifier(s) — preserve
        # it intact rather than collapsing to "Unknown" which loses info AND
        # collisions with every other empty-after-strip title.
        fallback = (raw or "").strip()
        if fallback:
            return (fallback, None, None)
        return ("Unknown", None, None)

    suffix = " ".join(suffixes) if suffixes else None
    return (title, year, suffix)


def parse_rating(value) -> float | None:
    """Safely parse a rating value to float."""
    if value is None:
        return None
    try:
        val = float(value)
        return val if val > 0 else None
    except (ValueError, TypeError):
        return None
