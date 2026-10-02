"""Lot 10 sub-lot D (FR-BE-222, ADR 0006): bulk EPG sync from XMLTV.

Everything runs on local fixtures: the XMLTV download is replaced by a fake that
writes a fixture file, so there is NO network call; databases are in-memory or
temp files, never `data/plexhub.db`.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape

import httpx
import pytest
from sqlalchemy import select

from app.config import settings
from app.models.database import EpgEntry, LiveChannel, XtreamAccount
from app.services import epg_sync_service as svc
from app.services.epg_sync_service import parse_xmltv_time, run_epg_sync
from app.services.xtream_service import xtream_service
from tests._records import Records

NOW_MS = (int(time.time()) // 60) * 60 * 1000  # whole minute: exact assertions
MIN = 60_000
SECRET = "S3CR3T-PASSWORD"


# ─── fixtures / helpers ─────────────────────────────────────────────────────


def xt(ms: int, offset_min: int = 0) -> str:
    """Format epoch ms as an XMLTV timestamp in the given UTC offset."""
    tz = timezone(timedelta(minutes=offset_min))
    return datetime.fromtimestamp(ms / 1000, tz=tz).strftime("%Y%m%d%H%M%S %z")


def prog(channel, start, stop, title="T", *, desc=None, lang=None, raw=None) -> str:
    attrs = raw if raw is not None else f'start="{start}" stop="{stop}"'
    title_xml = ""
    if title is not None:
        lang_attr = f' lang="{lang}"' if lang else ""
        title_xml = f"<title{lang_attr}>{escape(title)}</title>"
    desc_xml = f"<desc>{escape(desc)}</desc>" if desc else ""
    return f'<programme {attrs} channel="{channel}">{title_xml}{desc_xml}</programme>'


def xmltv(*programmes: str, channels=("ch1", "ch2")) -> str:
    chans = "".join(
        f'<channel id="{c}"><display-name>{c}</display-name></channel>' for c in channels
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE tv SYSTEM "xmltv.dtd">\n'
        f'<tv generator-info-name="test">{chans}{"".join(programmes)}</tv>'
    )


def account(id_="a", *, active=True) -> XtreamAccount:
    return XtreamAccount(
        id=id_, label=f"Compte {id_}", base_url=f"http://{id_}.example", port=80,
        username="user", password=SECRET, is_active=active, created_at=0,
    )


def channel(stream_id, server_id, epg_id, *, allowed=True, adult=False) -> LiveChannel:
    return LiveChannel(
        stream_id=stream_id, server_id=server_id, name=f"Chaine {stream_id}",
        name_sortable=f"chaine {stream_id}", epg_channel_id=epg_id,
        category_id="1", is_in_allowed_categories=allowed, is_adult=adult, added_at=0,
    )


@pytest.fixture(autouse=True)
def _window(monkeypatch):
    monkeypatch.setattr(settings, "EPG_SYNC_PAST_HOURS", 1)
    monkeypatch.setattr(settings, "EPG_SYNC_FUTURE_HOURS", 12)
    monkeypatch.setattr(settings, "EPG_SYNC_MAX_BYTES", 10_000_000)
    monkeypatch.setattr(settings, "EPG_SYNC_MAX_PROGRAMMES", 500_000)
    monkeypatch.setattr(settings, "EPG_SYNC_BATCH_SIZE", 500)
    monkeypatch.setattr(svc, "now_ms", lambda: NOW_MS)


def fake_download(monkeypatch, xml_by_account: dict[str, str | Exception], calls=None):
    """Replace the network download: write the fixture for the account (keyed by
    its id) into `dest`, or raise the configured exception."""

    async def _fake(acc, dest, *, max_bytes, total_timeout, **_):
        if calls is not None:
            calls.append((acc.id, dest))
        payload = xml_by_account[acc.id]
        if isinstance(payload, Exception):
            raise payload
        data = payload.encode("utf-8")
        with open(dest, "wb") as fh:
            fh.write(data)
        return len(data)

    monkeypatch.setattr(xtream_service, "download_xmltv", _fake)


async def seed(db_factory, *objs):
    async with db_factory() as s:
        s.add_all(objs)
        await s.commit()


async def rows(db_factory):
    async with db_factory() as s:
        return (await s.execute(
            select(EpgEntry).order_by(EpgEntry.stream_id, EpgEntry.start_time)
        )).scalars().all()


# ─── parse_xmltv_time ───────────────────────────────────────────────────────


class TestParseXmltvTime:
    UTC_2026_01_01_12 = int(datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)

    def test_utc_offset_zero(self):
        assert parse_xmltv_time("20260101120000 +0000") == self.UTC_2026_01_01_12

    def test_positive_offset_is_converted_to_utc(self):
        assert parse_xmltv_time("20260101130000 +0100") == self.UTC_2026_01_01_12

    def test_negative_offset_is_converted_to_utc(self):
        assert parse_xmltv_time("20260101070000 -0500") == self.UTC_2026_01_01_12

    def test_half_hour_offset(self):
        assert parse_xmltv_time("20260101173000 +0530") == self.UTC_2026_01_01_12

    def test_zulu_and_missing_offset_mean_utc(self):
        assert parse_xmltv_time("20260101120000Z") == self.UTC_2026_01_01_12
        assert parse_xmltv_time("20260101120000") == self.UTC_2026_01_01_12

    def test_colon_in_offset_and_no_space(self):
        assert parse_xmltv_time("20260101130000+01:00") == self.UTC_2026_01_01_12

    def test_truncated_forms(self):
        assert parse_xmltv_time("202601011200 +0000") == self.UTC_2026_01_01_12
        assert parse_xmltv_time("20260101") == int(
            datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000
        )

    @pytest.mark.parametrize("bad", [
        None, "", "garbage", "2026", "20261301120000 +0000", "20260101250000 +0000",
        "20260230120000", "20260101120000 +9999", "2026-01-01 12:00:00",
    ])
    def test_invalid_values_return_none(self, bad):
        assert parse_xmltv_time(bad) is None


# ─── parsing + mapping + window ─────────────────────────────────────────────


def _full_fixture() -> str:
    return xmltv(
        prog("ch1", xt(NOW_MS - 30 * MIN), xt(NOW_MS + 30 * MIN), "Journal", desc="Le JT", lang="fr"),
        prog("ch1", xt(NOW_MS + 30 * MIN), xt(NOW_MS + 90 * MIN), "Meteo"),
        # same instants as a +0530 offset programme
        prog("ch1", xt(NOW_MS + 100 * MIN, 330), xt(NOW_MS + 130 * MIN, 330), "Offset"),
        # case-differing channel id (LiveChannel has "Ch2"); started before the
        # window but still airing -> kept (overlap semantics)
        prog("CH2", xt(NOW_MS - 3 * 60 * MIN), xt(NOW_MS + 10 * MIN), "Film en cours"),
        prog("ch2", xt(NOW_MS + 10 * MIN), xt(NOW_MS + 20 * MIN), None),  # no <title>
        prog("ch1", xt(NOW_MS - 3 * 60 * MIN), xt(NOW_MS - 2 * 60 * MIN), "Trop tot"),
        prog("ch1", xt(NOW_MS + 13 * 60 * MIN), xt(NOW_MS + 14 * 60 * MIN), "Trop tard"),
        prog("zzz", xt(NOW_MS), xt(NOW_MS + 10 * MIN), "Inconnue"),
        prog("ch1", None, None, "Date KO", raw='start="garbage" stop="garbage"'),
        prog("ch1", None, None, "Sans stop", raw=f'start="{xt(NOW_MS)}"'),
        prog("ch1", xt(NOW_MS + 5 * MIN), xt(NOW_MS + 5 * MIN), "Duree nulle"),
    )


async def test_parsing_mapping_window_and_fanout(db_factory, monkeypatch):
    sid = "xtream_a"
    await seed(
        db_factory, account("a"),
        channel(10, sid, "ch1"), channel(11, sid, "CH1 "),  # two variants, same epg id
        channel(20, sid, "Ch2"),
        channel(30, sid, None),            # no epg id: never mapped
        channel(40, sid, "zzz", allowed=False),  # hidden category: not fetched for
    )
    fake_download(monkeypatch, {"a": _full_fixture()})

    report = await run_epg_sync(db_factory)

    assert report.accounts_ok == 1 and report.accounts_failed == 0
    acc = report.accounts[0]
    assert acc.status == "ok"
    assert acc.programmes_seen == 11
    assert acc.skipped_invalid == 3          # garbage date, no stop, stop == start
    assert acc.skipped_out_of_window == 2    # too early / too late
    assert acc.skipped_unmapped == 1         # "zzz" belongs to a hidden channel
    # ch1 programmes (3) x 2 stream_ids + ch2 programmes (2) x 1 stream_id
    assert acc.rows_emitted == 8
    assert (acc.inserted, acc.updated, acc.unchanged) == (8, 0, 0)

    stored = await rows(db_factory)
    assert len(stored) == 8
    by = {(r.stream_id, r.title): r for r in stored}

    journal = by[(10, "Journal")]
    assert journal.server_id == sid
    assert journal.epg_channel_id == "ch1"
    assert journal.start_time == NOW_MS - 30 * MIN
    assert journal.end_time == NOW_MS + 30 * MIN
    assert journal.description == "Le JT" and journal.lang == "fr"
    assert journal.fetched_at == NOW_MS
    # the variant stream_id gets the same programme, keeping ITS epg id spelling
    assert by[(11, "Journal")].epg_channel_id == "CH1 "
    assert by[(11, "Journal")].start_time == journal.start_time

    # +0530 offset normalised to the same epoch instants
    assert by[(10, "Offset")].start_time == NOW_MS + 100 * MIN
    assert by[(10, "Offset")].end_time == NOW_MS + 130 * MIN

    # started before the window, still airing: kept; case-insensitive channel id
    assert by[(20, "Film en cours")].start_time == NOW_MS - 3 * 60 * MIN
    assert by[(20, "Unknown")].description is None

    assert not any(r.stream_id in (30, 40) for r in stored)
    assert not any(r.title in {"Trop tot", "Trop tard", "Inconnue", "Date KO", "Sans stop"}
                   for r in stored)


async def test_xmltv_text_is_plain_not_base64_decoded(db_factory, monkeypatch):
    """Short-EPG titles are base64 in the Xtream JSON; XMLTV text is plain. A
    title that merely LOOKS like base64 must be stored verbatim."""
    await seed(db_factory, account("a"), channel(10, "xtream_a", "ch1"))
    looks_b64 = "TEST"  # valid base64 alphabet, would decode to garbage bytes
    fake_download(monkeypatch, {"a": xmltv(
        prog("ch1", xt(NOW_MS), xt(NOW_MS + MIN), looks_b64),
    )})
    await run_epg_sync(db_factory)
    assert [r.title for r in await rows(db_factory)] == ["TEST"]


# ─── idempotence / upsert ───────────────────────────────────────────────────


async def test_two_runs_do_not_duplicate_and_unchanged_rows_are_not_rewritten(
    db_factory, monkeypatch,
):
    await seed(db_factory, account("a"), channel(10, "xtream_a", "ch1"), channel(11, "xtream_a", "ch1"))
    fake_download(monkeypatch, {"a": _full_fixture()})

    first = await run_epg_sync(db_factory)
    before = await rows(db_factory)
    second = await run_epg_sync(db_factory)
    after = await rows(db_factory)

    assert first.inserted == 6 and first.updated == 0     # 3 ch1 programmes x 2 streams
    assert (second.inserted, second.updated, second.unchanged) == (0, 0, 6)
    assert [r.id for r in before] == [r.id for r in after]
    assert len(after) == 6


async def test_changed_programme_is_updated_in_place(db_factory, monkeypatch):
    await seed(db_factory, account("a"), channel(10, "xtream_a", "ch1"))
    start, stop = xt(NOW_MS + 10 * MIN), xt(NOW_MS + 40 * MIN)
    fake_download(monkeypatch, {"a": xmltv(prog("ch1", start, stop, "Titre v1", desc="d1"))})
    await run_epg_sync(db_factory)
    original = (await rows(db_factory))[0]

    new_stop = xt(NOW_MS + 50 * MIN)
    fake_download(monkeypatch, {"a": xmltv(prog("ch1", start, new_stop, "Titre v2", desc="d2"))})
    report = await run_epg_sync(db_factory)

    assert (report.inserted, report.updated, report.unchanged) == (0, 1, 0)
    stored = await rows(db_factory)
    assert len(stored) == 1
    assert stored[0].id == original.id
    assert stored[0].title == "Titre v2" and stored[0].description == "d2"
    assert stored[0].end_time == NOW_MS + 50 * MIN


async def test_repeated_programme_in_one_file_yields_one_row(db_factory, monkeypatch):
    await seed(db_factory, account("a"), channel(10, "xtream_a", "ch1"))
    start, stop = xt(NOW_MS + MIN), xt(NOW_MS + 2 * MIN)
    fake_download(monkeypatch, {"a": xmltv(
        prog("ch1", start, stop, "premier"), prog("ch1", start, stop, "dernier"),
    )})
    report = await run_epg_sync(db_factory)
    stored = await rows(db_factory)
    assert [r.title for r in stored] == ["dernier"]
    assert report.accounts[0].inserted == 1


async def test_short_epg_and_bulk_sync_share_the_dedup_key(db_factory, monkeypatch):
    """A row first written by the on-demand path is updated, not duplicated, by
    the bulk sync (same (server_id, stream_id, start_time))."""
    await seed(
        db_factory, account("a"), channel(10, "xtream_a", "ch1"),
        EpgEntry(
            server_id="xtream_a", epg_channel_id="ch1", stream_id=10, title="court",
            start_time=NOW_MS + MIN, end_time=NOW_MS + 2 * MIN,
        ),
    )
    fake_download(monkeypatch, {"a": xmltv(
        prog("ch1", xt(NOW_MS + MIN), xt(NOW_MS + 2 * MIN), "complet", desc="desc"),
    )})
    await run_epg_sync(db_factory)
    stored = await rows(db_factory)
    assert len(stored) == 1 and stored[0].title == "complet"


# ─── caps ───────────────────────────────────────────────────────────────────


async def test_programme_cap_stops_the_account_cleanly(db_factory, monkeypatch):
    monkeypatch.setattr(settings, "EPG_SYNC_MAX_PROGRAMMES", 3)
    monkeypatch.setattr(settings, "EPG_SYNC_BATCH_SIZE", 2)
    await seed(db_factory, account("a"), channel(10, "xtream_a", "ch1"))
    fake_download(monkeypatch, {"a": xmltv(*[
        prog("ch1", xt(NOW_MS + i * MIN), xt(NOW_MS + (i + 1) * MIN), f"P{i}") for i in range(10)
    ])})
    report = await run_epg_sync(db_factory)
    acc = report.accounts[0]
    assert acc.status == "ok" and acc.truncated is True
    assert acc.rows_emitted == 3 and acc.inserted == 3
    assert len(await rows(db_factory)) == 3


async def test_exact_cap_is_not_reported_as_truncated(db_factory, monkeypatch):
    monkeypatch.setattr(settings, "EPG_SYNC_MAX_PROGRAMMES", 3)
    await seed(db_factory, account("a"), channel(10, "xtream_a", "ch1"))
    fake_download(monkeypatch, {"a": xmltv(*[
        prog("ch1", xt(NOW_MS + i * MIN), xt(NOW_MS + (i + 1) * MIN), f"P{i}") for i in range(3)
    ])})
    acc = (await run_epg_sync(db_factory)).accounts[0]
    assert acc.truncated is False and acc.inserted == 3


async def test_size_cap_failure_is_isolated_and_reported(db_factory, monkeypatch):
    from app.services.xtream_service import XmltvTooLargeError

    await seed(
        db_factory, account("a"), account("b"),
        channel(10, "xtream_a", "ch1"), channel(10, "xtream_b", "ch1"),
    )
    fake_download(monkeypatch, {
        "a": XmltvTooLargeError("XMLTV body exceeds max_bytes=1"),
        "b": xmltv(prog("ch1", xt(NOW_MS + MIN), xt(NOW_MS + 2 * MIN), "ok")),
    })
    report = await run_epg_sync(db_factory)
    by_id = {a.account_id: a for a in report.accounts}
    assert by_id["a"].status == "failed" and "max_bytes" in by_id["a"].error
    assert by_id["b"].status == "ok" and by_id["b"].inserted == 1


# ─── failure isolation, secrets, temp files ─────────────────────────────────


async def test_one_failing_account_does_not_stop_the_others(db_factory, monkeypatch):
    await seed(
        db_factory, account("a"), account("b"), account("c"),
        channel(10, "xtream_a", "ch1"), channel(10, "xtream_b", "ch1"), channel(10, "xtream_c", "ch1"),
    )
    good = xmltv(prog("ch1", xt(NOW_MS + MIN), xt(NOW_MS + 2 * MIN), "ok"))
    fake_download(monkeypatch, {
        "a": good, "b": httpx.ConnectError("boom"), "c": good,
    })
    report = await run_epg_sync(db_factory)
    assert [a.status for a in report.accounts] == ["ok", "failed", "ok"]
    assert report.accounts_ok == 2 and report.accounts_failed == 1
    assert report.accounts[1].error == "ConnectError"
    assert report.inserted == 2


async def test_http_error_text_never_leaks_credentials(db_factory, monkeypatch):
    """httpx puts the full request URL (username/password query params) in the
    HTTPStatusError message: neither the report nor the logs may carry it."""
    await seed(db_factory, account("a"), channel(10, "xtream_a", "ch1"))
    url = f"http://a.example/xmltv.php?username=user&password={SECRET}"
    request = httpx.Request("GET", url)
    err = httpx.HTTPStatusError(
        f"Server error '500' for url '{url}'", request=request,
        response=httpx.Response(500, request=request),
    )
    fake_download(monkeypatch, {"a": err})
    with Records("plexhub") as logs:
        report = await run_epg_sync(db_factory)

    acc = report.accounts[0]
    assert acc.status == "failed" and acc.error == "HTTPStatusError: HTTP 500"
    assert SECRET not in repr(report)
    assert "EPG sync failed for account a" in logs.text, "the failure must have been logged"
    assert SECRET not in logs.text


async def test_invalid_xml_fails_the_account_but_keeps_already_written_batches(
    db_factory, monkeypatch,
):
    monkeypatch.setattr(settings, "EPG_SYNC_BATCH_SIZE", 1)
    await seed(db_factory, account("a"), account("b"),
               channel(10, "xtream_a", "ch1"), channel(10, "xtream_b", "ch1"))
    broken = xmltv(
        prog("ch1", xt(NOW_MS + MIN), xt(NOW_MS + 2 * MIN), "p1"),
        prog("ch1", xt(NOW_MS + 3 * MIN), xt(NOW_MS + 4 * MIN), "p2"),
    ).replace("</tv>", "<programme start=") # truncated mid-tag
    fake_download(monkeypatch, {
        "a": broken,
        "b": xmltv(prog("ch1", xt(NOW_MS + MIN), xt(NOW_MS + 2 * MIN), "fine")),
    })
    report = await run_epg_sync(db_factory)
    by_id = {a.account_id: a for a in report.accounts}
    assert by_id["a"].status == "failed" and by_id["a"].error.startswith("ParseError")
    assert by_id["a"].inserted == 2          # idempotent partial progress is kept
    assert by_id["b"].status == "ok"
    titles = sorted(r.title for r in await rows(db_factory))
    assert titles == ["fine", "p1", "p2"]


async def test_entity_declarations_are_refused(db_factory, monkeypatch):
    await seed(db_factory, account("a"), channel(10, "xtream_a", "ch1"))
    evil = (
        '<?xml version="1.0"?>\n<!DOCTYPE tv [<!ENTITY x "boom">]>\n'
        f'<tv>{prog("ch1", xt(NOW_MS), xt(NOW_MS + MIN), "&x;")}</tv>'
    )
    fake_download(monkeypatch, {"a": evil})
    acc = (await run_epg_sync(db_factory)).accounts[0]
    assert acc.status == "failed" and "entities" in acc.error
    assert await rows(db_factory) == []


@pytest.mark.parametrize("payload", ["ok", "broken", RuntimeError("x")])
async def test_temp_file_is_always_removed(db_factory, monkeypatch, payload):
    import os

    await seed(db_factory, account("a"), channel(10, "xtream_a", "ch1"))
    body = {
        "ok": xmltv(prog("ch1", xt(NOW_MS + MIN), xt(NOW_MS + 2 * MIN), "t")),
        "broken": "<tv><programme",
    }.get(payload, payload) if isinstance(payload, str) else payload
    calls: list = []
    fake_download(monkeypatch, {"a": body}, calls)
    await run_epg_sync(db_factory)
    assert len(calls) == 1
    assert not os.path.exists(calls[0][1])


# ─── account selection ──────────────────────────────────────────────────────


async def test_account_in_confirmed_outage_is_skipped_without_a_request(db_factory, monkeypatch):
    from app.services import account_outage_service

    acc_down = account("down")
    acc_down.outage_strikes = settings.ACCOUNT_OUTAGE_STRIKES
    await seed(
        db_factory, acc_down, account("up"),
        channel(10, "xtream_down", "ch1"), channel(10, "xtream_up", "ch1"),
    )
    account_outage_service.invalidate_mask_cache()
    calls: list = []
    fake_download(monkeypatch, {
        "up": xmltv(prog("ch1", xt(NOW_MS + MIN), xt(NOW_MS + 2 * MIN), "t")),
    }, calls)

    report = await run_epg_sync(db_factory)

    assert [c[0] for c in calls] == ["up"], "a masked provider must not be hit at all"
    by_id = {a.account_id: a for a in report.accounts}
    assert by_id["down"].status == "skipped_outage"
    assert report.accounts_skipped == 1 and by_id["up"].inserted == 1


async def test_inactive_account_is_ignored(db_factory, monkeypatch):
    await seed(db_factory, account("off", active=False), channel(10, "xtream_off", "ch1"))
    calls: list = []
    fake_download(monkeypatch, {}, calls)
    report = await run_epg_sync(db_factory)
    assert report.accounts == [] and calls == []


async def test_account_without_syncable_channels_does_not_download(db_factory, monkeypatch):
    await seed(
        db_factory, account("a"),
        channel(10, "xtream_a", None), channel(11, "xtream_a", "ch1", allowed=False),
    )
    calls: list = []
    fake_download(monkeypatch, {}, calls)
    report = await run_epg_sync(db_factory)
    assert report.accounts[0].status == "no_channels" and calls == []
    assert report.accounts_ok == 1


async def test_adult_channel_follows_the_live_routes_rule(db_factory, monkeypatch):
    """`GET /api/live/channels` filters ONLY on is_in_allowed_categories; the
    is_adult flag is exposed, not hidden. The sync applies the same rule: an
    adult channel in an allowed category is synced, one in a hidden category is
    not."""
    sid = "xtream_a"
    await seed(
        db_factory, account("a"),
        channel(10, sid, "adult-ok", adult=True, allowed=True),
        channel(11, sid, "adult-hidden", adult=True, allowed=False),
    )
    fake_download(monkeypatch, {"a": xmltv(
        prog("adult-ok", xt(NOW_MS + MIN), xt(NOW_MS + 2 * MIN), "visible"),
        prog("adult-hidden", xt(NOW_MS + MIN), xt(NOW_MS + 2 * MIN), "hidden"),
        channels=("adult-ok", "adult-hidden"),
    )})
    await run_epg_sync(db_factory)
    assert [(r.stream_id, r.title) for r in await rows(db_factory)] == [(10, "visible")]


async def test_served_by_the_live_routes_after_sync(api_client, db_factory, monkeypatch):
    """End to end: after the bulk sync, `GET /api/live/epg` (cache-only route)
    returns the now-playing programme for a channel nobody ever opened."""
    from app.db import database as db_module
    from app.services import api_key_service

    # Same wiring as tests/test_router_http_coverage.py: point get_db and the
    # API-key resolver at the isolated in-memory engine.
    monkeypatch.setattr(db_module, "async_session_factory", db_factory)
    monkeypatch.setattr(api_key_service, "async_session_factory", db_factory)
    monkeypatch.setattr(settings, "AI_API_KEY", "test-key-epg-sync")
    api_headers = {"X-API-Key": "test-key-epg-sync"}

    await seed(db_factory, account("a"), channel(10, "xtream_a", "ch1"))
    fake_download(monkeypatch, {"a": xmltv(
        prog("ch1", xt(NOW_MS - 10 * MIN), xt(NOW_MS + 10 * MIN), "En ce moment"),
    )})
    await run_epg_sync(db_factory)

    # The route compares against the real clock: use a window around real now.
    real_now = int(time.time() * 1000)
    async with db_factory() as s:
        row = (await s.execute(select(EpgEntry))).scalars().one()
        row.start_time, row.end_time = real_now - 10 * MIN, real_now + 10 * MIN
        await s.commit()
    resp = await api_client.get(
        "/api/live/epg", params={"server_id": "xtream_a"}, headers=api_headers,
    )
    assert resp.status_code == 200, resp.text
    assert [i["title"] for i in resp.json()["items"]] == ["En ce moment"]


# ─── pruning of ended programmes ────────────────────────────────────────────


def _row(server_id, stream_id, start, end, title="old"):
    return EpgEntry(
        server_id=server_id, epg_channel_id="ch1", stream_id=stream_id, title=title,
        start_time=start, end_time=end,
    )


async def test_successful_sync_prunes_programmes_that_ended_before_the_window(
    db_factory, monkeypatch,
):
    hour = 60 * MIN
    await seed(
        db_factory, account("a"), channel(10, "xtream_a", "ch1"),
        _row("xtream_a", 10, NOW_MS - 5 * hour, NOW_MS - 4 * hour, "ended long ago"),
        _row("xtream_a", 10, NOW_MS - 2 * hour, NOW_MS - 90 * MIN, "ended before window"),
        _row("xtream_a", 10, NOW_MS - 50 * MIN, NOW_MS - 20 * MIN, "ended inside past hour"),
        # another server's stale rows are not this sync's business
        _row("xtream_other", 10, NOW_MS - 5 * hour, NOW_MS - 4 * hour, "other server"),
    )
    fake_download(monkeypatch, {"a": xmltv(
        prog("ch1", xt(NOW_MS + MIN), xt(NOW_MS + 2 * MIN), "fresh"),
    )})

    report = await run_epg_sync(db_factory)

    assert report.accounts[0].pruned == 2 and report.pruned == 2
    titles = sorted(r.title for r in await rows(db_factory))
    assert titles == ["ended inside past hour", "fresh", "other server"]


async def test_prune_runs_in_several_short_transactions_by_stream_range(db_factory, monkeypatch):
    """2 streams per transaction over 5 stale streams = 3 delete transactions,
    each one a contiguous stream_id range; everything stale is still removed."""
    monkeypatch.setattr(svc, "_PRUNE_STREAMS_PER_TXN", 2)
    hour = 60 * MIN
    seen_ranges: list[str] = []
    real_write = svc.write_with_retry

    async def _spy(work, **kw):
        if kw.get("op") == "epg_sync.prune":
            seen_ranges.append(kw["op"])
        return await real_write(work, **kw)

    monkeypatch.setattr(svc, "write_with_retry", _spy)
    await seed(
        db_factory, account("a"), channel(10, "xtream_a", "ch1"),
        *[_row("xtream_a", sid, NOW_MS - 9 * hour + i * MIN, NOW_MS - 8 * hour + i * MIN, f"o{sid}-{i}")
          for sid in (3, 4, 5, 6, 7) for i in range(3)],
        # a recent row of one of those streams must survive the range delete
        _row("xtream_a", 5, NOW_MS + MIN, NOW_MS + 2 * MIN, "still airing"),
    )
    fake_download(monkeypatch, {"a": xmltv(
        prog("ch1", xt(NOW_MS + 10 * MIN), xt(NOW_MS + 11 * MIN), "fresh"),
    )})
    report = await run_epg_sync(db_factory)
    assert report.accounts[0].pruned == 15
    assert len(seen_ranges) == 3, "5 stale streams / 2 per txn = 3 short transactions"
    assert sorted(r.title for r in await rows(db_factory)) == ["fresh", "still airing"]


async def test_failed_account_is_not_pruned(db_factory, monkeypatch):
    await seed(
        db_factory, account("a"), channel(10, "xtream_a", "ch1"),
        _row("xtream_a", 10, NOW_MS - 9 * 60 * MIN, NOW_MS - 8 * 60 * MIN, "keep me"),
    )
    fake_download(monkeypatch, {"a": httpx.ConnectError("down")})
    report = await run_epg_sync(db_factory)
    assert report.accounts[0].status == "failed" and report.accounts[0].pruned == 0
    assert [r.title for r in await rows(db_factory)] == ["keep me"]


async def test_prune_failure_does_not_fail_the_account(db_factory, monkeypatch):
    await seed(db_factory, account("a"), channel(10, "xtream_a", "ch1"))
    fake_download(monkeypatch, {"a": xmltv(
        prog("ch1", xt(NOW_MS + MIN), xt(NOW_MS + 2 * MIN), "fresh"),
    )})

    async def _boom(*a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(svc, "_prune_ended", _boom)
    with Records("plexhub.epg_sync", logging.WARNING) as logs:
        report = await run_epg_sync(db_factory)

    acc = report.accounts[0]
    assert acc.status == "ok" and acc.inserted == 1 and acc.pruned == 0
    assert "prune failed" in logs.text


async def test_batch_pause_setting_is_applied_between_batches(db_factory, monkeypatch):
    monkeypatch.setattr(settings, "EPG_SYNC_BATCH_SIZE", 2)
    monkeypatch.setattr(settings, "EPG_SYNC_BATCH_PAUSE_MS", 7)
    await seed(db_factory, account("a"), channel(10, "xtream_a", "ch1"))
    fake_download(monkeypatch, {"a": xmltv(*[
        prog("ch1", xt(NOW_MS + i * MIN), xt(NOW_MS + (i + 1) * MIN), f"P{i}") for i in range(5)
    ])})

    import asyncio

    real_sleep = asyncio.sleep
    pauses: list[float] = []

    async def _spy(delay, *a, **k):
        pauses.append(delay)
        return await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", _spy)
    report = await run_epg_sync(db_factory)

    assert report.accounts[0].inserted == 5
    # 5 rows / 2 per batch = 3 batches -> 3 inter-batch pauses of 7 ms
    assert pauses.count(0.007) == 3
