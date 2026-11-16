"""Tests for per-library download directory routing (Movies / Series / Anime)."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.config import settings
from app.services import download_service
from app.services.download_service import PathConfinementError


def test_resolve_target_library_auto_and_explicit():
    assert download_service.resolve_target_library("movie", genres="Animation") == "movies"
    assert download_service.resolve_target_library("movie", target_library="anime") == "anime"
    assert download_service.resolve_target_library("episode", genres="Action, Drama") == "series"
    assert download_service.resolve_target_library("episode", genres="Animation, Action") == "anime"
    assert download_service.resolve_target_library("episode", category_name="Séries Animés VOSTFR") == "anime"
    assert download_service.resolve_target_library(
        "episode", target_library="series", genres="Animation",
    ) == "series"


def test_compute_dest_path_anime_movies_series():
    assert download_service.compute_dest_path(
        media_type="movie", title="Inception", year=2010,
        season=None, episode=None, ext="mkv", target_library="movies",
    ) == "Movies/Inception (2010)/Inception (2010).mkv"

    assert download_service.compute_dest_path(
        media_type="episode", title="Breaking Bad", year=None,
        season=1, episode=2, ext="mkv", target_library="series",
    ) == "Series/Breaking Bad/Season 01/Breaking Bad - S01E02.mkv"

    assert download_service.compute_dest_path(
        media_type="episode", title="Attack on Titan", year=None,
        season=1, episode=1, ext="mkv", target_library="anime",
    ) == "Anime/Attack on Titan/Season 01/Attack on Titan - S01E01.mkv"


def test_resolve_confined_routes_to_per_library_dirs_and_blocks_escape(tmp_path: Path, monkeypatch):
    dl_dir = tmp_path / "dl"
    movies_dir = tmp_path / "Film"
    series_dir = tmp_path / "data-series"
    anime_dir = tmp_path / "data-anime"
    for d in (dl_dir, movies_dir, series_dir, anime_dir):
        d.mkdir()

    monkeypatch.setattr(settings, "DOWNLOAD_DIR", str(dl_dir))
    monkeypatch.setattr(settings, "DOWNLOAD_MOVIES_DIR", str(movies_dir))
    monkeypatch.setattr(settings, "DOWNLOAD_SERIES_DIR", str(series_dir))
    monkeypatch.setattr(settings, "DOWNLOAD_ANIME_DIR", str(anime_dir))

    m_path = download_service.resolve_confined("Movies/Matrix (1999)/Matrix (1999).mkv")
    assert m_path == (movies_dir / "Matrix (1999)" / "Matrix (1999).mkv").resolve()

    s_path = download_service.resolve_confined("Series/Dark/Season 01/Dark - S01E01.mkv")
    assert s_path == (series_dir / "Dark" / "Season 01" / "Dark - S01E01.mkv").resolve()

    a_path = download_service.resolve_confined("Anime/Naruto/Season 01/Naruto - S01E01.mkv")
    assert a_path == (anime_dir / "Naruto" / "Season 01" / "Naruto - S01E01.mkv").resolve()

    with pytest.raises(PathConfinementError):
        download_service.resolve_confined("Movies/../../etc/passwd")

    with pytest.raises(PathConfinementError):
        download_service.resolve_confined("Anime/../../etc/passwd")

    # Sidecar subtitle confinement also honours per-library roots
    m_path.parent.mkdir(parents=True)
    srt = download_service.write_sidecar_subtitle(m_path, "fr", "1\n00:00:01,000 --> 00:00:02,000\nSalut\n")
    assert srt == m_path.with_name("Matrix (1999).fr.srt")
