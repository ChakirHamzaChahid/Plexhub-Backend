from pydantic import BaseModel


class PlexMovie(BaseModel):
    source_id: str
    title: str
    year: int | None = None
    stream_url: str
    poster_url: str | None = None
    fanart_url: str | None = None
    genres: str | None = None
    summary: str | None = None
    imdb_id: str | None = None
    tmdb_id: int | None = None
    content_rating: str | None = None
    rating: float | None = None
    duration_ms: int | None = None
    cast: str | None = None  # comma-separated actor names


class PlexEpisode(BaseModel):
    source_id: str
    series_title: str
    season_num: int
    episode_num: int
    title: str | None = None
    stream_url: str
    summary: str | None = None
    duration_ms: int | None = None
    thumb_url: str | None = None


class PlexSeries(BaseModel):
    source_id: str
    title: str
    year: int | None = None
    poster_url: str | None = None
    fanart_url: str | None = None
    genres: str | None = None
    summary: str | None = None
    imdb_id: str | None = None
    tmdb_id: int | None = None
    content_rating: str | None = None
    rating: float | None = None
    cast: str | None = None  # comma-separated actor names
    episodes: list[PlexEpisode] = []


class SyncReport(BaseModel):
    created: int = 0
    updated: int = 0
    deleted: int = 0
    unchanged: int = 0
    image_failures: int = 0
    image_failure_reasons: dict[str, int] = {}
    errors: list[str] = []
    duration_seconds: float = 0.0
