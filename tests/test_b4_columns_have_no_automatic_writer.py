"""Lot B4 (ADR 0013): the user-entered scrape columns are written ONLY by the metadata API.

No sync / enrichment / NFO / script module may even mention them: a user entry must never be
overwritten, and `match_locked` is a second line of defence, not the first.
"""
from __future__ import annotations

import re
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"

# Columns that exist ONLY since B4. (`poster_url`/`fanart_url` are also NFO/Plex-generator
# attribute names, and `directors`/`writers` NFO fields; they are checked as column references.)
UNIQUE_NAMES = (
    "poster_gallery_json", "fanart_gallery_json", "available_trailers_json",
    "metacritic_rating", "metacritic_votes", "rt_rating", "rt_votes", "spoken_languages",
)
COLUMN_REFERENCES = re.compile(r"Media\.(poster_url|fanart_url|directors|writers)\b")

ALLOWED = {
    "app/models/database.py",
    "app/db/migrations.py",
    "app/models/metadata_schemas.py",
    "app/services/metadata_columns.py",
    "app/services/metadata_fields.py",
    "app/services/metadata_service.py",
    "app/services/metadata_views.py",
}


def _sources():
    for path in APP.rglob("*.py"):
        rel = path.relative_to(APP.parent).as_posix()
        if rel not in ALLOWED:
            yield rel, path.read_text(encoding="utf-8")


def test_no_other_module_names_the_b4_columns():
    offenders = []
    for rel, text in _sources():
        if any(name in text for name in UNIQUE_NAMES) or COLUMN_REFERENCES.search(text):
            offenders.append(rel)
    assert offenders == []


def test_sync_upsert_payloads_never_carry_the_b4_columns():
    text = (APP / "workers" / "sync_worker.py").read_text(encoding="utf-8")
    for name in ("poster_url", "fanart_url", "directors", "writers"):
        assert f'"{name}"' not in text and f"'{name}'" not in text
