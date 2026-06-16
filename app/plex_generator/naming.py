import re


# Characters invalid on Windows and/or problematic for Plex path parsing
_INVALID_CHARS = re.compile(r'[\\/:*?"<>|]')
# Braces would break Plex's "{edition-...}" parsing if they appear inside a label.
_EDITION_INVALID_CHARS = re.compile(r'[\\/:*?"<>|{}]')


def sanitize_for_filesystem(name: str) -> str:
    """Remove or replace characters that are invalid in file/folder names.

    - Replaces \\ / : * ? \" < > | with a space
    - Collapses multiple spaces
    - Strips leading/trailing whitespace
    - Strips trailing dots (Windows silently ignores them)
    """
    name = _INVALID_CHARS.sub(" ", name)
    name = re.sub(r"\s+", " ", name).strip()
    name = name.rstrip(".")
    return name.strip() or "Unknown"


def sanitize_edition_label(label: str) -> str:
    """Sanitize a label used inside a Plex ``{edition-LABEL}`` tag.

    Same rules as :func:`sanitize_for_filesystem` but also strips braces so a
    label can never close the edition tag prematurely."""
    label = _EDITION_INVALID_CHARS.sub(" ", label)
    label = re.sub(r"\s+", " ", label).strip()
    label = label.rstrip(".")
    return label.strip() or "v"


def _decorate_with_disambiguator(
    base: str, suffix: str | None, fallback_id: str | None,
) -> str:
    """Append a collision-disambiguator if any was provided.

    Priority:
      1. `suffix` (extracted from the source title, e.g. "US", "HD") —
         preferred because it carries human meaning.
      2. `fallback_id` (e.g. short rating_key fragment) — last resort when
         two media collide and have no qualifier in their title.
    """
    if suffix:
        return f"{base} ({sanitize_for_filesystem(suffix)})"
    if fallback_id:
        return f"{base} [{sanitize_for_filesystem(fallback_id)}]"
    return base


def _movie_folder(
    title: str, year: int | None,
    suffix: str | None = None, fallback_id: str | None = None,
) -> str:
    safe = sanitize_for_filesystem(title)
    base = f"{safe} ({year})" if year else safe
    return _decorate_with_disambiguator(base, suffix, fallback_id)


def movie_path(
    title: str, year: int | None,
    suffix: str | None = None, fallback_id: str | None = None,
) -> str:
    """Relative path for a movie .strm file.

    Example: Films/Dune (2021)/Dune (2021).strm
    With collision: Films/Dune (2021) (HD)/Dune (2021) (HD).strm
    """
    folder = _movie_folder(title, year, suffix, fallback_id)
    return f"Films/{folder}/{folder}.strm"


def movie_nfo_path(
    title: str, year: int | None,
    suffix: str | None = None, fallback_id: str | None = None,
) -> str:
    """Relative path for a movie NFO file."""
    folder = _movie_folder(title, year, suffix, fallback_id)
    return f"Films/{folder}/movie.nfo"


def movie_poster_path(
    title: str, year: int | None,
    suffix: str | None = None, fallback_id: str | None = None,
) -> str:
    """Relative path for a movie poster image."""
    folder = _movie_folder(title, year, suffix, fallback_id)
    return f"Films/{folder}/poster.jpg"


def movie_fanart_path(
    title: str, year: int | None,
    suffix: str | None = None, fallback_id: str | None = None,
) -> str:
    """Relative path for a movie fanart image."""
    folder = _movie_folder(title, year, suffix, fallback_id)
    return f"Films/{folder}/fanart.jpg"


def _series_folder(
    series_title: str,
    year: int | None = None,
    suffix: str | None = None,
    fallback_id: str | None = None,
) -> str:
    safe = sanitize_for_filesystem(series_title)
    base = f"{safe} ({year})" if year else safe
    return _decorate_with_disambiguator(base, suffix, fallback_id)


def series_episode_path(
    series_title: str, season: int, episode: int,
    year: int | None = None,
    suffix: str | None = None, fallback_id: str | None = None,
) -> str:
    """Relative path for an episode .strm file.

    Example: Series/The Last of Us (2023)/Season 01/The Last of Us (2023) S01E01.strm
    """
    safe_title = _series_folder(series_title, year, suffix, fallback_id)
    season_str = f"Season {season:02d}"
    ep_str = f"{safe_title} S{season:02d}E{episode:02d}"
    return f"Series/{safe_title}/{season_str}/{ep_str}.strm"


def series_episode_nfo_path(
    series_title: str, season: int, episode: int,
    year: int | None = None,
    suffix: str | None = None, fallback_id: str | None = None,
) -> str:
    """Relative path for an episode .nfo file."""
    safe_title = _series_folder(series_title, year, suffix, fallback_id)
    season_str = f"Season {season:02d}"
    ep_str = f"{safe_title} S{season:02d}E{episode:02d}"
    return f"Series/{safe_title}/{season_str}/{ep_str}.nfo"


def series_nfo_path(
    series_title: str,
    year: int | None = None,
    suffix: str | None = None, fallback_id: str | None = None,
) -> str:
    """Relative path for a series NFO file."""
    safe_title = _series_folder(series_title, year, suffix, fallback_id)
    return f"Series/{safe_title}/tvshow.nfo"


def series_poster_path(
    series_title: str,
    year: int | None = None,
    suffix: str | None = None, fallback_id: str | None = None,
) -> str:
    """Relative path for a series poster image."""
    safe_title = _series_folder(series_title, year, suffix, fallback_id)
    return f"Series/{safe_title}/poster.jpg"


def series_fanart_path(
    series_title: str,
    year: int | None = None,
    suffix: str | None = None, fallback_id: str | None = None,
) -> str:
    """Relative path for a series fanart image."""
    safe_title = _series_folder(series_title, year, suffix, fallback_id)
    return f"Series/{safe_title}/fanart.jpg"


# ── Multi-version (dedup) paths ────────────────────────────────────────────
# When the same movie/episode exists across several accounts (or as several
# qualities/languages within one), they share ONE folder + ONE .nfo and each
# playable source becomes a distinct file. Movies use Plex "{edition-...}" tags;
# episodes use a trailing " - label" (Plex merges files by SxxEyy automatically).


def movie_version_path(
    title: str, year: int | None, edition_label: str,
    suffix: str | None = None, fallback_id: str | None = None,
) -> str:
    """Relative path for one version of a movie, tagged as a Plex edition.

    Example: Films/Terminator (1984)/Terminator (1984) {edition-VF Compte 1}.strm
    """
    folder = _movie_folder(title, year, suffix, fallback_id)
    tag = sanitize_edition_label(edition_label)
    return f"Films/{folder}/{folder} {{edition-{tag}}}.strm"


def series_episode_version_path(
    series_title: str, season: int, episode: int, version_label: str,
    year: int | None = None,
    suffix: str | None = None, fallback_id: str | None = None,
) -> str:
    """Relative path for one version of an episode.

    Example: Series/Show (2020)/Season 01/Show (2020) S01E01 - VF Compte 1.strm
    """
    folder = _series_folder(series_title, year, suffix, fallback_id)
    season_str = f"Season {season:02d}"
    base = f"{folder} S{season:02d}E{episode:02d}"
    label = sanitize_for_filesystem(version_label)
    return f"Series/{folder}/{season_str}/{base} - {label}.strm"
