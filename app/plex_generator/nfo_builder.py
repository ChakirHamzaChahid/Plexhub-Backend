from xml.etree.ElementTree import Element, SubElement, tostring
from xml.dom.minidom import parseString

from app.plex_generator.models import PlexMovie, PlexSeries


def _to_pretty_xml(root: Element) -> str:
    raw = tostring(root, encoding="unicode")
    dom = parseString(raw)
    return dom.toprettyxml(indent="  ", encoding=None)


def build_movie_nfo(movie: PlexMovie) -> str:
    """Build a Kodi-style movie.nfo XML string."""
    root = Element("movie")

    SubElement(root, "title").text = movie.title
    if movie.year:
        SubElement(root, "year").text = str(movie.year)
    if movie.summary:
        SubElement(root, "plot").text = movie.summary
    if movie.content_rating:
        SubElement(root, "mpaa").text = movie.content_rating
    if movie.genres:
        for genre in movie.genres.split(","):
            genre = genre.strip()
            if genre:
                SubElement(root, "genre").text = genre
    if movie.imdb_id:
        uid = SubElement(root, "uniqueid", type="imdb", default="true")
        uid.text = movie.imdb_id
    if movie.tmdb_id:
        uid = SubElement(root, "uniqueid", type="tmdb")
        uid.text = str(movie.tmdb_id)

    return _to_pretty_xml(root)


def build_tvshow_nfo(series: PlexSeries) -> str:
    """Build a Kodi-style tvshow.nfo XML string."""
    root = Element("tvshow")

    SubElement(root, "title").text = series.title
    if series.year:
        SubElement(root, "year").text = str(series.year)
    if series.summary:
        SubElement(root, "plot").text = series.summary
    if series.genres:
        for genre in series.genres.split(","):
            genre = genre.strip()
            if genre:
                SubElement(root, "genre").text = genre
    if series.imdb_id:
        uid = SubElement(root, "uniqueid", type="imdb", default="true")
        uid.text = series.imdb_id
    if series.tmdb_id:
        uid = SubElement(root, "uniqueid", type="tmdb")
        uid.text = str(series.tmdb_id)

    return _to_pretty_xml(root)
