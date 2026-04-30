"""NFO import service: parser + matching + write integration."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.models.database import Media
from app.services import nfo_import_service


_MOVIE_NFO = """<?xml version="1.0" encoding="utf-8" standalone="yes"?>
<movie>
  <title>FR - Die Hard 4 : Retour en enfer</title>
  <originaltitle>Live Free or Die Hard</originaltitle>
  <year>2007</year>
  <imdbid>tt0337978</imdbid>
  <tmdbid>1571</tmdbid>
  <uniqueid type="tmdb" default="true">1571</uniqueid>
</movie>
"""

_TVSHOW_NFO = """<?xml version="1.0" encoding="utf-8" standalone="yes"?>
<tvshow>
  <title>7SEEDS</title>
  <originaltitle>7SEEDS</originaltitle>
  <year>2019</year>
  <imdb_id>tt9348718</imdb_id>
  <tmdbid>85940</tmdbid>
</tvshow>
"""


def _write_nfo(tmp_path: Path, kind: str, folder: str, content: str) -> Path:
    sub = "Films" if kind == "movies" else "Series"
    name = "movie.nfo" if kind == "movies" else "tvshow.nfo"
    target = tmp_path / sub / folder / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def test_parse_movie_nfo_extracts_ids_and_year(tmp_path):
    path = _write_nfo(tmp_path, "movies", "Die Hard 4 (2007)", _MOVIE_NFO)
    entry = nfo_import_service.parse_nfo_file(path, "movie")
    assert entry is not None
    assert entry.imdb_id == "tt0337978"
    assert entry.tmdb_id == "1571"
    assert entry.nfo_year == 2007
    assert entry.folder_name == "Die Hard 4 (2007)"


def test_parse_tvshow_nfo_handles_imdb_id_underscore_variant(tmp_path):
    path = _write_nfo(tmp_path, "shows", "7SEEDS", _TVSHOW_NFO)
    entry = nfo_import_service.parse_nfo_file(path, "show")
    assert entry is not None
    assert entry.imdb_id == "tt9348718"
    assert entry.tmdb_id == "85940"


async def test_import_writes_missing_ids_and_skips_existing(
    tmp_path, db_session,
):
    _write_nfo(tmp_path, "movies", "Die Hard 4 (2007)", _MOVIE_NFO)

    # One row that matches (folder name parses to "Die Hard 4", year=2007), no IDs yet.
    db_session.add(Media(
        rating_key="rk-1", server_id="srv-1",
        filter="all", sort_order="default",
        library_section_id="lib-1", title="Die Hard 4 (2007)",
        type="movie", year=2007,
        added_at=1, updated_at=1,
    ))
    await db_session.commit()

    [report] = await nfo_import_service.import_nfo(
        db_session, tmp_path, kinds=("movies",), overwrite=False, dry_run=False,
    )
    await db_session.commit()

    assert report.matched == 1
    assert report.written == 1
    assert not report.unmatched

    refreshed = (await db_session.get(
        Media, {"rating_key": "rk-1", "server_id": "srv-1",
                "filter": "all", "sort_order": "default"}
    ))
    assert refreshed.imdb_id == "tt0337978"
    assert refreshed.tmdb_id == "1571"

    # Second run without overwrite must be a no-op for already-set IDs.
    [report2] = await nfo_import_service.import_nfo(
        db_session, tmp_path, kinds=("movies",), overwrite=False, dry_run=False,
    )
    assert report2.matched == 1
    assert report2.written == 0
    assert report2.skipped_id_already_set + report2.skipped_no_change == 1


async def test_import_dry_run_does_not_write(tmp_path, db_session):
    _write_nfo(tmp_path, "movies", "Die Hard 4 (2007)", _MOVIE_NFO)
    db_session.add(Media(
        rating_key="rk-1", server_id="srv-1",
        filter="all", sort_order="default",
        library_section_id="lib-1", title="Die Hard 4 (2007)",
        type="movie", year=2007,
        added_at=1, updated_at=1,
    ))
    await db_session.commit()

    [report] = await nfo_import_service.import_nfo(
        db_session, tmp_path, kinds=("movies",), overwrite=False, dry_run=True,
    )
    assert report.dry_run is True
    assert report.matched == 1
    assert report.written == 1  # would-be write count

    refreshed = await db_session.get(
        Media, {"rating_key": "rk-1", "server_id": "srv-1",
                "filter": "all", "sort_order": "default"}
    )
    assert refreshed.imdb_id is None
    assert refreshed.tmdb_id is None
