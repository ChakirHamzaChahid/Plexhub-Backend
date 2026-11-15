"""Admin web UI — HTMX + Jinja2 catalogue editor.

Mounted at `/admin`, separate from the JSON API (`/api/...`). Routes here render
HTML fragments rather than JSON; they call the same `media_service` functions
the API uses, so business logic isn't duplicated.
"""
import asyncio
import json
import logging
import re
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import case, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession


logger = logging.getLogger("plexhub.api.admin")

from app.config import settings
from app.db.database import async_session_factory, get_db
from app.models.database import MetadataEdit, ScrapeCacheEntry, XtreamAccount, XtreamCategory
from app.models.schemas import AccountCreate, MediaUpdate
from app.services.media_service import media_service
from app.services import (
    account_outage_service,
    account_service,
    api_key_service,
    backup_status_service,
    category_service,
    job_registry,
    manual_scrape_service,
    nfo_import_service,
    poster_match_service,
)
from app.utils import job_health, tasks as app_tasks
from app.utils.db_retry import session_factory_for, write_with_retry
from app.utils.ssrf import SsrfBlockedError
from app.utils.string_normalizer import clean_title
from app.utils.tasks import create_background_task
from app.utils.time import now_ms
from app.workers import manual_scrape_batch_worker


router = APIRouter(prefix="/admin", tags=["admin"])

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _fmt_ms(ms: Optional[int]) -> str:
    """Epoch-ms -> local 'YYYY-MM-DD HH:MM' for templates ({{ value | ms }})."""
    if not ms:
        return "—"
    from datetime import datetime, timezone
    return (
        datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        .astimezone()
        .strftime("%Y-%m-%d %H:%M")
    )


def _css_id(value: object) -> str:
    """Escape an id so it is usable in a CSS selector ({{ id | css_id }}).

    Element ids here embed a `rating_key`, and a movie/episode rating_key
    carries the file extension (`vod_439568.mkv`, `sync_worker.py:115,347`).
    `#row-xtream_1-vod_439568.mkv` is parsed by `querySelector` as "id
    row-xtream_1-vod_439568 with class mkv" — no match, so htmx raises
    `htmx:targetError` and the swap is silently lost (verified in-browser:
    Sauver/Appliquer/Re-scrape did nothing at all on every movie row).
    The id attribute itself stays raw; only SELECTORS go through this."""
    return re.sub(r"([^a-zA-Z0-9_-])", r"\\\1", str(value))


templates.env.filters["ms"] = _fmt_ms
templates.env.filters["css_id"] = _css_id


_ID_FILTERS = (
    "all", "missing_imdb", "missing_tmdb", "missing_both", "incomplete",
    "locked", "batch", "review",
)


def _norm_type(value: Optional[str]) -> str:
    """Query/form `type` -> `Media.type` vocabulary. Anything unknown falls
    back to "movie" rather than 422-ing a browse URL."""
    return "show" if (value or "").lower() in ("show", "series", "tv") else "movie"


def _norm_ids(value: Optional[str]) -> str:
    return value if value in _ID_FILTERS else "incomplete"


async def _catalogue_ctx(
    db: AsyncSession,
    *,
    media_type: str,
    ids: str,
    search: Optional[str],
    sort: str,
    page: int,
    page_size: int,
) -> dict:
    page_obj = await manual_scrape_service.list_catalogue(
        db, media_type=media_type, id_filter=ids, search=search or None,
        sort=sort, page=page, page_size=page_size,
    )
    return {
        "items": page_obj.items,
        "total": page_obj.total,
        "offset": page_obj.offset,
        "page": page,
        "page_size": page_size,
        "media_type": media_type,
        "ids": ids,
        "search": search or "",
        "sort": sort,
    }


async def _stats_ctx(db: AsyncSession) -> dict:
    stats = await manual_scrape_service.catalogue_stats(db)
    return {
        "stats": stats,
        "movie_stats": stats["movie"],
        "show_stats": stats["show"],
        "outages": await account_outage_service.list_outages(db),
        "backup_status": backup_status_service.compute_status(now_ms()),
    }


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def admin_index(
    request: Request,
    type: str = Query("movie"),  # noqa: A002 — query name fixed by ADR 0005 D10
    ids: str = Query("incomplete"),
    search: Optional[str] = Query(None),
    sort: str = Query("added_desc"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=10, le=500),
    db: AsyncSession = Depends(get_db),
):
    ctx = await _catalogue_ctx(
        db, media_type=_norm_type(type), ids=_norm_ids(ids),
        search=search, sort=sort, page=page, page_size=page_size,
    )
    ctx.update(await _stats_ctx(db))
    # Reloading /admin while a batch runs must show that batch (with its
    # polling and its Cancel button), not "Aucun lot lancé" followed by a
    # 409 on the next Lancer.
    ctx["job"] = manual_scrape_batch_worker.get_latest()
    return templates.TemplateResponse(request, "admin/index.html", ctx)


@router.get("/catalogue", response_class=HTMLResponse)
async def admin_catalogue_fragment(
    request: Request,
    type: str = Query("movie"),  # noqa: A002
    ids: str = Query("incomplete"),
    search: Optional[str] = Query(None),
    sort: str = Query("added_desc"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=10, le=500),
    db: AsyncSession = Depends(get_db),
):
    ctx = await _catalogue_ctx(
        db, media_type=_norm_type(type), ids=_norm_ids(ids),
        search=search, sort=sort, page=page, page_size=page_size,
    )
    return templates.TemplateResponse(request, "admin/_media_table.html", ctx)


@router.get("/stats", response_class=HTMLResponse)
async def admin_stats_fragment(
    request: Request, db: AsyncSession = Depends(get_db),
):
    return templates.TemplateResponse(request, "admin/_stats.html", await _stats_ctx(db))


@router.post("/movies/{rating_key}/ids", response_class=HTMLResponse)
async def admin_update_ids(
    rating_key: str,
    request: Request,
    server_id: str = Form(...),
    imdb_id: Optional[str] = Form(None),
    tmdb_id: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
):
    """Update one or both external IDs from a row form. Fields not submitted
    (i.e. None) are left untouched; empty string clears them."""
    payload_data: dict = {}
    if imdb_id is not None:
        payload_data["imdb_id"] = imdb_id
    if tmdb_id is not None:
        payload_data["tmdb_id"] = tmdb_id

    try:
        payload = MediaUpdate(**payload_data)
    except ValueError as exc:
        item = await media_service.get_media_by_key(db, rating_key, server_id)
        if not item:
            raise HTTPException(404, "Media not found")
        return templates.TemplateResponse(
            request,
            "admin/_media_row.html",
            {"item": item, "error": str(exc)},
            status_code=422,
        )

    fields = {
        k: v for k, v in payload.model_dump(exclude_unset=True).items()
        if k in ("imdb_id", "tmdb_id")
    }
    updated = await media_service.update_external_ids(
        db, rating_key, server_id, fields=fields,
    )
    if not updated:
        raise HTTPException(404, "Media not found")

    return templates.TemplateResponse(
        request,
        "admin/_media_row.html",
        {"item": updated, "saved": True},
        headers={"HX-Trigger": "refresh-stats"},
    )


