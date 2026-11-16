"""tests/test_admin_audit.py — Consolidated downloads nav, UDL-01 catalogue_total, and /admin/audit (T9.3)."""
from __future__ import annotations

import json

import pytest

from app.config import settings
from app.db import database as db_module
from app.models.database import (
    DownloadJob,
    Media,
    MetadataEdit,
    PlexMediaItem,
    PlexServer,
    ScrapeCacheEntry,
    XtreamAccount,
)
from app.utils.server_id import build_plex_server_id
from app.utils.time import now_ms

pytestmark = pytest.mark.asyncio

ADMIN_USER = "admin"
ADMIN_PASS = "test-admin-audit-pass"
ADMIN_AUTH = (ADMIN_USER, ADMIN_PASS)


@pytest.fixture(autouse=True)
def _configure_admin(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_USERNAME", ADMIN_USER)
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", ADMIN_PASS)


def _wire_db(monkeypatch, db_factory):
    monkeypatch.setattr(db_module, "async_session_factory", db_factory)


def _make_job(job_id: str, title: str = "Queued Movie") -> DownloadJob:
    ts = now_ms()
    return DownloadJob(
        id=job_id,
        server_id="xtream_acc1",
        rating_key=f"vod_{job_id}.mkv",
        media_type="movie",
        unification_id=f"imdb://tt{job_id}",
        title=title,
        dest_path=f"movies/{title}.mkv",
        state="queued",
        created_at=ts,
        updated_at=ts,
    )


class TestAdminAuditAndDownloadsAuth:
    async def test_audit_requires_basic_auth(self, api_client):
        resp = await api_client.get("/admin/audit")
        assert resp.status_code == 401

    async def test_downloads_requires_basic_auth(self, api_client):
        resp = await api_client.get("/admin/downloads")
        assert resp.status_code == 401

    async def test_plex_downloads_requires_basic_auth(self, api_client):
        resp = await api_client.get("/admin/plex-downloads")
        assert resp.status_code == 401


class TestDownloadsCatalogueTotalAndNav:
    async def test_xtream_downloads_catalogue_total_not_clobbered_by_queue(
        self, api_client, monkeypatch, db_factory
    ):
        _wire_db(monkeypatch, db_factory)
        async with db_factory() as s:
            s.add(
                XtreamAccount(
                    id="acc1",
                    label="Xtream One",
                    base_url="http://provider.example",
                    port=80,
                    username="u",
                    password="p",
                    is_active=True,
                    created_at=0,
                )
            )
            s.add(
                Media(
                    rating_key="1001",
                    server_id="xtream_acc1",
                    filter="all",
                    sort_order="default",
                    library_section_id="xtream_vod",
                    title="Terminator",
                    type="movie",
                    year=1984,
                    unification_id="imdb://tt0088247",
                    is_in_allowed_categories=True,
                    is_broken=False,
                    media_parts="[]",
                )
            )
            # 3 jobs in queue != 1 item in catalogue
            for i in range(1, 4):
                s.add(_make_job(f"000{i}", title=f"Job {i}"))
            await s.commit()

        resp = await api_client.get("/admin/downloads", auth=ADMIN_AUTH)
        assert resp.status_code == 200
        body = resp.text
        assert "1 résultat(s)" in body
        assert "3 job(s)" in body
        assert "3 résultat(s)" not in body
        # Source sub-tabs present
        assert 'href="/admin/downloads"' in body
        assert 'href="/admin/plex-downloads"' in body
        assert "Xtream" in body
        assert "Plex" in body

    async def test_plex_downloads_catalogue_total_not_clobbered_by_queue(
        self, api_client, monkeypatch, db_factory
    ):
        _wire_db(monkeypatch, db_factory)
        ts = now_ms()
        cid = "srv1"
        server_id = build_plex_server_id(cid)
        async with db_factory() as s:
            s.add(
                PlexServer(
                    client_identifier=cid,
                    name="Shared Plex",
                    owner_title="Owner",
                    owned=False,
                    base_uri="http://192.0.2.10:32400",
                    access_token="plex-secret-token-never-shown",
                    is_reachable=True,
                    created_at=ts,
                    updated_at=ts,
                )
            )
            s.add(
                PlexMediaItem(
                    server_id=server_id,
                    rating_key="1001",
                    type="movie",
                    title="Blade Runner",
                    year=1982,
                    unification_id="imdb://tt0083658",
                    added_at=ts,
                    synced_at=ts,
                )
            )
            s.add(
                PlexMediaItem(
                    server_id=server_id,
                    rating_key="1002",
                    type="movie",
                    title="Alien",
                    year=1979,
                    unification_id="imdb://tt0078748",
                    added_at=ts,
                    synced_at=ts,
                )
            )
            # 4 jobs in queue != 2 items in Plex catalogue
            for i in range(1, 5):
                s.add(_make_job(f"001{i}", title=f"Job {i}"))
            await s.commit()

        resp = await api_client.get("/admin/plex-downloads", auth=ADMIN_AUTH)
        assert resp.status_code == 200
        body = resp.text
        assert "2 résultat(s)" in body
        assert "4 job(s)" in body
        assert "4 résultat(s)" not in body
        assert "plex-secret-token-never-shown" not in body
        # Source sub-tabs present
        assert 'href="/admin/downloads"' in body
        assert 'href="/admin/plex-downloads"' in body


class TestAdminAuditView:
    async def test_audit_renders_nav_metadata_edits_and_scrape_cache_stats(
        self, api_client, monkeypatch, db_factory
    ):
        _wire_db(monkeypatch, db_factory)
        now = now_ms()
        async with db_factory() as s:
            e_old = MetadataEdit(
                edit_id="01EDITOLD00000000000000001",
                kind="patch",
                at_ms=now - 60_000,
                server_id="xtream_acc1",
                rating_key="vod_1001",
                item_type="movie",
                key_id="key_editor_1",
                reason="Fix TMDB match",
                revision_before="rev0",
                revision_after="rev1",
                before_json=json.dumps({"variants": [{"fields": {"tmdbId": 100}}]}),
                after_json=json.dumps({"variants": [{"fields": {"tmdbId": 200}}]}),
                response_json=json.dumps({"secretToken": "MUST_NOT_LEAK"}),
                reverted_by="01EDITNEW00000000000000002",
            )
            e_new = MetadataEdit(
                edit_id="01EDITNEW00000000000000002",
                kind="revert",
                at_ms=now - 10_000,
                server_id="xtream_acc1",
                rating_key="vod_1001",
                item_type="movie",
                key_id="master",
                reason="Rollback",
                revision_before="rev1",
                revision_after="rev0",
                before_json=json.dumps({"variants": [{"fields": {"tmdbId": 200}}]}),
                after_json=json.dumps({"variants": [{"fields": {"tmdbId": 100}}]}),
                response_json="{}",
                reverts="01EDITOLD00000000000000001",
            )
            s.add_all([e_old, e_new])

            s.add_all(
                [
                    ScrapeCacheEntry(
                        family="tmdb_match",
                        key_version=1,
                        language="fr-FR",
                        cache_key="movie:terminator:1984",
                        result="matched",
                        schema_version=1,
                        payload="{}",
                        fetched_at=now - 1000,
                        expires_at=now + 60_000,
                    ),
                    ScrapeCacheEntry(
                        family="tmdb_match",
                        key_version=1,
                        language="fr-FR",
                        cache_key="movie:alien:1979",
                        result="nomatch",
                        schema_version=1,
                        payload=None,
                        fetched_at=now - 100_000,
                        expires_at=now - 5_000,
                    ),
                    ScrapeCacheEntry(
                        family="omdb_imdb",
                        key_version=1,
                        language="*",
                        cache_key="tt0088247",
                        result="found",
                        schema_version=1,
                        payload="{}",
                        fetched_at=now - 500,
                        expires_at=now + 120_000,
                    ),
                ]
            )
            await s.commit()

        resp = await api_client.get("/admin/audit", auth=ADMIN_AUTH)
        assert resp.status_code == 200
        body = resp.text

        # Consolidated nav: Téléchargements -> /admin/downloads, Audit -> /admin/audit, no Télécharger Plex in nav
        assert '<a href="/admin/downloads"' in body
        assert ">Téléchargements<" in body
        assert ">Télécharger Plex<" not in body
        assert '<a href="/admin/audit"' in body
        assert ">Audit<" in body

        # Metadata edits rendered in descending order (newer before older)
        assert "Rollback" in body
        assert "Fix TMDB match" in body
        assert body.index("Rollback") < body.index("Fix TMDB match")
        assert "01EDITNEW00000000000000002" in body
        assert "01EDITOLD00000000000000001" in body
        assert "key_editor_1" in body
        assert "master" in body
        assert "tmdbId" in body
        assert "MUST_NOT_LEAK" not in body

        # Scrape cache stats: total 3, active 2, expired 1 + family breakdown
        assert 'id="cache-total">3<' in body
        assert 'id="cache-active">2<' in body
        assert 'id="cache-expired">1<' in body
        assert "tmdb_match" in body
        assert "omdb_imdb" in body
