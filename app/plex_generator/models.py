from pydantic import BaseModel, model_validator


class PlexMovieVersion(BaseModel):
    """One playable source for a movie group (an account/quality/language variant).

    Several versions share a single movie folder + movie.nfo; each version is a
    distinct .strm file (edition-tagged when the group has more than one)."""
    source_id: str            # rating_key (unique within its server)
    server_id: str = ""       # which Xtream server this version comes from
    label: str | None = None  # human edition label, e.g. "VF · Compte 1"
    stream_url: str


class PlexMovie(BaseModel):
    source_id: str            # GROUP id (unification_id, or rating_key when ungrouped)
    title: str
    is_adult: bool = False    # adult/X-rated → folder/file/NFO title get the "[XXX] " tag
    year: int | None = None
    stream_url: str | None = None       # back-compat single-version shortcut
    versions: list[PlexMovieVersion] = []
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

    @model_validator(mode="after")
    def _coerce_single_version(self) -> "PlexMovie":
        # Back-compat: callers that pass source_id+stream_url (single source) get
        # an implicit one-element versions list so the generator has one code path.
        if not self.versions and self.stream_url:
            self.versions = [PlexMovieVersion(
                source_id=self.source_id, stream_url=self.stream_url,
            )]
        return self


class PlexEpisodeVersion(BaseModel):
    """One playable source for a single (season, episode) across accounts."""
    source_id: str
    server_id: str = ""
    label: str | None = None
    stream_url: str


class PlexEpisode(BaseModel):
    source_id: str            # GROUP id for this episode slot
    series_title: str
    season_num: int
    episode_num: int
    title: str | None = None
    stream_url: str | None = None       # back-compat single-version shortcut
    versions: list[PlexEpisodeVersion] = []
    summary: str | None = None
    duration_ms: int | None = None
    thumb_url: str | None = None

    @model_validator(mode="after")
    def _coerce_single_version(self) -> "PlexEpisode":
        if not self.versions and self.stream_url:
            self.versions = [PlexEpisodeVersion(
                source_id=self.source_id, stream_url=self.stream_url,
            )]
        return self


class PlexSeries(BaseModel):
    source_id: str            # GROUP id (unification_id, or rating_key when ungrouped)
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
