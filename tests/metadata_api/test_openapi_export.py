"""Contrat machine de l'API d'édition des métadonnées (lot B2).

Extrait de `app.openapi()` les chemins `/api/metadata/v1/*` et TOUS les composants qu'ils
référencent (transitivement), et l'écrit de façon déterministe dans
`docs/contracts/metadata-v1.openapi.json` : c'est le fichier que plexhubTMM lit
(`/schema-sync`). Le test échoue si le fichier commité diffère de ce que l'application
génère. Pour le régénérer après une évolution voulue du contrat :

    REGEN_METADATA_OPENAPI=1 pytest tests/metadata_api/test_openapi_export.py

Aucun hôte, aucune clé, aucune valeur de configuration : seulement chemins, paramètres et
schémas. Ses versions propres sont `x-schema-version` (formes de réponse) et
`x-revision-form` (forme de l'empreinte de révision).
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from app.models.metadata_schemas import SCHEMA_VERSION
from app.services.metadata_plan import REVISION_FORM

EXPORT_PATH = Path(__file__).resolve().parents[2] / "docs" / "contracts" / "metadata-v1.openapi.json"
PREFIX = "/api/metadata/v1/"
EXPECTED_OPERATIONS = {
    ("get", "/api/metadata/v1/lookup"),
    ("get", "/api/metadata/v1/items"),
    ("get", "/api/metadata/v1/items/{serverId}/{ratingKey}"),
    ("get", "/api/metadata/v1/items/{serverId}/{ratingKey}/episodes"),
    ("post", "/api/metadata/v1/items/{serverId}/{ratingKey}/preview"),
    ("patch", "/api/metadata/v1/items/{serverId}/{ratingKey}"),
    ("get", "/api/metadata/v1/items/{serverId}/{ratingKey}/history"),
    ("get", "/api/metadata/v1/edits/{editId}"),
    ("post", "/api/metadata/v1/edits/{editId}/revert/preview"),
    ("post", "/api/metadata/v1/edits/{editId}/revert"),
    ("post", "/api/metadata/v1/items/{serverId}/{ratingKey}/lock/preview"),
    ("post", "/api/metadata/v1/items/{serverId}/{ratingKey}/lock"),
    ("get", "/api/metadata/v1/backup/status"),
    ("get", "/api/metadata/v1/fields"),
}
_REF_PREFIX = "#/components/schemas/"


def _refs(node, found: set[str]) -> None:
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
    from app.main import app

    spec = app.openapi()
    paths = {p: spec["paths"][p] for p in sorted(spec["paths"]) if p.startswith(PREFIX)}
    all_schemas = spec.get("components", {}).get("schemas", {})

    needed: set[str] = set()
    _refs(paths, needed)
    pending = list(needed)
    while pending:
        name = pending.pop()
        assert name in all_schemas, f"composant référencé mais absent : {name}"
        inner: set[str] = set()
        _refs(all_schemas[name], inner)
        for ref in inner - needed:
            needed.add(ref)
            pending.append(ref)

    return {
        "openapi": spec["openapi"],
        "info": {"title": "PlexHub Backend - metadata edit API /api/metadata/v1", "version": "v1"},
        "x-schema-version": SCHEMA_VERSION,
        "x-revision-form": REVISION_FORM,
        "paths": paths,
        "components": {"schemas": {name: all_schemas[name] for name in sorted(needed)}},
    }


def render_contract() -> str:
    return json.dumps(build_contract(), sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def test_export_matches_committed_file():
    generated = render_contract()
    if os.environ.get("REGEN_METADATA_OPENAPI") == "1":
        EXPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        EXPORT_PATH.write_bytes(generated.encode("utf-8"))
    assert EXPORT_PATH.exists(), "relancer avec REGEN_METADATA_OPENAPI=1"
    committed = EXPORT_PATH.read_bytes().decode("utf-8").replace("\r\n", "\n")
    assert committed == generated, (
        "docs/contracts/metadata-v1.openapi.json diffère de app.openapi() ; si c'est voulu : "
        "REGEN_METADATA_OPENAPI=1 pytest tests/metadata_api/test_openapi_export.py "
        "(et prévenir plexhubTMM : le contrat a changé)"
    )


def test_export_contains_the_fourteen_routes_and_their_schemas():
    contract = build_contract()
    operations = {(method, path) for path, item in contract["paths"].items() for method in item}
    assert operations == EXPECTED_OPERATIONS
    schemas = contract["components"]["schemas"]
    for name in (
        "ItemView", "ItemSummary", "LookupResponse", "ItemListResponse", "EpisodesResponse",
        "PatchBody", "PatchResult", "RevertBody", "LockBody", "HistoryResponse", "EditResponse",
        "Edit", "BackupStatusResponse", "FieldsResponse", "EditableFields", "PatchFields",
        "MetadataErrorResponse", "MetadataErrorDetail",
    ):
        assert name in schemas, f"schéma absent du contrat : {name}"


def test_error_models_do_not_rename_the_scrape_gateway_components():
    """Deux modèles de même nom font préfixer les DEUX composants par leur module : le contrat
    de la passerelle B1 (`ErrorResponse`) serait cassé."""
    from app.main import app

    names = set(app.openapi()["components"]["schemas"])
    assert "ErrorResponse" in names and "ErrorDetail" in names
    assert not [n for n in names if n.startswith("app__")]


def test_export_is_self_contained():
    contract = build_contract()
    found: set[str] = set()
    _refs(contract["paths"], found)
    _refs(contract["components"], found)
    assert found <= set(contract["components"]["schemas"])
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


def test_export_declares_the_headers_and_error_responses():
    contract = build_contract()
    patch = contract["paths"]["/api/metadata/v1/items/{serverId}/{ratingKey}"]["patch"]
    headers = {p["name"] for p in patch["parameters"] if p["in"] == "header"}
    assert {"If-Match", "Idempotency-Key"} <= headers
    for status in ("400", "401", "403", "404", "409", "412", "429", "503"):
        assert status in patch["responses"], status


def test_export_has_no_host_and_no_secret():
    contract = build_contract()
    assert "servers" not in contract
    text = render_contract()
    assert not re.search(r"https?://", text), "aucun hôte ni URL dans le contrat"
    for forbidden in ("phk_", "api_key=", "apikey=", "password", "localhost", "192.168."):
        assert forbidden not in text.lower(), f"motif interdit dans le contrat : {forbidden}"
