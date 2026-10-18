"""B1-11 — `/cache/stats`, `/cache/invalidate`, `/cache/purge` (US6, FR-029..FR-031,
FR-015b, FR-028b)."""
from __future__ import annotations

import pytest

from app.services import scrape_gateway_cache as cache
from app.services import scrape_keys
from app.services.scrape_gateway_cache import counters
from app.utils.time import now_ms
from tests._scrape_v1_support import BASE, MASTER_HEADERS

DAY = 86_400_000


@pytest.fixture(autouse=True)
def _auto(gw_flights_cleanup):
    yield


async def _seed(factory, *, now: int):
    movie = {"tmdbId": 603}  # payload content is opaque to the cache
    await cache.put_many([
        (scrape_keys.movie_key(603, "fr-FR"), "found", movie, now),
        (scrape_keys.movie_key(603, "en-US"), "found", movie, now),
        (scrape_keys.movie_key(604, "fr-FR"), "not_found", None, now - 10 * DAY),  # expired
        (scrape_keys.season_key(1396, 0, "fr-FR"), "found", {"s": 0}, now),
        (scrape_keys.season_key(1396, 1, "en-US"), "found", {"s": 1}, now),
        (scrape_keys.season_key(13960, 1, "fr-FR"), "found", {"s": 1}, now),
        (scrape_keys.match_key("movie", "matrix", 1999, "fr-FR"), "ambiguous",
         {"confidence": 0.8, "candidates": []}, now),
    ], session_factory=factory)


async def test_stats_shape(gw_client, gw_db, gw_http):
    now = now_ms()
    await _seed(gw_db, now=now)
    counters.hit()
    response = await gw_client.get(f"{BASE}/cache/stats", headers=MASTER_HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"generatedAt", "families", "ttl", "sinceStart"}
    assert body["ttl"] == {"successDays": 30, "negativeDays": 3}
    assert body["sinceStart"] == {"hits": 1, "misses": 0, "coalesced": 0, "upstreamErrors": 0}
    families = {(f["family"], f["keyVersion"]): f for f in body["families"]}
    assert [(f["family"], f["keyVersion"]) for f in body["families"]] == sorted(families)
    movie = families[("movie", 1)]
    assert (movie["entries"], movie["positive"], movie["negative"], movie["expired"]) == (3, 2, 1, 1)
    assert families[("match", 1)]["ambiguous"] == 1
    assert movie["oldestFetchedAt"] == now - 10 * DAY and movie["newestFetchedAt"] == now


async def test_invalidate_by_keys_is_idempotent(gw_client, gw_db):
    await _seed(gw_db, now=now_ms())
    keys = {"keys": ["1|fr-FR|movie|603", "1|fr-FR|movie|605"]}
    first = await gw_client.post(f"{BASE}/cache/invalidate", json=keys, headers=MASTER_HEADERS)
    assert first.status_code == 200 and first.json() == {"deleted": 1}
    again = await gw_client.post(f"{BASE}/cache/invalidate", json=keys, headers=MASTER_HEADERS)
    assert again.json() == {"deleted": 0}
    assert await cache.get(scrape_keys.movie_key(603, "en-US"), session_factory=gw_db) is not None


async def test_invalidate_with_one_malformed_key_deletes_nothing(gw_client, gw_db):
    await _seed(gw_db, now=now_ms())
    response = await gw_client.post(f"{BASE}/cache/invalidate", headers=MASTER_HEADERS,
                                    json={"keys": ["1|fr-FR|movie|603", "1|fr|movie|603"]})
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_parameter"
    assert await cache.get(scrape_keys.movie_key(603, "fr-FR"), session_factory=gw_db) is not None


async def test_invalidate_family_season_removes_every_language(gw_client, gw_db):
    await _seed(gw_db, now=now_ms())
    response = await gw_client.post(f"{BASE}/cache/invalidate", headers=MASTER_HEADERS,
                                    json={"family": "season", "tmdbId": 1396})
    assert response.json() == {"deleted": 2}
    assert await cache.get(scrape_keys.season_key(13960, 1, "fr-FR"), session_factory=gw_db)
    movie = await gw_client.post(f"{BASE}/cache/invalidate", headers=MASTER_HEADERS,
                                 json={"family": "movie", "tmdbId": 603})
    assert movie.json() == {"deleted": 2}
    absent = await gw_client.post(f"{BASE}/cache/invalidate", headers=MASTER_HEADERS,
                                  json={"family": "show", "tmdbId": 77})
    assert absent.json() == {"deleted": 0}


@pytest.mark.parametrize("body", [
    {},
    {"keys": ["1|fr-FR|movie|603"], "family": "movie", "tmdbId": 603},
    {"keys": ["1|fr-FR|movie|603"], "tmdbId": 603},
    {"family": "omdb", "tmdbId": 603},
    {"family": "match", "tmdbId": 603},
    {"family": "movie"},
    {"tmdbId": 603},
])
async def test_invalidate_semantic_errors_are_400(gw_client, body):
    response = await gw_client.post(f"{BASE}/cache/invalidate", json=body, headers=MASTER_HEADERS)
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "invalid_parameter"


@pytest.mark.parametrize("body", [
    {"keys": []},
    {"keys": ["1|fr-FR|movie|603"] * 501},
    {"family": "movie", "tmdbId": 0},
    {"family": "movie", "tmdbId": 1_000_000_000},
    {"keys": "1|fr-FR|movie|603"},
    {"unexpected": 1},
])
async def test_invalidate_bounds_are_native_422(gw_client, body):
    response = await gw_client.post(f"{BASE}/cache/invalidate", json=body, headers=MASTER_HEADERS)
    assert response.status_code == 422, response.text
    assert isinstance(response.json()["detail"], list)


async def test_purge_default_limit_and_bounds(gw_client, gw_db):
    now = now_ms()
    await _seed(gw_db, now=now)
    response = await gw_client.post(f"{BASE}/cache/purge", headers=MASTER_HEADERS)
    assert response.status_code == 200 and response.json() == {"deleted": 1, "remaining": 0}
    for limit in (0, 100_001, -1):
        bad = await gw_client.post(f"{BASE}/cache/purge", json={"limit": limit}, headers=MASTER_HEADERS)
        assert bad.status_code == 422


async def test_purge_is_bounded_by_limit(gw_client, gw_db):
    old = now_ms() - 40 * DAY
    await cache.put_many(
        [(scrape_keys.movie_key(i, "fr-FR"), "not_found", None, old) for i in range(1, 6)],
        session_factory=gw_db,
    )
    response = await gw_client.post(f"{BASE}/cache/purge", json={"limit": 2}, headers=MASTER_HEADERS)
    assert response.json() == {"deleted": 2, "remaining": 3}
