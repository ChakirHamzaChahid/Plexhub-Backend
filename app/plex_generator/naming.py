import re


# Characters invalid on Windows and/or problematic for Plex path parsing
_INVALID_CHARS = re.compile(r'[\\/:*?"<>|]')


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


def _movie_folder(title: str, year: int | None) -> str:
    safe = sanitize_for_filesystem(title)
    if year:
        return f"{safe} ({year})"
    return safe


def movie_path(title: str, year: int | None) -> str:
    """Relative path for a movie .strm file.

    Example: Films/Dune (2021)/Dune (2021).strm
    """
    folder = _movie_folder(title, year)
    return f"Films/{folder}/{folder}.strm"


def movie_nfo_path(title: str, year: int | None) -> str:
    """Relative path for a movie NFO file."""
    folder = _movie_folder(title, year)
    return f"Films/{folder}/movie.nfo"


def movie_poster_path(title: str, year: int | None) -> str:
    """Relative path for a movie poster image."""
    folder = _movie_folder(title, year)
    return f"Films/{folder}/poster.jpg"


def movie_fanart_path(title: str, year: int | None) -> str:
    """Relative path for a movie fanart image."""
    folder = _movie_folder(title, year)
    return f"Films/{folder}/fanart.jpg"


def _series_folder(series_title: str) -> str:
    return sanitize_for_filesystem(series_title)


def series_episode_path(
    series_title: str, season: int, episode: int,
) -> str:
    """Relative path for an episode .strm file.

    Example: Series/The Last of Us/Season 01/The Last of Us S01E01.strm
    """
    safe_title = _series_folder(series_title)
    season_str = f"Season {season:02d}"
    ep_str = f"{safe_title} S{season:02d}E{episode:02d}"
    return f"Series/{safe_title}/{season_str}/{ep_str}.strm"


def series_episode_nfo_path(
    series_title: str, season: int, episode: int,
) -> str:
    """Relative path for an episode .nfo file.

    Example: Series/The Last of Us/Season 01/The Last of Us S01E01.nfo
    """
    safe_title = _series_folder(series_title)
    season_str = f"Season {season:02d}"
    ep_str = f"{safe_title} S{season:02d}E{episode:02d}"
    return f"Series/{safe_title}/{season_str}/{ep_str}.nfo"


def series_nfo_path(series_title: str) -> str:
    """Relative path for a series NFO file."""
    safe_title = _series_folder(series_title)
    return f"Series/{safe_title}/tvshow.nfo"


def series_poster_path(series_title: str) -> str:
    """Relative path for a series poster image."""
    safe_title = _series_folder(series_title)
    return f"Series/{safe_title}/poster.jpg"


def series_fanart_path(series_title: str) -> str:
    """Relative path for a series fanart image."""
    safe_title = _series_folder(series_title)
    return f"Series/{safe_title}/fanart.jpg"
