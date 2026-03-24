from xml.etree.ElementTree import Element, SubElement, tostring
from xml.dom.minidom import parseString

from app.plex_generator.models import PlexMovie, PlexSeries, PlexEpisode


def _to_pretty_xml(root: Element) -> str:
    raw = tostring(root, encoding="unicode")
    dom = parseString(raw)
    return dom.toprettyxml(indent="  ", encoding=None)


def _add_genres(parent: Element, genres: str | None) -> None:
    if not genres:
        return
    for genre in genres.split(","):
        genre = genre.strip()
        if genre:
            SubElement(parent, "genre").text = genre


def _add_uniqueids(parent: Element, imdb_id: str | None, tmdb_id: int | None) -> None:
    if imdb_id:
        uid = SubElement(parent, "uniqueid", type="imdb", default="true")
        uid.text = imdb_id
    if tmdb_id:
        uid = SubElement(parent, "uniqueid", type="tmdb")
        if not imdb_id:
            uid.set("default", "true")
        uid.text = str(tmdb_id)


def _add_ratings(parent: Element, rating: float | None) -> None:
    if not rating or rating <= 0:
        return
    ratings_el = SubElement(parent, "ratings")
    rating_el = SubElement(ratings_el, "rating", name="default", max="10", default="true")
    SubElement(rating_el, "value").text = f"{rating:.1f}"


def _add_cast(parent: Element, cast: str | None) -> None:
    if not cast:
        return
    for actor_name in cast.split(","):
        actor_name = actor_name.strip()
        if actor_name:
            actor_el = SubElement(parent, "actor")
            SubElement(actor_el, "name").text = actor_name


def _add_thumb(parent: Element, poster_url: str | None, fanart_url: str | None) -> None:
    if poster_url:
        SubElement(parent, "thumb", aspect="poster").text = poster_url
    if fanart_url:
        fanart_el = SubElement(parent, "fanart")
        SubElement(fanart_el, "thumb").text = fanart_url


def _duration_minutes(duration_ms: int | None) -> int | None:
    if not duration_ms or duration_ms <= 0:
        return None
    return round(duration_ms / 60000)


def build_movie_nfo(movie: PlexMovie) -> str:
    """Build a Jellyfin/Kodi-compatible movie.nfo XML string."""
    root = Element("movie")

    SubElement(root, "title").text = movie.title
    if movie.year:
        SubElement(root, "year").text = str(movie.year)
    if movie.summary:
        SubElement(root, "plot").text = movie.summary
    if movie.content_rating:
        SubElement(root, "mpaa").text = movie.content_rating

    runtime = _duration_minutes(movie.duration_ms)
    if runtime:
        SubElement(root, "runtime").text = str(runtime)

    _add_ratings(root, movie.rating)
    _add_genres(root, movie.genres)
    _add_uniqueids(root, movie.imdb_id, movie.tmdb_id)
    _add_thumb(root, movie.poster_url, movie.fanart_url)
    _add_cast(root, movie.cast)

    return _to_pretty_xml(root)


def build_tvshow_nfo(series: PlexSeries) -> str:
    """Build a Jellyfin/Kodi-compatible tvshow.nfo XML string."""
    root = Element("tvshow")

    SubElement(root, "title").text = series.title
    if series.year:
        SubElement(root, "year").text = str(series.year)
    if series.summary:
        SubElement(root, "plot").text = series.summary
    if series.content_rating:
        SubElement(root, "mpaa").text = series.content_rating

    _add_ratings(root, series.rating)
    _add_genres(root, series.genres)
    _add_uniqueids(root, series.imdb_id, series.tmdb_id)
    _add_thumb(root, series.poster_url, series.fanart_url)
    _add_cast(root, series.cast)

    return _to_pretty_xml(root)


def build_episode_nfo(episode: PlexEpisode) -> str:
    """Build a Jellyfin/Kodi-compatible episodedetails.nfo XML string."""
    root = Element("episodedetails")

    SubElement(root, "title").text = episode.title or f"Episode {episode.episode_num}"
    SubElement(root, "showtitle").text = episode.series_title
    SubElement(root, "season").text = str(episode.season_num)
    SubElement(root, "episode").text = str(episode.episode_num)

    if episode.summary:
        SubElement(root, "plot").text = episode.summary

    runtime = _duration_minutes(episode.duration_ms)
    if runtime:
        SubElement(root, "runtime").text = str(runtime)

    if episode.thumb_url:
        SubElement(root, "thumb").text = episode.thumb_url

    return _to_pretty_xml(root)