@router.post("/movies/{rating_key}/rescrape", response_class=HTMLResponse)
async def admin_rescrape(
    rating_key: str,
    request: Request,
    server_id: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    outcome = await media_service.enqueue_rescrape(db, rating_key, server_id)
    if outcome == "not_found":
        raise HTTPException(404, "Media not found")
    item = await media_service.get_media_by_key(db, rating_key, server_id)
    if outcome == "locked":
        # ADR 0005 D7/L9: show the locked state instead of silently queueing
        # nothing — unlocking is a manual-scraper action (W2), not available yet.
        return templates.TemplateResponse(
            request,
            "admin/_media_row.html",
            {"item": item, "locked_rescrape": True},
        )
    return templates.TemplateResponse(
        request,
        "admin/_media_row.html",
        {"item": item, "rescraped": True},
        headers={"HX-Trigger": "refresh-stats"},
    )


# ──────────────────────────────────────────────────────────────────────────────
# Manual scraper (ADR 0005 D10, W2) — apply/unlock/clear/rescrape by
# (server_id, rating_key). Writes go through `manual_scrape_service`, which
# uses its OWN fresh sessions (`write_with_retry`/`load_row`) rather than the
# request-scoped `db: AsyncSession = Depends(get_db)` used elsewhere in this
# module — see `manual_scrape_service.load_row`'s docstring (ADR 0005 D6):
# re-reading through the request session after a fresh-session write can
# still see a pre-write WAL snapshot.
# ──────────────────────────────────────────────────────────────────────────────


@router.post("/media/{server_id}/{rating_key}/apply", response_class=HTMLResponse)
async def admin_media_apply(
    server_id: str,
    rating_key: str,
    request: Request,
    tmdb_id: Optional[str] = Form(None),
    imdb_id: Optional[str] = Form(None),
    type: str = Form("movie"),  # noqa: A002 — form field name is fixed by the ADR
    force: Optional[str] = Form(None),
    propagate: Optional[str] = Form(None),
):
    media_type = "show" if type == "show" else "movie"

    parsed_tmdb_id: Optional[int] = None
    if tmdb_id and tmdb_id.strip():
        if not tmdb_id.strip().isdigit():
            item = await manual_scrape_service.load_row(
                server_id, rating_key, session_factory=async_session_factory,
            )
            if item is None:
                raise HTTPException(404, "Media not found")
            return templates.TemplateResponse(
                request, "admin/_media_row.html",
                {"item": item, "error": "L'id TMDB doit être numérique."},
                status_code=422,
            )
        parsed_tmdb_id = int(tmdb_id.strip())

    parsed_imdb_id = imdb_id.strip() if imdb_id and imdb_id.strip() else None

    if parsed_tmdb_id is None and parsed_imdb_id is None:
        item = await manual_scrape_service.load_row(
            server_id, rating_key, session_factory=async_session_factory,
        )
        if item is None:
            raise HTTPException(404, "Media not found")
        return templates.TemplateResponse(
            request, "admin/_media_row.html",
            {"item": item, "error": "Indique un id IMDb ou un id TMDB."},
            status_code=422,
        )

    try:
        outcome = await manual_scrape_service.apply_candidate(
            server_id=server_id, rating_key=rating_key, media_type=media_type,
            tmdb_id=parsed_tmdb_id, imdb_id=parsed_imdb_id,
            source="manual", force=bool(force), propagate=bool(propagate),
            session_factory=async_session_factory,
        )
    except OperationalError as exc:
        # SQLite has a single writer. When a pipeline pass (or a snapshot
        # rebuild) holds it, all three `write_with_retry` attempts can burn a
        # full `busy_timeout=60s` each and the write never lands. Answering
        # 500 made the button look DEAD: htmx 1.x swaps no 4xx/5xx except the
        # 409/422 the layout opts in, so the operator saw nothing at all and
        # could chain right past an item that was never written.
        if "database is locked" not in str(exc).lower():
            raise
        item = await manual_scrape_service.load_row(
            server_id, rating_key, session_factory=async_session_factory,
        )
        if item is None:
            raise HTTPException(404, "Media not found") from None
        logger.warning(
            "manual apply gave up on a locked database for %s/%s",
            server_id, rating_key,
        )
        return templates.TemplateResponse(
            request, "admin/_media_row.html",
            {
                "item": item,
                "error": (
                    "Base occupée par une écriture concurrente — rien n'a été "
                    "enregistré. Réessaie dans un instant."
                ),
            },
            status_code=409,
        )

    if outcome.status == "not_found":
        raise HTTPException(404, "Media not found")

    item = await manual_scrape_service.load_row(
        server_id, rating_key, session_factory=async_session_factory,
    )
    if item is None:
        raise HTTPException(404, "Media not found")

    if outcome.status == "applied":
        # `refresh-review` too (W5): applying an identity resolves this
        # item's review row inside the same write, so the "À vérifier" list
        # is now stale. Both listeners exist (`_stats.html`, `index.html`).
        # `close-scrape-panel` closes the drawer — an out-of-band swap can't
        # (this response is a <tr> fragment; a sibling <div> is
        # foster-parented out of the table and breaks the whole swap).
        return templates.TemplateResponse(
            request, "admin/_media_row.html",
            {"item": item, "applied": True, "outcome": outcome},
            headers={
                "HX-Trigger": "refresh-stats, refresh-review, close-scrape-panel",
            },
        )
    if outcome.status == "conflict":
        return templates.TemplateResponse(
            request, "admin/_media_row.html",
            {"item": item, "conflict": True, "outcome": outcome},
            status_code=409,
        )
    if outcome.status == "provider_not_found":
        return templates.TemplateResponse(
            request, "admin/_media_row.html",
            {"item": item, "error": "Aucun résultat trouvé pour cet identifiant."},
            status_code=422,
        )
    # skipped_locked
    return templates.TemplateResponse(
        request, "admin/_media_row.html",
        {"item": item, "locked_rescrape": True},
        status_code=409,
    )


@router.post("/media/{server_id}/{rating_key}/unlock", response_class=HTMLResponse)
async def admin_media_unlock(server_id: str, rating_key: str, request: Request):
    ok = await manual_scrape_service.unlock(
        server_id, rating_key, session_factory=async_session_factory,
    )
    if not ok:
        raise HTTPException(404, "Media not found")
    item = await manual_scrape_service.load_row(
        server_id, rating_key, session_factory=async_session_factory,
    )
    if item is None:
        raise HTTPException(404, "Media not found")
    return templates.TemplateResponse(
        request, "admin/_media_row.html",
        {"item": item, "unlocked": True},
        headers={"HX-Trigger": "refresh-stats"},
    )


@router.post("/media/{server_id}/{rating_key}/clear", response_class=HTMLResponse)
async def admin_media_clear(
    server_id: str, rating_key: str, request: Request,
    lock: Optional[str] = Form(None),
):
    ok = await manual_scrape_service.clear_ids(
        server_id, rating_key, lock=bool(lock), session_factory=async_session_factory,
    )
    if not ok:
        raise HTTPException(404, "Media not found")
    item = await manual_scrape_service.load_row(
        server_id, rating_key, session_factory=async_session_factory,
    )
    if item is None:
        raise HTTPException(404, "Media not found")
    return templates.TemplateResponse(
        request, "admin/_media_row.html",
        {"item": item, "cleared": True},
        headers={"HX-Trigger": "refresh-stats"},
    )


@router.post("/media/{server_id}/{rating_key}/rescrape", response_class=HTMLResponse)
async def admin_media_rescrape(
    server_id: str, rating_key: str, request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Same `enqueue_rescrape` behaviour as `/admin/movies/{rk}/rescrape`
    (ADR 0005 L8/L9), just addressed by (server_id, rating_key) in the path
    instead of a form field — the W2 companion of the new `_media_row.html`
    manual-scraper controls."""
    outcome = await media_service.enqueue_rescrape(db, rating_key, server_id)
    if outcome == "not_found":
        raise HTTPException(404, "Media not found")
    item = await media_service.get_media_by_key(db, rating_key, server_id)
    if outcome == "locked":
        return templates.TemplateResponse(
            request, "admin/_media_row.html",
            {"item": item, "locked_rescrape": True},
        )
    return templates.TemplateResponse(
        request, "admin/_media_row.html",
        {"item": item, "rescraped": True},
        headers={"HX-Trigger": "refresh-stats"},
    )


# ──────────────────────────────────────────────────────────────────────────────
# Manual scraper, W4: poster proxy + search panel + candidate cards
# ──────────────────────────────────────────────────────────────────────────────


@router.get("/media/{server_id}/{rating_key}/poster")
async def admin_media_poster(server_id: str, rating_key: str, which: str = Query("xtream")):
    """Proxy the media's own poster (ADR 0005 D10).

    The URL is read from the DB row addressed by the media PK — a client
    NEVER supplies a URL (that would be an open SSRF/credential-probing
    proxy). Fetching goes through `poster_match_service`'s dedicated,
    SSRF-vetted client: never `tmdb_service`'s, which injects `api_key` on
    every request (ADR 0005 F2). Any failure (no URL, blocked host, decode/
    size/content-type rejection) is a 404 — the template renders the
    thumbnail with a plain `<img>`, and an error body would be rendered as a
    broken image either way."""
    item = await manual_scrape_service.load_row(
        server_id, rating_key, session_factory=async_session_factory,
    )
    if item is None:
        raise HTTPException(404, "Media not found")
    url = item.resolved_thumb_url if which == "current" else item.thumb_url
    if not url:
        raise HTTPException(404, "No poster for this media")
    try:
        image = await poster_match_service.fetch_image(url)
    except poster_match_service.PosterFetchError:
        # Never echo the exception message/URL: an Xtream poster URL can
        # embed the account credentials.
        raise HTTPException(404, "Poster unavailable") from None
    return Response(
        content=image.content,
        media_type=image.content_type,
        headers={
            "Cache-Control": "private, max-age=86400",
            # GZipMiddleware would otherwise try to re-compress an already
            # compressed image (same guard as the trailer FileResponse).
            "Content-Encoding": "identity",
            # These bytes come from the provider and are served from the
            # /admin origin: never let a browser sniff them into something
            # scriptable. (`poster_match_service` also rejects SVG outright.)
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/media/{server_id}/{rating_key}/scrape", response_class=HTMLResponse)
async def admin_media_scrape_panel(
    server_id: str,
    rating_key: str,
    request: Request,
    auto: int = Query(0),
):
    """The scrape drawer: both posters side by side and a search form
    pre-filled from the row (ADR 0005 D10). No network here — candidates are
    fetched by the `/candidates` route the form targets.

    `auto=1` is set by the post-apply chaining flow only: it makes the search
    form fire on load, so the operator lands on a filled candidate grid. A
    plain click keeps the drawer inert (no provider call until asked)."""
    item = await manual_scrape_service.load_row(
        server_id, rating_key, session_factory=async_session_factory,
    )
    if item is None:
        raise HTTPException(404, "Media not found")
    return templates.TemplateResponse(
        request, "admin/_scrape_panel.html",
        {
            "item": item,
            "media_type": _norm_type(item.type),
            "auto_search": bool(auto),
            # Pre-fill the search box with the cleaned title (see the template).
            "search_title": clean_title(item.title or "")[0],
            "search_year": item.year or clean_title(item.title or "")[1],
        },
    )


@router.get("/media/{server_id}/{rating_key}/candidates", response_class=HTMLResponse)
async def admin_media_candidates(
    server_id: str,
    rating_key: str,
    request: Request,
    title: Optional[str] = Query(None),
    year: Optional[str] = Query(None),
    type: str = Query("movie"),  # noqa: A002
    provider: str = Query("auto"),
):
    item = await manual_scrape_service.load_row(
        server_id, rating_key, session_factory=async_session_factory,
    )
    if item is None:
        raise HTTPException(404, "Media not found")

    parsed_year: Optional[int] = None
    if year and year.strip().isdigit():
        parsed_year = int(year.strip())

    query = manual_scrape_service.ScrapeQuery(
        media_type=_norm_type(type),
        title=(title or item.title or "").strip(),
        year=parsed_year,
        provider=provider if provider in ("auto", "tmdb", "omdb") else "auto",
    )
    result = await manual_scrape_service.search_candidates(
        query, xtream_poster_url=item.thumb_url, summary=item.summary,
    )
    return templates.TemplateResponse(
        request, "admin/_scrape_candidates.html", {"item": item, "result": result},
    )


@router.post("/media/{server_id}/{rating_key}/lookup", response_class=HTMLResponse)
async def admin_media_lookup(
    server_id: str,
    rating_key: str,
    request: Request,
    raw_id: str = Form(...),
    type: str = Form("movie"),  # noqa: A002
):
    item = await manual_scrape_service.load_row(
        server_id, rating_key, session_factory=async_session_factory,
    )
    if item is None:
        raise HTTPException(404, "Media not found")
    try:
        result = await manual_scrape_service.lookup(
            raw_id, media_type=_norm_type(type), xtream_poster_url=item.thumb_url,
        )
    except ValueError:
        return templates.TemplateResponse(
            request, "admin/_scrape_candidates.html",
            {
                "item": item, "result": None,
                "error": "Identifiant illisible — attendu ttXXXXXXX, un id TMDB, "
                         "ou une URL IMDb/TMDB.",
            },
            status_code=422,
        )
    return templates.TemplateResponse(
        request, "admin/_scrape_candidates.html", {"item": item, "result": result},
    )


# ──────────────────────────────────────────────────────────────────────────────
# Manual scraper, W5: batch job + "À vérifier" review queue (ADR 0005 D8/D10)
# ──────────────────────────────────────────────────────────────────────────────


async def _review_ctx(db: AsyncSession, *, media_type: str | None, page: int,
                      page_size: int) -> dict:
    rows, total = await manual_scrape_service.list_reviews(
        db, media_type=media_type, page=page, page_size=page_size,
    )
    return {
        # Candidates come from the STORED JSON, never from a fresh search:
        # rendering this list must cost zero TMDB/OMDb/poster calls however
        # many rows it holds (ADR 0005 D10).
        "reviews": [
            {"row": r, "candidates": manual_scrape_service.review_candidates(r)}
            for r in rows
        ],
        "review_total": total,
        "review_page": page,
        "review_page_size": page_size,
        "review_type": media_type,
    }


@router.get("/review", response_class=HTMLResponse)
async def admin_review_list(
    request: Request,
    type: Optional[str] = Query(None),  # noqa: A002 — name fixed by ADR 0005 D10
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=5, le=200),
    db: AsyncSession = Depends(get_db),
):
    media_type = _norm_type(type) if type else None
    return templates.TemplateResponse(
        request, "admin/_review_list.html",
        await _review_ctx(db, media_type=media_type, page=page, page_size=page_size),
    )


@router.post("/review/{server_id}/{rating_key}/apply", response_class=HTMLResponse)
async def admin_review_apply(
    server_id: str,
    rating_key: str,
    request: Request,
    candidate_index: int = Form(0),
):
    """Apply one of the candidates frozen on the review row.

    The ids come from the STORED candidate at `candidate_index`, not from the
    client: the form only picks an index, so a tampered POST can at worst
    apply a different candidate the batch had already scored for this very
    item — never an arbitrary identity."""
    review = await manual_scrape_service.load_review(
        server_id, rating_key, session_factory=async_session_factory,
    )
    if review is None:
        raise HTTPException(404, "Review not found")

    candidates = manual_scrape_service.review_candidates(review)
    if candidate_index < 0 or candidate_index >= len(candidates):
        raise HTTPException(422, "Unknown candidate index")
    candidate = candidates[candidate_index]

    raw_tmdb = candidate.get("tmdb_id")
    raw_imdb = candidate.get("imdb_id")
    tmdb_id = int(raw_tmdb) if isinstance(raw_tmdb, int) or (
        isinstance(raw_tmdb, str) and raw_tmdb.isdigit()
    ) else None
    imdb_id = raw_imdb.strip() if isinstance(raw_imdb, str) and raw_imdb.strip() else None
    if tmdb_id is None and imdb_id is None:
        raise HTTPException(422, "Candidate carries no usable id")

    outcome = await manual_scrape_service.apply_candidate(
        server_id=server_id, rating_key=rating_key,
        media_type="show" if review.media_type == "show" else "movie",
        tmdb_id=tmdb_id, imdb_id=imdb_id, source="manual",
        force=False, propagate=False, session_factory=async_session_factory,
    )
    if outcome.status == "not_found":
        raise HTTPException(404, "Media not found")
    if outcome.status == "conflict":
        return templates.TemplateResponse(
            request, "admin/_review_row_result.html",
            {"outcome": outcome, "conflict": True}, status_code=409,
        )
    if outcome.status in ("provider_not_found", "skipped_locked"):
        return templates.TemplateResponse(
            request, "admin/_review_row_result.html",
            {"outcome": outcome, "conflict": False}, status_code=422,
        )
    # Applied: `apply_candidate` already resolved the review row inside its
    # own write transaction, so the entry simply disappears from the list.
    return HTMLResponse(
        "", headers={"HX-Trigger": "refresh-stats, refresh-review"},
    )


@router.post("/review/{server_id}/{rating_key}/dismiss", response_class=HTMLResponse)
async def admin_review_dismiss(server_id: str, rating_key: str):
    ok = await manual_scrape_service.dismiss_review(
        server_id, rating_key, session_factory=async_session_factory,
    )
    if not ok:
        raise HTTPException(404, "Review not found")
    return HTMLResponse("", headers={"HX-Trigger": "refresh-stats, refresh-review"})


def _batch_fragment(request: Request, job: Optional[dict], *,
                    status_code: int = 200, message: Optional[str] = None):
    return templates.TemplateResponse(
        request, "admin/_scrape_batch_status.html",
        {"job": job, "message": message},
        status_code=status_code,
    )


@router.post("/scrape-batch", response_class=HTMLResponse)
async def admin_scrape_batch_start(
    request: Request,
    type: str = Form("all"),  # noqa: A002
    dry_run: Optional[str] = Form(None),
    max_items: Optional[str] = Form(None),
):
    media_type = "all" if (type or "all") == "all" else _norm_type(type)
    parsed_max: Optional[int] = None
    if max_items and max_items.strip().isdigit():
        parsed_max = max(1, int(max_items.strip()))

    try:
        job_id = manual_scrape_batch_worker.start(
            media_type=media_type, dry_run=bool(dry_run), max_items=parsed_max,
            session_factory=async_session_factory,
        )
    except manual_scrape_batch_worker.BatchAlreadyRunningError:
        return _batch_fragment(
            request, manual_scrape_batch_worker.get_latest(), status_code=409,
            message="Un lot est déjà en cours.",
        )
    return _batch_fragment(
        request, manual_scrape_batch_worker.get(job_id), status_code=202,
    )


@router.get("/scrape-batch/status", response_class=HTMLResponse)
async def admin_scrape_batch_status(
    request: Request, job_id: Optional[str] = Query(None),
):
    job = (
        manual_scrape_batch_worker.get(job_id) if job_id
        else manual_scrape_batch_worker.get_latest()
    )
    return _batch_fragment(request, job)


@router.post("/scrape-batch/{job_id}/cancel", response_class=HTMLResponse)
async def admin_scrape_batch_cancel(job_id: str, request: Request):
    job = manual_scrape_batch_worker.get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    manual_scrape_batch_worker.cancel(job_id)
    return _batch_fragment(
        request, job, message="Annulation demandée — le lot s'arrête après l'item en cours.",
    )


@router.get("/import-nfo", response_class=HTMLResponse)
async def admin_import_nfo_form(request: Request):
    return templates.TemplateResponse(
        request,
        "admin/import_nfo.html",
        {
            "library_dir": settings.PLEX_LIBRARY_DIR,
            "report_groups": None,
            "submitted": False,
            "kinds": ["movies", "shows"],
            "overwrite": False,
            "dry_run": True,
            "error": None,
        },
    )


@router.post("/import-nfo", response_class=HTMLResponse)
async def admin_import_nfo_run(
    request: Request,
    movies: Optional[str] = Form(None),
    shows: Optional[str] = Form(None),
    overwrite: Optional[str] = Form(None),
    dry_run: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
):
    from pathlib import Path

    library_dir = settings.PLEX_LIBRARY_DIR
    selected_kinds: list[str] = []
    if movies:
        selected_kinds.append("movies")
    if shows:
        selected_kinds.append("shows")

    logger.info(
        "POST /admin/import-nfo: library_dir=%r kinds=%s overwrite=%s dry_run=%s",
        library_dir, selected_kinds, bool(overwrite), bool(dry_run),
    )

    error: Optional[str] = None
    reports = None

    if not library_dir:
        error = (
            "PLEX_LIBRARY_DIR n'est pas défini dans .env — "
            "le backend ne sait pas où plexhub-backend stocke les .nfo."
        )
    elif not selected_kinds:
        error = "Sélectionne au moins un type (films ou séries)."
    else:
        root_path = Path(library_dir)
        if not root_path.exists():
            error = f"PLEX_LIBRARY_DIR introuvable côté serveur : {library_dir}"
        elif not root_path.is_dir():
            error = f"PLEX_LIBRARY_DIR n'est pas un dossier : {library_dir}"
        else:
            reports = await nfo_import_service.import_nfo(
                db, root_path,
                kinds=tuple(selected_kinds),
                overwrite=bool(overwrite),
                dry_run=bool(dry_run),
            )

    return templates.TemplateResponse(
        request,
        "admin/import_nfo.html",
        {
            "library_dir": library_dir,
            "report_groups": reports,
            "submitted": True,
            "kinds": selected_kinds or ["movies", "shows"],
            "overwrite": bool(overwrite),
            "dry_run": bool(dry_run),
            "error": error,
        },
    )


# ──────────────────────────────────────────────────────────────────────────────
# API keys — create / list / revoke (Basic-Auth protected like the rest of /admin)
# ──────────────────────────────────────────────────────────────────────────────


async def _keys_ctx(db: AsyncSession) -> dict:
    rows = await api_key_service.list_keys(db)
    now = now_ms()
    keys = [
        {
            "id": r.id,
            "label": r.label,
            "key_prefix": r.key_prefix,
            "status": api_key_service.status_of(r, at=now),
            "created_at": r.created_at,
            "expires_at": r.expires_at,
            "last_used_at": r.last_used_at,
            "last_used_ip": r.last_used_ip,
            # Stored values as-is (an unknown value still confines the key).
            "scopes": (r.scopes or "").split(),
        }
        for r in rows
    ]
    return {
        "keys": keys,
        "now": now,
        "known_scopes": sorted(api_key_service.KNOWN_SCOPES),
    }


@router.get("/keys", response_class=HTMLResponse)
async def admin_keys(request: Request, db: AsyncSession = Depends(get_db)):
    return templates.TemplateResponse(
        request, "admin/keys.html", await _keys_ctx(db)
    )


@router.get("/keys/table", response_class=HTMLResponse)
async def admin_keys_table(request: Request, db: AsyncSession = Depends(get_db)):
    return templates.TemplateResponse(
        request, "admin/_keys_table.html", await _keys_ctx(db)
    )


@router.post("/keys", response_class=HTMLResponse)
async def admin_keys_create(
    request: Request,
    label: str = Form(...),
    expires_in_days: Optional[str] = Form(None),
    # Repeated form field (one per checked box). Validated by
    # api_key_service.create_key against the closed set (ADR 0008 § D2.1):
    # never trusted here.
    scopes: list[str] = Form(default=[]),
    db: AsyncSession = Depends(get_db),
):
    label = (label or "").strip()
    if not label:
        return templates.TemplateResponse(
            request,
            "admin/_key_created.html",
            {"error": "Le label (utilisateur) est obligatoire.", "plaintext": None},
            status_code=422,
        )
    expires_at = None
    if expires_in_days and expires_in_days.strip():
        try:
            days = int(expires_in_days)
            if days > 0:
                expires_at = now_ms() + days * 86_400_000
        except ValueError:
            pass
    try:
        row, plaintext = await api_key_service.create_key(
            db, label=label, expires_at=expires_at, scopes=scopes
        )
    except ValueError as exc:
        # 422 + the SAME fragment: swapped thanks to the htmx:beforeSwap opt-in
        # of base.html (htmx 1.x never swaps a 4xx otherwise). No key created.
        return templates.TemplateResponse(
            request,
            "admin/_key_created.html",
            {"error": str(exc), "plaintext": None},
            status_code=422,
        )
    return templates.TemplateResponse(
        request,
        "admin/_key_created.html",
        {
            "plaintext": plaintext,
            "label": row.label,
            "scopes": (row.scopes or "").split(),
            "error": None,
        },
        headers={"HX-Trigger": "refresh-keys"},
    )


@router.post("/keys/{key_id}/revoke", response_class=HTMLResponse)
async def admin_keys_revoke(
    key_id: str, request: Request, db: AsyncSession = Depends(get_db),
):
    await api_key_service.revoke_key(db, key_id)
    return templates.TemplateResponse(
        request, "admin/_keys_table.html", await _keys_ctx(db)
    )


# ──────────────────────────────────────────────────────────────────────────────
# Comptes Xtream & éditeur de catégories (T9.1)
# ──────────────────────────────────────────────────────────────────────────────


async def _accounts_ctx(db: AsyncSession) -> dict:
    result = await db.execute(
        select(XtreamAccount).order_by(XtreamAccount.created_at.desc())
    )
    accounts = result.scalars().all()
    threshold = settings.ACCOUNT_OUTAGE_STRIKES
    account_rows = [
        {
            "id": a.id,
            "label": a.label,
            "base_url": a.base_url,
            "port": a.port,
            "username": a.username,
            "status": a.status,
            "is_active": a.is_active,
            "created_at": a.created_at,
            "last_synced_at": a.last_synced_at,
            "expiration_date": a.expiration_date,
            "max_connections": a.max_connections,
            "allowed_formats": a.allowed_formats,
            "category_filter_mode": a.category_filter_mode,
            "outage_strikes": int(a.outage_strikes or 0),
            "outage_since": a.outage_since,
            "is_outage": bool(
                a.outage_strikes
                and a.outage_strikes >= threshold
                and settings.ACCOUNT_OUTAGE_MASK_ENABLED
            ),
        }
        for a in accounts
    ]
    return {"accounts": account_rows}


@router.get("/accounts", response_class=HTMLResponse)
async def admin_accounts(request: Request, db: AsyncSession = Depends(get_db)):
    return templates.TemplateResponse(
        request, "admin/accounts.html", await _accounts_ctx(db)
    )


@router.get("/accounts/table", response_class=HTMLResponse)
async def admin_accounts_table(request: Request, db: AsyncSession = Depends(get_db)):
    return templates.TemplateResponse(
        request, "admin/_accounts_table.html", await _accounts_ctx(db)
    )


@router.post("/accounts", response_class=HTMLResponse)
async def admin_accounts_create(
    request: Request,
    label: str = Form(...),
    base_url: str = Form(...),
    port: int = Form(80),
    username: str = Form(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    label = (label or "").strip()
    base_url = (base_url or "").strip()
    username = (username or "").strip()
    if not label or not base_url or not username or not password:
        return templates.TemplateResponse(
            request,
            "admin/_account_created.html",
            {"error": "Tous les champs (libellé, URL, identifiant, mot de passe) sont obligatoires."},
            status_code=422,
        )

    body = AccountCreate(
        label=label,
        base_url=base_url,
        port=port,
        username=username,
        password=password,
    )
    try:
        account_values = await account_service.prepare_account_create(db, body)
    except account_service.AccountAlreadyExistsError:
        return templates.TemplateResponse(
            request,
            "admin/_account_created.html",
            {"error": "Un compte avec cette URL et cet identifiant existe déjà."},
            status_code=409,
        )
    except (account_service.AccountAuthenticationError, SsrfBlockedError, ValueError) as exc:
        return templates.TemplateResponse(
            request,
            "admin/_account_created.html",
            {"error": f"Échec d'authentification : {exc}"},
            status_code=422,
        )

    async def _work_create(session: AsyncSession) -> XtreamAccount:
        existing = await session.execute(
            select(XtreamAccount).where(XtreamAccount.id == account_values["id"])
        )
        if existing.scalars().first():
            raise account_service.AccountAlreadyExistsError(account_values["id"])
        row = XtreamAccount(**account_values)
        session.add(row)
        await session.commit()
        return row

    try:
        account = await write_with_retry(
            _work_create,
            session_factory=session_factory_for(db),
            op="admin.create_account",
        )
    except account_service.AccountAlreadyExistsError:
        return templates.TemplateResponse(
            request,
            "admin/_account_created.html",
            {"error": "Un compte avec cette URL et cet identifiant existe déjà."},
            status_code=409,
        )

    from app.workers.sync_worker import sync_account
    create_background_task(sync_account(account.id), name=f"sync_{account.id}")

    return templates.TemplateResponse(
        request,
        "admin/_account_created.html",
        {
            "error": None,
            "label": account.label,
            "account_id": account.id,
        },
        headers={"HX-Trigger": "refresh-accounts"},
    )


@router.post("/accounts/{account_id}/test", response_class=HTMLResponse)
async def admin_accounts_test(
    account_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    try:
        user_info = await account_service.test_account_connection(db, account_id)
        exp_date_raw = user_info.get("exp_date")
        exp_date_ms = int(exp_date_raw) * 1000 if exp_date_raw else None
        return templates.TemplateResponse(
            request,
            "admin/_account_test_result.html",
            {
                "success": True,
                "status": user_info.get("status", "Unknown"),
                "expiration_date": exp_date_ms,
                "max_connections": int(user_info.get("max_connections", 1)),
                "allowed_formats": ",".join(user_info.get("allowed_output_formats", [])),
                "error": None,
            },
        )
    except account_service.AccountNotFoundError:
        return templates.TemplateResponse(
            request,
            "admin/_account_test_result.html",
            {"success": False, "error": "Compte introuvable."},
            status_code=404,
        )
    except (account_service.AccountAuthenticationError, Exception) as exc:
        return templates.TemplateResponse(
            request,
            "admin/_account_test_result.html",
            {"success": False, "error": str(exc)},
            status_code=200,
        )


@router.post("/accounts/{account_id}/toggle", response_class=HTMLResponse)
async def admin_accounts_toggle(
    account_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    async def _work_toggle(session: AsyncSession):
        updated = await account_service.toggle_account_active(session, account_id)
        await session.commit()
        return updated

    try:
        await write_with_retry(
            _work_toggle,
            session_factory=session_factory_for(db),
            op="admin.toggle_account_active",
        )
    except account_service.AccountNotFoundError:
        raise HTTPException(404, "Compte introuvable")

    return templates.TemplateResponse(
        request, "admin/_accounts_table.html", await _accounts_ctx(db)
    )


async def _categories_ctx(
    db: AsyncSession, account_id: str, message: str | None = None
) -> dict:
    result = await db.execute(
        select(XtreamAccount).where(XtreamAccount.id == account_id)
    )
    account = result.scalars().first()
    if not account:
        raise HTTPException(404, "Compte introuvable")

    categories, filter_mode = await category_service.get_categories(db, account_id)

    cat_rows = []
    allowed_count = 0
    blocked_count = 0
    adult_count = 0

    for cat in categories:
        is_adult = category_service.is_adult_category(cat.category_name, cat.category_id)
        if cat.is_allowed:
            allowed_count += 1
        else:
            blocked_count += 1
        if is_adult:
            adult_count += 1
        cat_rows.append(
            {
                "id": cat.id,
                "account_id": cat.account_id,
                "category_id": cat.category_id,
                "category_type": cat.category_type,
                "category_name": cat.category_name,
                "is_allowed": cat.is_allowed,
                "is_adult": is_adult,
            }
        )

    account_dict = {
        "id": account.id,
        "label": account.label,
        "category_filter_mode": filter_mode,
    }

    return {
        "account": account_dict,
        "categories": cat_rows,
        "stats": {
            "allowed_count": allowed_count,
            "blocked_count": blocked_count,
            "adult_count": adult_count,
        },
        "message": message,
    }


@router.get("/accounts/{account_id}/categories", response_class=HTMLResponse)
async def admin_accounts_categories(
    account_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    ctx = await _categories_ctx(db, account_id)
    return templates.TemplateResponse(request, "admin/_categories_editor.html", ctx)


@router.post("/accounts/{account_id}/categories/mode", response_class=HTMLResponse)
async def admin_accounts_categories_mode(
    account_id: str,
    request: Request,
    filter_mode: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    if filter_mode not in ("all", "whitelist", "blacklist"):
        raise HTTPException(400, "Mode de filtrage invalide")

    async def _work_mode(session: AsyncSession):
        await category_service.update_filter_mode(session, account_id, filter_mode)
        await session.commit()

    await write_with_retry(
        _work_mode,
        session_factory=session_factory_for(db),
        op="admin.update_category_filter_mode",
    )
    await category_service.update_media_category_visibility(db, account_id)

    ctx = await _categories_ctx(
        db, account_id, message=f"Mode de filtrage mis à jour : {filter_mode.upper()}."
    )
    return templates.TemplateResponse(request, "admin/_categories_editor.html", ctx)


@router.post(
    "/accounts/{account_id}/categories/{category_id}/toggle",
    response_class=HTMLResponse,
)
async def admin_accounts_category_toggle(
    account_id: str,
    category_id: str,
    request: Request,
    category_type: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    async def _work_toggle(session: AsyncSession) -> bool:
        new_status = await category_service.toggle_category_allowed(
            session, account_id, category_id, category_type
        )
        await session.commit()
        return new_status

    try:
        new_allowed = await write_with_retry(
            _work_toggle,
            session_factory=session_factory_for(db),
            op="admin.toggle_category_allowed",
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc))

    await category_service.update_media_category_visibility(db, account_id)

    stmt = select(XtreamCategory).where(
        XtreamCategory.account_id == account_id,
        XtreamCategory.category_id == category_id,
        XtreamCategory.category_type == category_type,
    )
    result = await db.execute(stmt)
    cat = result.scalar_one_or_none()
    if not cat:
        raise HTTPException(404, "Catégorie introuvable")

    is_adult = category_service.is_adult_category(cat.category_name, cat.category_id)
    cat_dict = {
        "id": cat.id,
        "account_id": cat.account_id,
        "category_id": cat.category_id,
        "category_type": cat.category_type,
        "category_name": cat.category_name,
        "is_allowed": new_allowed,
        "is_adult": is_adult,
    }
    return templates.TemplateResponse(
        request, "admin/_category_row.html", {"cat": cat_dict}
    )


@router.post("/accounts/{account_id}/categories/refresh", response_class=HTMLResponse)
async def admin_accounts_categories_refresh(
    account_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    try:
        vod_count, series_count = await category_service.refresh_categories_from_provider(
            db, account_id
        )
        await db.commit()
    except category_service.AccountNotFoundError:
        raise HTTPException(404, "Compte introuvable")
    except Exception as exc:
        ctx = await _categories_ctx(
            db, account_id, message=f"Erreur lors du rafraîchissement : {exc}"
        )
        return templates.TemplateResponse(request, "admin/_categories_editor.html", ctx)

    ctx = await _categories_ctx(
        db,
        account_id,
        message=f"Catégories synchronisées : {vod_count} VOD, {series_count} séries.",
    )
    return templates.TemplateResponse(request, "admin/_categories_editor.html", ctx)


@router.post("/accounts/{account_id}/categories/block-adult", response_class=HTMLResponse)
async def admin_accounts_categories_block_adult(
    account_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    async def _work_block(session: AsyncSession) -> int:
        count = await category_service.block_adult_categories_for_account(
            session, account_id
        )
        await session.commit()
        return count

    count = await write_with_retry(
        _work_block,
        session_factory=session_factory_for(db),
        op="admin.block_adult_categories",
    )
    await category_service.update_media_category_visibility(db, account_id)
    await category_service.update_media_adult_flags(db, account_id)

    ctx = await _categories_ctx(
        db,
        account_id,
        message=f"{count} catégorie(s) adulte(s) bloquée(s) et catalogue mis à jour.",
    )
    return templates.TemplateResponse(request, "admin/_categories_editor.html", ctx)



# ──────────────────────────────────────────────────────────────────────────────
# Pilotage & suivi temps réel des jobs et sauvegardes (T9.2)
# ──────────────────────────────────────────────────────────────────────────────

_SECRET_URL_CREDS_RE = re.compile(r"://[^:/\s@]+:[^@/\s]+@")
_SECRET_KV_RE = re.compile(
    r"(?i)(password|passwd|token|api_key|apikey|secret|authorization|x-api-key)\s*[:=]\s*\S+"
)


def _sanitize_job_message(raw: object, max_len: int = 160) -> str:
    """Strip URL credentials and key=value secrets, then bound length."""
    text = str(raw or "").strip()
    if not text:
        return ""
    text = _SECRET_URL_CREDS_RE.sub("://***:***@", text)
    text = _SECRET_KV_RE.sub(lambda m: f"{m.group(1)}=***", text)
    text = " ".join(text.split())
    if len(text) > max_len:
        return text[: max_len - 1] + "…"
    return text


async def _exec_sync_job() -> None:
    from app.workers import sync_worker

    await sync_worker.run_all_accounts()


async def _exec_epg_sync_job() -> None:
    from app.services import epg_sync_service

    await epg_sync_service.run_epg_sync_job()


async def _exec_enrichment_job() -> None:
    from app.workers import enrichment_worker

    await enrichment_worker.run()


async def _exec_health_check_job() -> None:
    from app.workers import health_check_worker

    await health_check_worker.run()


async def _exec_plex_generate_job() -> None:
    from app.services import plex_generation_service

    await plex_generation_service.generate_plex_library_auto()


async def _exec_backup_job() -> None:
    from app.scripts import backup_db

    await asyncio.to_thread(backup_db.run_backup)


_ADMIN_JOB_SPECS: dict[str, dict[str, Any]] = {
    "sync": {
        "label": "Synchronisation Xtream",
        "phase": "sync",
        "phases": ("sync",),
        "prefixes": ("sync_", "admin_sync_"),
        "health_key": "pipeline",
        "runner": _exec_sync_job,
    },
    "epg_sync": {
        "label": "Synchro EPG (XMLTV)",
        "phase": "epg_sync",
        "phases": ("epg_sync",),
        "prefixes": ("epg_sync_", "admin_epg_sync_"),
        "health_key": "epg_sync",
        "runner": _exec_epg_sync_job,
    },
    "enrichment": {
        "label": "Enrichissement TMDB",
        "phase": "enrichment",
        "phases": ("enrichment",),
        "prefixes": ("enrichment_", "admin_enrichment_"),
        "health_key": None,
        "runner": _exec_enrichment_job,
    },
    "health_check": {
        "label": "Validation des flux",
        "phase": "validation",
        "phases": ("validation", "health_check"),
        "prefixes": ("validation_", "health_check_", "admin_health_check_"),
        "health_key": "health_check",
        "runner": _exec_health_check_job,
    },
    "plex_generate": {
        "label": "Génération bibliothèque Plex",
        "phase": "generation",
        "phases": ("generation", "plex_generate"),
        "prefixes": ("plex_generate_", "admin_plex_generate_"),
        "health_key": None,
        "runner": _exec_plex_generate_job,
    },
    "backup": {
        "label": "Sauvegarde BDD",
        "phase": "backup",
        "phases": ("backup", "db_backup"),
        "prefixes": ("backup_", "admin_backup_"),
        "health_key": "db_backup",
        "runner": _exec_backup_job,
    },
}


def _match_job_entry(job_name: str, entry: dict[str, Any]) -> bool:
    spec = _ADMIN_JOB_SPECS[job_name]
    job_id = str(entry.get("job_id") or "")
    phase = str(entry.get("phase") or "")
    return job_id.startswith(spec["prefixes"]) or phase in spec["phases"]


def _is_job_running(job_name: str, all_jobs: list[dict[str, Any]] | None = None) -> bool:
    task_name = f"admin_job_{job_name}"
    if any(
        not task.done() and task.get_name() == task_name
        for task in app_tasks._background_tasks
    ):
        return True
    entries = all_jobs if all_jobs is not None else job_registry.get_all_jobs()
    return any(
        _match_job_entry(job_name, entry) and entry.get("status") == "processing"
        for entry in entries
    )


def _build_job_views(backup_status: dict[str, Any]) -> list[dict[str, Any]]:
    all_jobs = job_registry.get_all_jobs()
    views: list[dict[str, Any]] = []
    for name, spec in _ADMIN_JOB_SPECS.items():
        matched = [e for e in all_jobs if _match_job_entry(name, e)]
        running = _is_job_running(name, all_jobs)

        health_success = (
            job_health.last_success_ms(spec["health_key"])
            if spec["health_key"]
            else None
        )
        registry_success = next(
            (
                e.get("finished_at") or e.get("started_at")
                for e in matched
                if e.get("status") == "completed"
            ),
            None,
        )
        success_candidates = [
            ts for ts in (health_success, registry_success) if isinstance(ts, int)
        ]
        if name == "backup" and isinstance(backup_status.get("lastSuccessAt"), int):
            success_candidates.append(backup_status["lastSuccessAt"])
        last_success_at = max(success_candidates) if success_candidates else None

        last_failed = next(
            (e for e in matched if e.get("status") == "failed"), None
        )
        last_failure_at = (
            (last_failed.get("finished_at") or last_failed.get("started_at"))
            if last_failed
            else None
        )
        latest = matched[0] if matched else None

        if running:
            status = "running"
            message = "En cours d'exécution…"
        elif latest and latest.get("status") == "failed":
            status = "error"
            message = _sanitize_job_message(latest.get("error")) or "Échec du dernier run"
        elif (
            last_failure_at is not None
            and (last_success_at is None or last_failure_at > last_success_at)
        ):
            status = "error"
            message = (
                _sanitize_job_message(last_failed.get("error") if last_failed else None)
                or "Échec du dernier run"
            )
        else:
            status = "idle"
            message = (
                "Dernier run réussi"
                if last_success_at is not None
                else "Aucun run récent"
            )

        views.append(
            {
                "name": name,
                "label": spec["label"],
                "status": status,
                "last_success_at": last_success_at,
                "last_failure_at": last_failure_at,
                "message": message,
            }
        )
    return views


def _jobs_ctx(
    *,
    flash_message: str | None = None,
    flash_error: str | None = None,
) -> dict[str, Any]:
    current_ms = now_ms()
    backup_status = backup_status_service.compute_status(current_ms)
    return {
        "jobs": _build_job_views(backup_status),
        "backup_status": backup_status,
        "is_master": job_health.is_master(),
        "server_now_ms": current_ms,
        "flash_message": flash_message,
        "flash_error": flash_error,
    }


@router.get("/jobs", response_class=HTMLResponse)
async def admin_jobs_index(request: Request):
    return templates.TemplateResponse(request, "admin/jobs.html", _jobs_ctx())


@router.get("/jobs/panel", response_class=HTMLResponse)
async def admin_jobs_panel(request: Request):
    return templates.TemplateResponse(request, "admin/_jobs_panel.html", _jobs_ctx())


@router.post("/jobs/{job_name}/run", response_class=HTMLResponse)
async def admin_jobs_run(job_name: str, request: Request):
    spec = _ADMIN_JOB_SPECS.get(job_name)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"Unknown job '{job_name}'")

    if _is_job_running(job_name):
        return templates.TemplateResponse(
            request,
            "admin/_jobs_panel.html",
            _jobs_ctx(
                flash_error=f"Le job « {spec['label']} » est déjà en cours."
            ),
            status_code=409,
        )

    job_id = f"admin_{job_name}_{uuid.uuid4().hex[:8]}"
    job_registry.create_job(job_id, phase=spec["phase"])
    runner: Callable[[], Awaitable[None]] = spec["runner"]
    health_key: str | None = spec["health_key"]

    async def _tracked() -> None:
        try:
            await runner()
            job_registry.update_job(job_id, status="completed", progress={})
            if health_key:
                job_health.mark_job_success(health_key)
        except Exception as exc:
            logger.error("Admin job %s (%s) failed: %s", job_name, job_id, exc)
            job_registry.update_job(
                job_id,
                status="failed",
                error=_sanitize_job_message(str(exc)) or "Job failed",
            )

    create_background_task(_tracked(), name=f"admin_job_{job_name}")
    return templates.TemplateResponse(
        request,
        "admin/_jobs_panel.html",
        _jobs_ctx(flash_message=f"Job « {spec['label']} » démarré."),
    )


# ──────────────────────────────────────────────────────────────────────────────
# Vue audit lecture seule : metadata_edits + stats scrape_cache (T9.3)
# ──────────────────────────────────────────────────────────────────────────────


def _extract_edit_fields(before_raw: str | None, after_raw: str | None) -> str:
    """Return a comma-separated summary of touched field names without secrets."""
    try:
        after_doc = json.loads(after_raw or "{}")
        before_doc = json.loads(before_raw or "{}")
    except (ValueError, TypeError):
        return "—"
    if not isinstance(after_doc, dict):
        return "—"

    changed: list[str] = []
    after_variants = after_doc.get("variants")
    before_variants = before_doc.get("variants") if isinstance(before_doc, dict) else None
    if isinstance(after_variants, list) and after_variants:
        for idx, av in enumerate(after_variants):
            if not isinstance(av, dict):
                continue
            af = av.get("fields") if isinstance(av.get("fields"), dict) else {}
            bf = (
                before_variants[idx].get("fields")
                if isinstance(before_variants, list)
                and idx < len(before_variants)
                and isinstance(before_variants[idx], dict)
                and isinstance(before_variants[idx].get("fields"), dict)
                else {}
            )
            for k, val in af.items():
                if (not bf or bf.get(k) != val) and k not in changed:
                    changed.append(str(k))
            if av.get("lock") != (
                before_variants[idx].get("lock")
                if isinstance(before_variants, list)
                and idx < len(before_variants)
                and isinstance(before_variants[idx], dict)
                else None
            ) and "lock" not in changed:
                changed.append("lock")
    else:
        for k in after_doc:
            if k not in ("variants", "derived") and k not in changed:
                changed.append(str(k))
    return ", ".join(changed) if changed else "—"


async def _audit_ctx(db: AsyncSession) -> dict[str, Any]:
    """Read-only audit context: last 50 metadata_edits + scrape_cache breakdown."""
    edits_stmt = (
        select(MetadataEdit)
        .order_by(MetadataEdit.at_ms.desc(), MetadataEdit.edit_id.desc())
        .limit(50)
    )
    edit_rows = (await db.execute(edits_stmt)).scalars().all()
    edits = [
        {
            "edit_id": e.edit_id,
            "at_ms": e.at_ms,
            "kind": e.kind,
            "item_type": e.item_type,
            "server_id": e.server_id,
            "rating_key": e.rating_key,
            "key_id": e.key_id,
            "reason": e.reason,
            "reverts": e.reverts,
            "reverted_by": e.reverted_by,
            "fields_summary": _extract_edit_fields(e.before_json, e.after_json),
        }
        for e in edit_rows
    ]

    current = now_ms()
    cache_stmt = (
        select(
            ScrapeCacheEntry.family,
            ScrapeCacheEntry.key_version,
            func.count().label("total"),
            func.coalesce(
                func.sum(case((ScrapeCacheEntry.expires_at > current, 1), else_=0)), 0
            ).label("active"),
            func.coalesce(
                func.sum(case((ScrapeCacheEntry.expires_at <= current, 1), else_=0)), 0
            ).label("expired"),
        )
        .group_by(ScrapeCacheEntry.family, ScrapeCacheEntry.key_version)
        .order_by(ScrapeCacheEntry.family, ScrapeCacheEntry.key_version)
    )
    cache_rows = (await db.execute(cache_stmt)).all()
    cache_families = [
        {
            "family": r.family,
            "key_version": r.key_version,
            "total": int(r.total),
            "active": int(r.active),
            "expired": int(r.expired),
        }
        for r in cache_rows
    ]
    return {
        "edits": edits,
        "cache_families": cache_families,
        "cache_total": sum(r["total"] for r in cache_families),
        "cache_active": sum(r["active"] for r in cache_families),
        "cache_expired": sum(r["expired"] for r in cache_families),
    }


@router.get("/audit", response_class=HTMLResponse)
async def admin_audit_index(request: Request, db: AsyncSession = Depends(get_db)):
    """Strictly read-only audit page for metadata edits and scrape_cache stats."""
    return templates.TemplateResponse(request, "admin/audit.html", await _audit_ctx(db))


__all__ = ["router"]
