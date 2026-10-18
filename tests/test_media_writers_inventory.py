"""B2-08 — l'inventaire des écrivains de la table `media` est figé (ADR 0012 § D2.2, contrat B2 § 8.2).

Le verrou de ligne n'a AUCUNE garde centrale : chaque site qui écrit dans `media` doit décider,
explicitement, s'il saute les lignes verrouillées pour les colonnes de
`metadata_columns.METADATA_WRITABLE_COLUMNS`. Ce test analyse le code de `app/` (AST, sans l'exécuter)
et échoue dès qu'un site d'écriture apparaît, disparaît ou change de nombre sans que la liste
ci-dessous (avec sa justification) ne soit mise à jour.

Catégories :
- `protected`  : saute les lignes verrouillées (testé dans `test_metadata_lock_protection.py`) ;
- `operator`   : écriture explicite d'un opérateur/client (elle pose ou respecte le verrou elle-même) ;
- `exempt`     : n'écrit AUCUNE colonne protégée, ou exception assumée (documentée ADR 0012).

Quand ce test échoue : ajouter la garde `match_locked` au nouvel écrivain, puis le lister ici.
"""
from __future__ import annotations

import ast
import re
from collections import Counter
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"

_WRITE_FUNCS = {"update", "delete", "insert", "sqlite_insert", "sqlite_upsert"}
_RAW_SQL = re.compile(r"\b(UPDATE|DELETE\s+FROM|INSERT\s+(?:OR\s+\w+\s+)?INTO)\s+media\b", re.IGNORECASE)


def _is_media(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        return node.id == "Media"
    if isinstance(node, ast.Attribute):
        return node.attr == "Media" or (node.attr == "__table__" and _is_media(node.value))
    return False


def scan_media_writers() -> Counter[tuple[str, str, str]]:
    """Count the write sites on `media`: (file, enclosing function, operation)."""
    found: Counter[tuple[str, str, str]] = Counter()
    for path in sorted(APP.rglob("*.py")):
        rel = path.relative_to(APP.parent).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.body and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant) and isinstance(node.body[0].value.value, str)
        }
        stack: list[str] = []

        class Visitor(ast.NodeVisitor):
            def visit_FunctionDef(self, node):  # noqa: N802 - ast API
                stack.append(node.name)
                self.generic_visit(node)
                stack.pop()

            visit_AsyncFunctionDef = visit_FunctionDef  # noqa: N815

            def visit_Call(self, node):  # noqa: N802
                func = node.func
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
                where = stack[-1] if stack else "<module>"
                if name in _WRITE_FUNCS and node.args and _is_media(node.args[0]):
                    found[(rel, where, f"{name}(Media)")] += 1
                elif name == "Media" and isinstance(func, ast.Name):
                    found[(rel, where, "Media(...)")] += 1
                self.generic_visit(node)

            def visit_Constant(self, node):  # noqa: N802
                if isinstance(node.value, str) and id(node) not in docstrings and _RAW_SQL.search(node.value):
                    found[(rel, stack[-1] if stack else "<module>", "raw SQL")] += 1

        Visitor().visit(tree)
    return found


