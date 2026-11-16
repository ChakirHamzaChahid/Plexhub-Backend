"""Tests for Lot 11 T11.1 — post-download automatic subtitle translation."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.models.database import DownloadJob, XtreamAccount
from app.services import download_service, subtitle_service
from app.services.download_service import PathConfinementError
from app.services.subtitle_service import SubtitleTranslation
from app.utils.server_id import build_server_id
from app.utils.time import now_ms
from app.workers import download_worker


def test_sanitize_lang_tag():
    assert subtitle_service.sanitize_lang_tag("fr") == "fr"
    assert subtitle_service.sanitize_lang_tag(" EN ") == "en"
    assert subtitle_service.sanitize_lang_tag("pt-BR") == "pt-BR"
    assert subtitle_service.sanitize_lang_tag("") is None
    assert subtitle_service.sanitize_lang_tag("../etc") is None
    assert subtitle_service.sanitize_lang_tag("fr/../../x") is None
    assert subtitle_service.sanitize_lang_tag("fr;rm -rf") is None


def test_write_sidecar_subtitle_confinement(tmp_path: Path, monkeypatch):
    dl_dir = tmp_path / "downloads"
    dl_dir.mkdir()
    monkeypatch.setattr(settings, "DOWNLOAD_DIR", str(dl_dir))

    inside = dl_dir / "Movies" / "Film (2024)" / "Film (2024).mkv"
    inside.parent.mkdir(parents=True)
    written = download_service.write_sidecar_subtitle(inside, "fr", "1\n00:00:01,000 --> 00:00:02,000\nSalut\n")
    assert written == inside.with_name("Film (2024).fr.srt")
    assert written.read_text(encoding="utf-8").startswith("1\n")

    outside = tmp_path / "outside" / "evil.mkv"
    outside.parent.mkdir()
    with pytest.raises(PathConfinementError):
        download_service.write_sidecar_subtitle(outside, "fr", "x")


async def test_sidecar_subtitles_disabled_by_default(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "DOWNLOAD_AUTO_SUBTITLES_LANG", "")
    dest = tmp_path / "Movie.mkv"
    dest.write_bytes(b"video")
    (tmp_path / "Movie.en.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n", encoding="utf-8")

    job = SimpleNamespace(id="job1")
    with patch.object(subtitle_service, "translate_subtitles", new_callable=AsyncMock) as mock_tr:
        await download_worker._write_sidecar_subtitles(job, dest)
        mock_tr.assert_not_awaited()
    assert not (tmp_path / "Movie.fr.srt").exists()


async def test_sidecar_subtitles_from_external_srt(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "DOWNLOAD_AUTO_SUBTITLES_LANG", "fr")
    dest = tmp_path / "Movie (2024).mkv"
    dest.write_bytes(b"video")
    (tmp_path / "Movie (2024).en.srt").write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nHello world\n",
        encoding="utf-8",
    )

    translated_srt = "1\n00:00:01,000 --> 00:00:02,000\nBonjour le monde\n"
    job = SimpleNamespace(id="job2")
    with patch.object(
        subtitle_service,
        "translate_subtitles",
        new=AsyncMock(
            return_value=SubtitleTranslation(
                translated_content=translated_srt, fmt="srt", cue_count=1, model="gemma4",
            )
        ),
    ) as mock_tr:
        await download_worker._write_sidecar_subtitles(job, dest)
        mock_tr.assert_awaited_once()

    sidecar = tmp_path / "Movie (2024).fr.srt"
    assert sidecar.is_file()
    assert "Bonjour le monde" in sidecar.read_text(encoding="utf-8")


async def test_sidecar_subtitles_from_external_vtt(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "DOWNLOAD_AUTO_SUBTITLES_LANG", "fr")
    dest = tmp_path / "Clip.mp4"
    dest.write_bytes(b"video")
    (tmp_path / "Clip.vtt").write_text(
        "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHello VTT\n",
        encoding="utf-8",
    )

    job = SimpleNamespace(id="job3")
    with patch.object(
        subtitle_service,
        "translate_subtitles",
        new=AsyncMock(
            return_value=SubtitleTranslation(
                translated_content="1\n00:00:01,000 --> 00:00:02,000\nBonjour VTT\n",
                fmt="srt",
                cue_count=1,
                model="gemma4",
            )
        ),
    ) as mock_tr:
        await download_worker._write_sidecar_subtitles(job, dest)
        call_args = mock_tr.call_args
        assert "00:00:01,000 --> 00:00:02,000" in call_args.args[0]
        assert call_args.kwargs["fmt"] == "srt"

    assert (tmp_path / "Clip.fr.srt").is_file()


async def test_sidecar_subtitles_extracted_via_ffmpeg(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "DOWNLOAD_AUTO_SUBTITLES_LANG", "fr")
    dest = tmp_path / "Embedded.mkv"
    dest.write_bytes(b"video")

    extracted = "1\n00:00:01,000 --> 00:00:02,000\nFrom MKV\n"
    translated = "1\n00:00:01,000 --> 00:00:02,000\nDepuis MKV\n"
    job = SimpleNamespace(id="job4")

    with (
        patch.object(subtitle_service, "extract_srt_from_media", new=AsyncMock(return_value=extracted)),
        patch.object(
            subtitle_service,
            "translate_subtitles",
            new=AsyncMock(
                return_value=SubtitleTranslation(
                    translated_content=translated, fmt="srt", cue_count=1, model="gemma4",
                )
            ),
        ),
    ):
        await download_worker._write_sidecar_subtitles(job, dest)

    sidecar = tmp_path / "Embedded.fr.srt"
    assert sidecar.is_file()
    assert "Depuis MKV" in sidecar.read_text(encoding="utf-8")


async def test_run_job_succeeds_even_if_ollama_unavailable(db_engine, tmp_path: Path, monkeypatch, respx_mock):
    monkeypatch.setattr(settings, "DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "DOWNLOAD_MIN_FREE_DISK_MB", 0)
    monkeypatch.setattr(settings, "DOWNLOAD_AUTO_SUBTITLES_LANG", "fr")

    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    account = XtreamAccount(
        id="acc1", label="Acc", base_url="http://provider.example", port=80,
        username="u", password="p", is_active=True, created_at=0,
    )
    job = DownloadJob(
        id="job_ollama_down", batch_id=None, server_id=build_server_id("acc1"),
        rating_key="vod_101", media_type="movie", unification_id=None,
        title="Film Test", season=None, episode=None,
        dest_path="Movies/Film Test/Film Test.mkv",
        state="queued", bytes_total=None, bytes_done=0, attempts=0,
        created_at=now_ms(), updated_at=now_ms(),
    )
    async with factory() as s:
        s.add_all([account, job])
        await s.commit()

    # Pre-seed an external subtitle to force translate_subtitles invocation
    dest_dir = tmp_path / "Movies" / "Film Test"
    dest_dir.mkdir(parents=True)
    (dest_dir / "Film Test.en.srt").write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nHi\n", encoding="utf-8",
    )

    respx_mock.get("http://provider.example/movie/u/p/101.ts").mock(
        return_value=httpx.Response(200, content=b"fake-video-bytes")
    )

    with (
        patch.object(download_service, "assert_public_redirect_host", new=AsyncMock(return_value=None)),
        patch.object(download_service, "_assert_public_redirect_host", new=AsyncMock(return_value=None)),
        patch.object(
            subtitle_service,
            "translate_subtitles",
            new=AsyncMock(side_effect=httpx.ConnectError("ollama down")),
        ),
    ):
        await download_worker._run_job(factory, "job_ollama_down", asyncio.Semaphore(1))

    async with factory() as s:
        refreshed = await s.get(DownloadJob, "job_ollama_down")
        assert refreshed is not None
        assert refreshed.state == "completed"
    assert (dest_dir / "Film Test.mkv").is_file()
    assert not (dest_dir / "Film Test.fr.srt").exists()
