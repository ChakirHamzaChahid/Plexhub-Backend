"""Contrat machine de la passerelle de scraping (lot B1, ticket B1-12).

Extrait de `app.openapi()` les 9 chemins `/api/scrape/v1/*` et TOUS les
composants qu'ils référencent (transitivement), et l'écrit de façon
déterministe dans `docs/contracts/scrape-v1.openapi.json` : c'est le fichier
que plexhubTMM lit (`/schema-sync`) pour vérifier ses types. Le test échoue si
le fichier commité diffère de ce que l'application génère. Pour le régénérer
après une évolution voulue du contrat :

    REGEN_SCRAPE_OPENAPI=1 pytest tests/test_scrape_v1_openapi_export.py

Le fichier ne contient ni `servers` (aucun hôte), ni clé, ni valeur de
configuration : uniquement des chemins, des paramètres et des schémas. La
version de l'application n'y figure pas (le contrat ne bouge pas à chaque
release) ; ses versions propres sont `x-key-version` / `x-schema-version`.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from app.services import scrape_keys

EXPORT_PATH = Path(__file__).resolve().parent.parent / "docs" / "contracts" / "scrape-v1.openapi.json"
PREFIX = "/api/scrape/v1/"
EXPECTED_PATHS = {
    "/api/scrape/v1/match",
    "/api/scrape/v1/search",
    "/api/scrape/v1/movie/{tmdbId}",
    "/api/scrape/v1/tv/{tmdbId}",
    "/api/scrape/v1/tv/{tmdbId}/season/{seasonNumber}",
    "/api/scrape/v1/find",
    "/api/scrape/v1/cache/stats",
    "/api/scrape/v1/cache/invalidate",
    "/api/scrape/v1/cache/purge",
}
_REF_PREFIX = "#/components/schemas/"


def _refs(node, found: set[str]) -> None:
    """Noms des composants référencés par `node` (parcours récursif)."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            assert ref.startswith(_REF_PREFIX), f"référence hors components/schemas : {ref}"
            found.add(ref[len(_REF_PREFIX):])
        for value in node.values():
            _refs(value, found)
    elif isinstance(node, list):
        for value in node:
            _refs(value, found)


def build_contract() -> dict:
    """Le sous-ensemble de l'OpenAPI de l'application qui constitue le contrat."""
    from app.main import app

    spec = app.openapi()
    paths = {path: spec["paths"][path] for path in sorted(spec["paths"]) if path.startswith(PREFIX)}
    all_schemas = spec.get("components", {}).get("schemas", {})

    needed: set[str] = set()
    _refs(paths, needed)
    pending = list(needed)
    while pending:  # fermeture transitive des références
        name = pending.pop()
        assert name in all_schemas, f"composant référencé mais absent : {name}"
        inner: set[str] = set()
        _refs(all_schemas[name], inner)
        for ref in inner - needed:
            needed.add(ref)
            pending.append(ref)

    return {
        "openapi": spec["openapi"],
        "info": {
            "title": "PlexHub Backend - scrape gateway /api/scrape/v1",
            "version": "v1",
        },
        "x-key-version": scrape_keys.KEY_VERSION,
        "x-schema-version": scrape_keys.SCHEMA_VERSION,
        "paths": paths,
        "components": {"schemas": {name: all_schemas[name] for name in sorted(needed)}},
    }


def render_contract() -> str:
    """Rendu canonique : clés triées, indentation 2, UTF-8 non échappé, LF final."""
    return json.dumps(build_contract(), sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def test_export_matches_committed_file():
    generated = render_contract()
    if os.environ.get("REGEN_SCRAPE_OPENAPI") == "1":
        EXPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        EXPORT_PATH.write_bytes(generated.encode("utf-8"))
    assert EXPORT_PATH.exists(), "relancer avec REGEN_SCRAPE_OPENAPI=1"
    committed = EXPORT_PATH.read_bytes().decode("utf-8").replace("\r\n", "\n")
    assert committed == generated, (
        "docs/contracts/scrape-v1.openapi.json diffère de app.openapi() ; si c'est voulu : "
        "REGEN_SCRAPE_OPENAPI=1 pytest tests/test_scrape_v1_openapi_export.py "
        "(et prévenir plexhubTMM : le contrat a changé)"
    )


def test_export_contains_the_nine_routes_and_their_schemas():
    contract = build_contract()
    assert set(contract["paths"]) == EXPECTED_PATHS
    operations = sum(len(item) for item in contract["paths"].values())
    assert operations == 9  # une opération par chemin, aucune autre méthode
    schemas = contract["components"]["schemas"]
    for name in (
        "MatchResponse", "ScrapeSearchResponse", "MovieResponse", "ShowResponse",
        "SeasonResponse", "FindResponse", "CacheStatsResponse", "InvalidateResponse",
        "PurgeResponse", "InvalidateBody", "PurgeRequest", "ErrorResponse",
        "MovieDetails", "ShowDetails", "SeasonDetails", "EpisodeDetails",
    ):
        assert name in schemas, f"schéma absent du contrat : {name}"


def test_export_is_self_contained():
    """Chaque $ref du fichier pointe un composant du fichier (aucun orphelin)."""
    contract = build_contract()
    found: set[str] = set()
    _refs(contract["paths"], found)
    _refs(contract["components"], found)
    assert found <= set(contract["components"]["schemas"])
    # Tout composant exporté est atteignable depuis un chemin (pas de bruit).
    reachable: set[str] = set()
    _refs(contract["paths"], reachable)
    pending = list(reachable)
    while pending:
        inner: set[str] = set()
        _refs(contract["components"]["schemas"][pending.pop()], inner)
        for ref in inner - reachable:
            reachable.add(ref)
            pending.append(ref)
    assert reachable == set(contract["components"]["schemas"])


def test_export_has_no_host_and_no_secret():
    contract = build_contract()
    assert "servers" not in contract
    text = render_contract()
    assert not re.search(r"https?://", text), "aucun hôte ni URL dans le contrat"
    for forbidden in ("phk_", "api_key=", "apikey=", "password", "localhost", "192.168."):
        assert forbidden not in text.lower(), f"motif interdit dans le contrat : {forbidden}"
