"""Admin web UI — HTMX + Jinja2 catalogue editor.

Mounted at `/admin`, separate from the JSON API (`/api/...`). Routes here render
HTML fragments rather than JSON; they call the same `media_service` functions
the API uses, so business logic isn't duplicated.
"""
import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession


logger = logging.getLogger("plexhub.api.admin")

from app.config import settings
from app.db.database import async_session_factory, get_db
from app.models.schemas import MediaUpdate
from app.services.media_service import media_service
from app.services import (
    account_outage_service,
    api_key_service,
    manual_scrape_service,
    nfo_import_service,
    poster_match_service,
)
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


templates.env.filters["ms"] = _fmt_ms


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
        "review_keys": page_obj.review_keys,
        "page": page,
        "page_size": page_size,
        "media_type": media_type,
        "ids": ids,
        "search": search or "",
        "sort": sort,
    }


async def _stats_ctx(db: AsyncSession) -> dict:
    stats = await manual_scrape_service.catalogue_stats(db)
    movie = stats["movie"]
    return {
        "stats": stats,
        "movie_stats": movie,
        "show_stats": stats["show"],
        # Back-compat keys for the pre-ADR-0005 movie-only counters (the
        # `/admin/movies/stats` alias and its tests still read these).
        "total_movies": movie.total,
        "missing_imdb_count": movie.missing_imdb,
        "missing_tmdb_count": movie.missing_tmdb,
        "outages": await account_outage_service.list_outages(db),
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
    tab: str = Query("catalogue"),
    db: AsyncSession = Depends(get_db),
):
    ctx = await _catalogue_ctx(
        db, media_type=_norm_type(type), ids=_norm_ids(ids),
        search=search, sort=sort, page=page, page_size=page_size,
    )
    ctx.update(await _stats_ctx(db))
    ctx["tab"] = "review" if tab == "review" else "catalogue"
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


@router.get("/movies", response_class=HTMLResponse)
async def admin_movies_fragment(
    request: Request,
    missing_imdb: bool = Query(False),
    missing_tmdb: bool = Query(False),
    search: Optional[str] = Query(None),
    sort: str = Query("added_desc"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=10, le=500),
    db: AsyncSession = Depends(get_db),
):
    """Pre-ADR-0005 alias (ADR 0005 D10): the old boolean pair maps onto the
    new `ids` filter — both set means "either id missing" (`incomplete`),
    neither means `all`."""
    if missing_imdb and missing_tmdb:
        ids = "incomplete"
    elif missing_imdb:
        ids = "missing_imdb"
    elif missing_tmdb:
        ids = "missing_tmdb"
    else:
        ids = "all"
    ctx = await _catalogue_ctx(
        db, media_type="movie", ids=ids, search=search, sort=sort,
        page=page, page_size=page_size,
    )
    return templates.TemplateResponse(request, "admin/_media_table.html", ctx)


@router.get("/stats", response_class=HTMLResponse)
@router.get("/movies/stats", response_class=HTMLResponse)
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

    outcome = await manual_scrape_service.apply_candidate(
        server_id=server_id, rating_key=rating_key, media_type=media_type,
        tmdb_id=parsed_tmdb_id, imdb_id=parsed_imdb_id,
        source="manual", force=bool(force), propagate=bool(propagate),
        session_factory=async_session_factory,
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
async def admin_media_scrape_panel(server_id: str, rating_key: str, request: Request):
    """The scrape drawer: both posters side by side and a search form
    pre-filled from the row (ADR 0005 D10). No network here — candidates are
    fetched by the `/candidates` route the form targets."""
    item = await manual_scrape_service.load_row(
        server_id, rating_key, session_factory=async_session_factory,
    )
    if item is None:
        raise HTTPException(404, "Media not found")
    return templates.TemplateResponse(
        request, "admin/_scrape_panel.html",
        {"item": item, "media_type": _norm_type(item.type)},
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
        headers={"HX-Trigger": "refresh-stats"} if reports and not dry_run else None,
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
        }
        for r in rows
    ]
    return {"keys": keys, "now": now}


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
    row, plaintext = await api_key_service.create_key(db, label=label, expires_at=expires_at)
    return templates.TemplateResponse(
        request,
        "admin/_key_created.html",
        {"plaintext": plaintext, "label": row.label, "error": None},
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


__all__ = ["router"]
