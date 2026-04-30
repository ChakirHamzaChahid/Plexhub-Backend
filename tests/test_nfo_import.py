"""NFO import service: parser + deterministic mapping-driven import."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.models.database import Media, XtreamAccount
from app.plex_generator.mapping import MappingStore
from app.plex_generator.naming import movie_path, series_nfo_path
from app.services import nfo_import_service
from app.utils.server_id import build_server_id


_ACCOUNT_ID = "8fb2c0f3"
_SERVER_ID = build_server_id(_ACCOUNT_ID)


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


# ---------- helpers ----------

async def _seed_account(db_session, account_id: str = _ACCOUNT_ID) -> None:
    db_session.add(XtreamAccount(
        id=account_id, label="test", base_url="http://x", port=80,
        username="u", password="p", is_active=True, created_at=0,
    ))
    await db_session.commit()


def _seed_movie_on_disk(
    tmp_path: Path,
    rating_key: str,
    title: str,
    year: int,
    nfo_content: str = _MOVIE_NFO,
    account_id: str = _ACCOUNT_ID,
) -> Path:
    """Mimic plex_generator output: <root>/<account>/Films/<folder>/<folder>.strm
    + movie.nfo in same folder + entry in .plex_mapping.json."""
    account_root = tmp_path / account_id
    rel_strm = movie_path(title, year)
    strm_full = account_root / rel_strm
    strm_full.parent.mkdir(parents=True, exist_ok=True)
    strm_full.write_text("http://example/stream\n", encoding="utf-8")
    (strm_full.parent / "movie.nfo").write_text(nfo_content, encoding="utf-8")

    mapping = MappingStore(account_root)
    mapping.load()
    mapping.set(rating_key, rel_strm, "http://example/stream")
    mapping.save()
    return strm_full.parent


def _seed_show_on_disk(
    tmp_path: Path,
    title: str,
    nfo_content: str = _TVSHOW_NFO,
    account_id: str = _ACCOUNT_ID,
) -> Path:
    account_root = tmp_path / account_id
    nfo_rel = series_nfo_path(title)  # "Series/<safe_title>/tvshow.nfo"
    nfo_full = account_root / nfo_rel
    nfo_full.parent.mkdir(parents=True, exist_ok=True)
    nfo_full.write_text(nfo_content, encoding="utf-8")
    return nfo_full


# ---------- parser ----------

def test_parse_movie_nfo_extracts_ids_and_year(tmp_path):
    nfo = tmp_path / "movie.nfo"
    nfo.write_text(_MOVIE_NFO, encoding="utf-8")
    entry = nfo_import_service.parse_nfo_file(nfo, "movie")
    assert entry is not None
    assert entry.imdb_id == "tt0337978"
    assert entry.tmdb_id == "1571"
    assert entry.nfo_year == 2007


def test_parse_tvshow_nfo_handles_imdb_id_underscore_variant(tmp_path):
    nfo = tmp_path / "tvshow.nfo"
    nfo.write_text(_TVSHOW_NFO, encoding="utf-8")
    entry = nfo_import_service.parse_nfo_file(nfo, "show")
    assert entry is not None
    assert entry.imdb_id == "tt9348718"
    assert entry.tmdb_id == "85940"


# ---------- movie import (mapping-driven) ----------

async def test_movie_import_writes_missing_ids_and_skips_existing(
    tmp_path, db_session,
):
    await _seed_account(db_session)
    rating_key = "vod_18661.mkv"
    _seed_movie_on_disk(tmp_path, rating_key, "Die Hard 4", 2007)

    db_session.add(Media(
        rating_key=rating_key, server_id=_SERVER_ID,
        filter="all", sort_order="default",
        library_section_id="lib-1", title="Die Hard 4",
        type="movie", year=2007,
        added_at=1, updated_at=1,
    ))
    await db_session.commit()

    [report] = await nfo_import_service.import_nfo(
        db_session, tmp_path, kinds=("movies",), overwrite=False, dry_run=False,
    )
    await db_session.commit()

    assert report.account_id == _ACCOUNT_ID
    assert report.matched == 1
    assert report.written == 1
    assert not report.unmatched

    refreshed = await db_session.get(
        Media, {"rating_key": rating_key, "server_id": _SERVER_ID,
                "filter": "all", "sort_order": "default"}
    )
    assert refreshed.imdb_id == "tt0337978"
    assert refreshed.tmdb_id == "1571"

    # Second run must be a no-op for already-set IDs (overwrite=False).
    [report2] = await nfo_import_service.import_nfo(
        db_session, tmp_path, kinds=("movies",), overwrite=False, dry_run=False,
    )
    assert report2.matched == 1
    assert report2.written == 0
    assert (
        report2.skipped_id_already_set + report2.skipped_no_change == 1
    )


async def test_movie_import_dry_run_does_not_write(tmp_path, db_session):
    await _seed_account(db_session)
    rating_key = "vod_18661.mkv"
    _seed_movie_on_disk(tmp_path, rating_key, "Die Hard 4", 2007)

    db_session.add(Media(
        rating_key=rating_key, server_id=_SERVER_ID,
        filter="all", sort_order="default",
        library_section_id="lib-1", title="Die Hard 4",
        type="movie", year=2007,
        added_at=1, updated_at=1,
    ))
    await db_session.commit()

    [report] = await nfo_import_service.import_nfo(
        db_session, tmp_path, kinds=("movies",), overwrite=False, dry_run=True,
    )
    assert report.matched == 1
    assert report.written == 1  # would-be write count

    refreshed = await db_session.get(
        Media, {"rating_key": rating_key, "server_id": _SERVER_ID,
                "filter": "all", "sort_order": "default"}
    )
    assert refreshed.imdb_id is None
    assert refreshed.tmdb_id is None


async def test_movie_import_unmatched_when_db_row_missing(
    tmp_path, db_session,
):
    """The mapping points at rating_key but no Media row exists for it."""
    await _seed_account(db_session)
    _seed_movie_on_disk(tmp_path, "vod_orphan.mkv", "Orphan", 2020)

    [report] = await nfo_import_service.import_nfo(
        db_session, tmp_path, kinds=("movies",), overwrite=False, dry_run=True,
    )
    assert report.matched == 0
    assert report.unmatched
    assert any("vod_orphan.mkv" in u for u in report.unmatched)


async def test_movie_import_skips_other_account(tmp_path, db_session):
    """A row owned by a different server_id must not be touched."""
    await _seed_account(db_session)
    rating_key = "vod_18661.mkv"
    _seed_movie_on_disk(tmp_path, rating_key, "Die Hard 4", 2007)

    db_session.add(Media(
        rating_key=rating_key,
        server_id=build_server_id("aaaaaaaa"),  # other account
        filter="all", sort_order="default",
        library_section_id="lib-1", title="Die Hard 4",
        type="movie", year=2007,
        added_at=1, updated_at=1,
    ))
    await db_session.commit()

    [report] = await nfo_import_service.import_nfo(
        db_session, tmp_path, kinds=("movies",), overwrite=False, dry_run=False,
    )
    assert report.matched == 0
    assert report.unmatched


# ---------- show import (DB-driven via series_nfo_path) ----------

async def test_show_import_writes_missing_ids(tmp_path, db_session):
    await _seed_account(db_session)
    _seed_show_on_disk(tmp_path, "7SEEDS")

    db_session.add(Media(
        rating_key="series_42", server_id=_SERVER_ID,
        filter="all", sort_order="default",
        library_section_id="lib-1", title="7SEEDS",
        type="show", year=2019,
        added_at=1, updated_at=1,
    ))
    await db_session.commit()

    [report] = await nfo_import_service.import_nfo(
        db_session, tmp_path, kinds=("shows",), overwrite=False, dry_run=False,
    )
    await db_session.commit()

    assert report.matched == 1
    assert report.written == 1

    refreshed = await db_session.get(
        Media, {"rating_key": "series_42", "server_id": _SERVER_ID,
                "filter": "all", "sort_order": "default"}
    )
    assert refreshed.imdb_id == "tt9348718"
    assert refreshed.tmdb_id == "85940"


async def test_show_import_unmatched_when_nfo_missing(tmp_path, db_session):
    await _seed_account(db_session)
    # account dir exists but no Series/<title>/tvshow.nfo
    (tmp_path / _ACCOUNT_ID).mkdir(parents=True, exist_ok=True)
    db_session.add(Media(
        rating_key="series_42", server_id=_SERVER_ID,
        filter="all", sort_order="default",
        library_section_id="lib-1", title="Ghost Show",
        type="show", year=2019,
        added_at=1, updated_at=1,
    ))
    await db_session.commit()

    [report] = await nfo_import_service.import_nfo(
        db_session, tmp_path, kinds=("shows",), overwrite=False, dry_run=True,
    )
    assert report.matched == 0
    assert any("Ghost Show" in u for u in report.unmatched)
