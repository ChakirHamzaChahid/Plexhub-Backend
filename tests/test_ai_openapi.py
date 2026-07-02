"""OpenAPI schema validation: 5 AI routes + camelCase aliases in schemas."""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient


pytestmark = pytest.mark.asyncio


def _build_app():
    from app.main import app
    return app


async def _get_openapi() -> dict:
    # /openapi.json is intentionally served only behind Basic Auth (the public
    # tunnel must not advertise the schema), so validate the generated schema
    # directly rather than over HTTP.
    return _build_app().openapi()


async def test_openapi_contains_5_ai_routes():
    spec = await _get_openapi()
    paths = set(spec["paths"].keys())
    expected = {
        "/api/ai/rank",
        "/api/ai/rank-multi",
        "/api/ai/embed/status",
        "/api/ai/embed/rebuild",
        "/api/ai/embed/jobs/{job_id}",
    }
    assert expected.issubset(paths), f"missing: {expected - paths}"


async def test_openapi_camelcase_aliases():
    spec = await _get_openapi()
    schemas = spec["components"]["schemas"]

    # RankResponse must expose camelCase keys
    rank_response = schemas.get("RankResponse")
    assert rank_response is not None
    props = set(rank_response["properties"].keys())
    assert {"cacheHits", "cacheMisses", "cacheMissesDropped", "resolutionFailed", "ranked"}.issubset(props)

    # EmbedStatus must expose rssMb (int) and modelName
    embed_status = schemas.get("EmbedStatus")
    assert embed_status is not None
    es_props = embed_status["properties"]
    assert "rssMb" in es_props
    assert es_props["rssMb"]["type"] == "integer"  # C6: int not float
    assert "modelName" in es_props
    assert "vecLoaded" in es_props


async def test_openapi_ai_routes_have_auth_dependency():
    """All /api/ai/* endpoints must require X-API-Key (via dependencies)."""
    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/ai/rank-multi", json={
            "refs": [{"tmdbId": 1}],
            "candidates": [{"tmdbId": 2}],
            "limit": 5,
            "mediaType": "movie",
        })
    # Without X-API-Key header, must be 401 or 503 (cf. verify_api_key motifs), never 200.
    assert resp.status_code in (401, 503)