# (file, function, operation) -> (expected sites, category, why)
EXPECTED: dict[tuple[str, str, str], tuple[int, str, str]] = {
    # ── sync: provider content change ───────────────────────────────────────
    ("app/workers/sync_worker.py", "upsert_media_batch", "sqlite_upsert(Media)"): (
        1, "protected", "CASE(match_locked) over METADATA_WRITABLE_COLUMNS + tmdb_id/unification/history blocks"),
    ("app/workers/sync_worker.py", "upsert_media_batch", "update(Media)"): (
        1, "exempt", "page_offset relocation only (pagination slot, not a metadata column)"),
    ("app/workers/sync_worker.py", "upsert_media_batch", "delete(Media)"): (
        1, "exempt", "evicts the row occupying a pagination slot (provider removed it)"),
    ("app/workers/sync_worker.py", "apply_series_trailers", "update(Media)"): (
        1, "protected", "WHERE match_locked = false"),
    ("app/workers/sync_worker.py", "differential_cleanup", "delete(Media)"): (
        1, "exempt", "assumed exception: a title the provider removed disappears (edit history stays readable)"),
    ("app/workers/sync_worker.py", "differential_cleanup_filtered", "delete(Media)"): (
        1, "exempt", "assumed exception: same as differential_cleanup"),
    ("app/workers/sync_worker.py", "differential_cleanup_episodes", "delete(Media)"): (
        1, "exempt", "assumed exception: same as differential_cleanup"),
    # ── automatic enrichment / back-fills / scripts ─────────────────────────
    ("app/workers/enrichment_worker.py", "_apply_enrichment_results", "update(Media)"): (
        1, "protected", "WHERE match_locked = false (ADR 0005 L1)"),
    ("app/workers/enrichment_backfill_worker.py", "_write_page", "update(Media)"): (
        1, "protected", "candidates and write both exclude locked rows"),
    ("app/scripts/backfill_certifications.py", "run", "update(Media)"): (
        1, "protected", "candidates and write both exclude locked rows"),
    ("app/services/trailer_service.py", "_work", "update(Media)"): (
        1, "protected", "WHERE match_locked = false (a locked empty trailer may be a deliberate clear)"),
    ("app/scripts/strip_titles_pollution.py", "_update_db_for_account", "update(Media)"): (
        2, "protected", "locked movies/shows skipped; locked episodes keep their title"),
    ("app/scripts/validate_id_consistency.py", "_apply_fix", "update(Media)"): (
        1, "protected", "WHERE match_locked = false (ADR 0005 L6)"),
    ("app/scripts/dedup_resolved_twins.py", "main", "raw SQL"): (
        1, "protected", "AND match_locked = 0 (ADR 0005 L6)"),
    ("app/cli.py", "_run_recompute_unification", "update(Media)"): (
        1, "protected", "locked rows excluded from the scan and from the UPDATE"),
    ("app/services/nfo_import_service.py", "_import_movies_for_account", "update(Media)"): (
        1, "protected", "values come from _compute_updates, which skips protected columns of a locked row"),
    ("app/services/nfo_import_service.py", "_apply_show_nfo", "update(Media)"): (
        1, "protected", "values come from _compute_updates, which skips protected columns of a locked row"),
    # ── explicit operator / client writes ───────────────────────────────────
    ("app/services/metadata_service.py", "write", "update(Media)"): (
        1, "operator", "the metadata API itself: If-Match, journal, sets the lock"),
    ("app/services/media_service.py", "update_external_ids", "update(Media)"): (
        1, "operator", "legacy PATCH /api/media: an explicit id edit that sets the lock"),
    ("app/services/manual_scrape_service.py", "work", "update(Media)"): (
        4, "operator", "manual scraper apply/propagate/unlock/clear: honors the lock for non-manual sources"),
    # ── no protected column written (or assumed exception) ──────────────────
    ("app/services/category_service.py", "update_media_category_visibility", "update(Media)"): (
        7, "exempt", "is_in_allowed_categories visibility flag only"),
    ("app/services/category_service.py", "_work", "update(Media)"): (
        3, "exempt", "assumed exception: adult classification (is_adult, content_rating='XXX') prevails; "
                     "the metadata API already refuses contentRating on an adult item"),
    ("app/utils/rating_blend.py", "recompute_display_rating_stmt", "update(Media)"): (
        1, "exempt", "display_rating derived from imdb/tmdb ratings, which are protected"),
    ("app/workers/health_check_worker.py", "_apply_batch", "update(Media)"): (
        1, "exempt", "stream health flags (is_broken, last_stream_check, file_size)"),
    ("app/workers/health_check_worker.py", "_run_pipeline_validation_impl", "update(Media)"): (
        1, "exempt", "stream health flags"),
    ("app/services/account_service.py", "delete_account_cascade", "delete(Media)"): (
        1, "exempt", "the owning Xtream account is being deleted"),
}


def test_the_inventory_of_media_writers_is_pinned():
    actual = scan_media_writers()
    expected = Counter({key: count for key, (count, _cat, _why) in EXPECTED.items()})
    new = {k: actual[k] for k in actual if actual[k] != expected.get(k, 0)}
    gone = {k: expected[k] for k in expected if actual.get(k, 0) != expected[k]}
    message = []
    if new:
        message.append(
            "NEW/CHANGED writer(s) of `media` (give them a `match_locked` guard for the columns of "
            "METADATA_WRITABLE_COLUMNS, then list them in EXPECTED with a justification): "
            + "; ".join(f"{k} x{v}" for k, v in sorted(new.items()))
        )
    if gone:
        message.append("EXPECTED writer(s) missing/changed (remove or update the entry): "
                       + "; ".join(f"{k} x{v}" for k, v in sorted(gone.items())))
    assert not message, "\n".join(message)


def test_every_entry_is_justified_and_categorised():
    for key, (count, category, why) in EXPECTED.items():
        assert count >= 1, key
        assert category in {"protected", "operator", "exempt"}, key
        assert len(why) >= 15, key


def test_the_scanner_detects_the_patterns_it_claims(tmp_path):
    """Self-check on a fabricated module: every write shape must be seen."""
    source = '''
from sqlalchemy import delete, update
from sqlalchemy.dialects.sqlite import insert as sqlite_upsert
"""docstring mentioning UPDATE media is ignored"""

async def writer(db):
    """UPDATE media in a docstring is ignored too"""
    await db.execute(update(Media).values(a=1))
    await db.execute(delete(Media))
    await db.execute(sqlite_upsert(Media).values([]))
    await db.execute(text("UPDATE media SET a = 1"))
    db.add(Media(rating_key="x"))
'''
    tree = ast.parse(source)
    ops = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name in _WRITE_FUNCS and node.args and _is_media(node.args[0]):
                ops.append(name)
            elif name == "Media":
                ops.append("Media(...)")
    assert sorted(ops) == ["Media(...)", "delete", "sqlite_upsert", "update"]
    assert _RAW_SQL.search("UPDATE media SET a = 1")
    assert not _RAW_SQL.search("UPDATE media_group SET a = 1")
