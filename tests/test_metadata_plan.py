"""B2-04b — révision, `plan_patch`, `plan_lock`, `plan_revert` (ADR 0011 § D2.1–D2.6).
Noyau PUR : aucune I/O ; les variantes sont construites depuis des dicts de colonnes.
"""
from __future__ import annotations

import pytest

from app.services.metadata_fields import normalize_patch
from app.services.metadata_plan import (
    RevertConflict,
    compute_revision,
    plan_lock,
    plan_patch,
    plan_revert,
    variant_from_columns,
    variant_images,
)


def cols(**kw):
    base = dict(
        rating_key="vod_1.mkv", server_id="xtream_a", filter="all", sort_order="default",
        type="movie", title="The Matrix", year=1999, imdb_id=None, tmdb_id=None, tvdb_id=None,
        wikidata_id=None, original_title=None, summary="old", tagline=None, genres="Action",
        studio=None, country=None, content_rating=None, premiered=None, status=None,
        duration=None, imdb_rating=None, imdb_votes=None, tmdb_rating=None, tmdb_votes=None,
        cast=None, cast_json=None, youtube_trailer=None,
        unification_id="title_the_matrix_1999", history_group_key="title_the_matrix_1999",
        title_sortable="the matrix", display_rating=6.0, match_locked=False, match_source=None,
        is_adult=False,
    )
    base.update(kw)
    return base


def V(**kw):
    return variant_from_columns(cols(**kw))


def patch(variants, body, **flags):
    t = variants[0].type
    return plan_patch(variants, normalize_patch(body, t, is_adult=variants[0].is_adult), **flags)


# ─── révision ───────────────────────────────────────────────────────────────


def test_revision_is_22_chars_url_safe_and_stable():
    rev = compute_revision([V()])
    assert len(rev) == 22 and all(c.isalnum() or c in "-_" for c in rev)
    assert compute_revision([V()]) == rev


@pytest.mark.parametrize(
    "change",
    [
        dict(title="Other"), dict(summary="new"), dict(genres="Drame"), dict(year=2000),
        dict(imdb_id="tt0133093"), dict(imdb_rating=7.5), dict(imdb_votes=3, imdb_rating=7.5),
        dict(cast="A, B"), dict(unification_id="imdb://tt1"), dict(history_group_key="x"),
        dict(title_sortable="z"), dict(display_rating=7.0), dict(match_locked=True),
        dict(match_locked=True, match_source="manual"), dict(duration=5000), dict(youtube_trailer="dQw4w9WgXcQ"),
    ],
)
def test_revision_changes_with_every_field_derived_or_lock(change):
    assert compute_revision([V(**change)]) != compute_revision([V()])


def test_revision_ignores_what_it_does_not_cover():
    # not editable/derived: images, flow, read state, flags, hashes
    assert compute_revision([V(thumb_url="x", is_broken=True, content_hash="h", updated_at=5)]) == compute_revision([V()])


def test_revision_covers_every_variant_and_ignores_input_order():
    a, b = V(), V(filter="cat2")
    rev = compute_revision([a, b])
    assert compute_revision([b, a]) == rev
    assert compute_revision([a, V(filter="cat2", summary="diverged")]) != rev
    assert compute_revision([a]) != rev


def test_revision_rounds_floats_at_1e_6():
    assert compute_revision([V(display_rating=6.0)]) == compute_revision([V(display_rating=6.0000000001)])
    assert compute_revision([V(display_rating=6.0)]) != compute_revision([V(display_rating=6.01)])


# ─── plan_patch : base ──────────────────────────────────────────────────────


def test_empty_or_equal_patch_is_a_noop_without_lock():
    for body in ({}, {"summary": "old"}, {"genres": ["Action"]}, {"title": "  The Matrix "}):
        plan = patch([V()], body)
        assert plan.noop is True
        assert plan.changed_fields == []
        assert plan.revision_after == plan.revision_before
        assert all(not c.updates for c in plan.changes)


def test_write_sets_columns_lock_and_source_on_a_free_row():
    plan = patch([V()], {"summary": "new", "genres": ["Drame", "Action"]})
    assert plan.noop is False
    assert plan.changed_fields == ["genres", "summary"]
    (change,) = plan.changes
    assert change.updates["summary"] == "new" and change.updates["genres"] == "Drame, Action"
    assert change.updates["match_locked"] is True and change.updates["match_source"] == "plexhubtmm"
    assert plan.lock_before.locked is False and plan.lock_after.locked is True
    assert plan.lock_after.source == "plexhubtmm"
    assert plan.revision_after != plan.revision_before
    assert plan.revision_after == compute_revision(plan.after)


def test_an_already_locked_row_keeps_its_match_source():
    plan = patch([V(match_locked=True, match_source="manual")], {"summary": "new"})
    (change,) = plan.changes
    assert "match_locked" not in change.updates and "match_source" not in change.updates
    assert plan.lock_after.locked is True and plan.lock_after.source == "manual"


def test_a_locked_row_without_source_stays_without_source():
    plan = patch([V(match_locked=True, match_source=None)], {"summary": "new"})
    assert plan.lock_after.source is None


def test_lock_is_applied_to_every_variant_even_unchanged_ones():
    a, b = V(summary="same"), V(filter="cat2", summary="other")
    plan = patch([a, b], {"summary": "same"})  # changes variant b only (alignment)
    assert plan.noop is False
    by = {c.filter: c for c in plan.changes}
    assert by["all"].updates.get("match_locked") is True       # unchanged fields, still locked
    assert "summary" not in by["all"].updates
    assert by["cat2"].updates["summary"] == "same"
    assert plan.changed_fields == ["summary"]


def test_variants_are_ordered_canonical_first_by_bytes():
    plan = patch([V(filter="b"), V(filter="B"), V(filter="a")], {"summary": "x"})
    assert [v.filter for v in plan.before] == ["B", "a", "b"]  # 'B'(0x42) < 'a'(0x61) < 'b'
    assert [c.filter for c in plan.changes] == ["B", "a", "b"]


def test_divergent_variants_keep_their_untouched_fields():
    a, b = V(genres="Action"), V(filter="cat2", genres="Comedie")
    plan = patch([a, b], {"summary": "n"})
    after = {v.filter: v for v in plan.after}
    assert after["all"].flat["genres"] == ["Action"] and after["cat2"].flat["genres"] == ["Comedie"]


# ─── plan_patch : dérivés ───────────────────────────────────────────────────


def test_adding_an_imdb_id_regroups_a_movie():
    plan = patch([V()], {"ids": {"imdb": "tt0133093"}})
    (change,) = plan.changes
    assert change.updates["imdb_id"] == "tt0133093"
    assert change.updates["unification_id"] == "imdb://tt0133093"
    assert change.updates["history_group_key"] == "imdb://tt0133093"
    assert plan.unification_before == "title_the_matrix_1999"
    assert plan.unification_after == "imdb://tt0133093"
    assert plan.regroup_changes is True


def test_tmdb_id_regroups_when_no_imdb():
    plan = patch([V()], {"ids": {"tmdb": "603"}})
    assert plan.changes[0].updates["unification_id"] == "tmdb://603"


def test_title_change_on_an_idless_movie_regroups_and_resorts():
    plan = patch([V()], {"title": "Matrix Reloaded", "year": 2003})
    u = plan.changes[0].updates
    assert u["title"] == "Matrix Reloaded" and u["title_sortable"] == "matrix reloaded"
    assert u["unification_id"] == "title_matrix_reloaded_2003"
    assert u["history_group_key"] == "title_matrix_reloaded_2003"


def test_title_change_does_not_regroup_a_movie_that_has_an_id():
    v = V(imdb_id="tt0133093", unification_id="imdb://tt0133093", history_group_key="imdb://tt0133093")
    plan = patch([v], {"title": "Autre"})
    assert "unification_id" not in plan.changes[0].updates
    assert plan.regroup_changes is False


def test_unrelated_field_never_recomputes_unification():
    v = V(unification_id="legacy-value", history_group_key="legacy-key")
    plan = patch([v], {"summary": "n"})
    assert "unification_id" not in plan.changes[0].updates
    assert "history_group_key" not in plan.changes[0].updates


def test_unknown_title_yields_empty_unification_and_falls_back_to_the_rating_key():
    plan = patch([V(unification_id="x", history_group_key="x")], {"title": "Unknown"})
    u = plan.changes[0].updates
    assert u["unification_id"] == "" and u["history_group_key"] == "vod_1.mkvxtream_a"


def test_episodes_never_change_unification_or_history_key():
    """Deux épisodes « Pilot » de séries différentes : aucune fusion d'historique."""
    for rk, series in (("ep_1.mkv", "series_1"), ("ep_2.mkv", "series_2")):
        v = V(type="episode", rating_key=rk, title="Old", unification_id="", history_group_key=f"{rk}xtream_a",
              grandparent_rating_key=series)
        plan = patch([v], {"title": "Pilot", "summary": "s"})
        u = plan.changes[0].updates
        assert u["title"] == "Pilot" and u["title_sortable"] == "pilot"
        assert "unification_id" not in u and "history_group_key" not in u
        assert plan.regroup_changes is False
        assert plan.unification_after == ""


def test_ratings_update_display_rating_with_the_blend():
    plan = patch([V(display_rating=6.0)], {"ratings": {"imdb": {"value": 8.0, "votes": 10}}})
    u = plan.changes[0].updates
    assert u["imdb_rating"] == 8.0 and u["imdb_votes"] == 10
    assert u["display_rating"] == 8.0
    both = patch([V(tmdb_rating=6.0, display_rating=6.0)], {"ratings": {"imdb": {"value": 8.0}}})
    assert both.changes[0].updates["display_rating"] == 7.0


def test_clearing_every_rating_leaves_display_rating_untouched():
    v = V(imdb_rating=8.0, imdb_votes=2, display_rating=8.0)
    plan = patch([v], {"ratings": {"imdb": None}})
    u = plan.changes[0].updates
    assert u["imdb_rating"] is None and u["imdb_votes"] is None
    assert "display_rating" not in u


def test_derived_values_are_computed_per_variant():
    a, b = V(title="Same"), V(filter="cat2", title="Other")
    plan = patch([a, b], {"ids": {"imdb": "tt0133093"}})
    assert {c.filter: c.updates["unification_id"] for c in plan.changes} == {
        "all": "imdb://tt0133093", "cat2": "imdb://tt0133093",
    }


# ─── plan_lock ──────────────────────────────────────────────────────────────


def test_lock_on_a_free_row_and_noop_when_already_locked():
    plan = plan_lock([V()], locked=True)
    assert plan.kind == "lock" and plan.noop is False and plan.changed_fields == []
    assert plan.changes[0].updates == {"match_locked": True, "match_source": "plexhubtmm"}
    assert plan.revision_after != plan.revision_before
    assert plan_lock([V(match_locked=True, match_source="manual")], locked=True).noop is True


def test_unlock_clears_source_and_noop_when_already_free():
    plan = plan_lock([V(match_locked=True, match_source="manual")], locked=False)
    assert plan.changes[0].updates == {"match_locked": False, "match_source": None}
    assert plan.lock_after.locked is False and plan.lock_after.source is None
    assert plan_lock([V()], locked=False).noop is True


def test_lock_on_mixed_variants_only_touches_the_ones_that_differ():
    a, b = V(match_locked=True, match_source="manual"), V(filter="cat2")
    plan = plan_lock([a, b], locked=True)
    by = {c.filter: c.updates for c in plan.changes}
    assert by["all"] == {} and by["cat2"] == {"match_locked": True, "match_source": "plexhubtmm"}


# ─── plan_revert ────────────────────────────────────────────────────────────


def _apply(variants, plan):
    """Rebuild the post-write variants from a plan (what the DB would hold)."""
    return list(plan.after)


def _edit(plan):
    return {"before": variant_images(plan.before), "after": variant_images(plan.after)}


def test_revert_restores_fields_derived_and_lock():
    before = [V()]
    p = patch(before, {"ids": {"imdb": "tt0133093"}, "summary": "new"})
    edit = _edit(p)
    r = plan_revert(_apply(before, p), edit, lock_newer_edit=False)
    assert r.kind == "revert" and r.noop is False
    u = r.changes[0].updates
    assert u["summary"] == "old" and u["imdb_id"] is None
    assert u["unification_id"] == "title_the_matrix_1999"
    assert u["history_group_key"] == "title_the_matrix_1999"
    assert u["match_locked"] is False and u["match_source"] is None
    assert r.revision_after == compute_revision(before)
    assert r.unification_before == "imdb://tt0133093" and r.unification_after == "title_the_matrix_1999"
    assert r.regroup_changes is True
    assert sorted(r.changed_fields) == ["ids.imdb", "summary"]


def test_revert_restores_divergent_variants_exactly():
    before = [V(summary="A"), V(filter="cat2", summary="B")]
    p = patch(before, {"summary": "Z"})
    r = plan_revert(_apply(before, p), _edit(p), lock_newer_edit=False)
    by = {c.filter: c.updates for c in r.changes}
    assert by["all"]["summary"] == "A" and by["cat2"]["summary"] == "B"
    assert r.revision_after == compute_revision(before)


def test_revert_display_rating_is_restored_from_the_record_not_recomputed():
    before = [V(display_rating=6.0)]
    p = patch(before, {"ratings": {"imdb": {"value": 9.0}}})
    r = plan_revert(_apply(before, p), _edit(p), lock_newer_edit=False)
    assert r.changes[0].updates["display_rating"] == 6.0
    assert r.changes[0].updates["imdb_rating"] is None


def test_revert_conflicts_when_a_field_changed_since_on_any_variant():
    before = [V(), V(filter="cat2")]
    p = patch(before, {"summary": "Z", "genres": ["Drame"]})
    now = list(p.after)
    now[1] = V(filter="cat2", summary="changed-by-sync", genres="Drame", match_locked=True, match_source="plexhubtmm",
               unification_id="title_the_matrix_1999", history_group_key="title_the_matrix_1999")
    with pytest.raises(RevertConflict) as exc:
        plan_revert(now, _edit(p), lock_newer_edit=False)
    assert exc.value.fields == ["summary"]


def test_revert_lock_rule():
    before = [V()]
    p = patch(before, {"summary": "Z"})
    edit = _edit(p)
    # a newer non-reverted edit exists -> the lock cannot be restored
    with pytest.raises(RevertConflict) as exc:
        plan_revert(_apply(before, p), edit, lock_newer_edit=True)
    assert exc.value.fields == ["lock"]
    # lock changed since (e.g. unlocked by someone) -> conflict too
    unlocked = [V(summary="Z", match_locked=False, match_source=None)]
    with pytest.raises(RevertConflict) as exc:
        plan_revert(unlocked, edit, lock_newer_edit=False)
    assert exc.value.fields == ["lock"]


def test_revert_of_an_edit_that_did_not_change_the_lock_ignores_newer_edits():
    before = [V(match_locked=True, match_source="manual")]
    p = patch(before, {"summary": "Z"})
    r = plan_revert(_apply(before, p), _edit(p), lock_newer_edit=True)
    assert "match_locked" not in r.changes[0].updates
    assert r.changes[0].updates["summary"] == "old"


def test_revert_of_a_lock_edit():
    before = [V()]
    p = plan_lock(before, locked=True)
    r = plan_revert(_apply(before, p), _edit(p), lock_newer_edit=False)
    assert r.changes[0].updates == {"match_locked": False, "match_source": None}
    assert r.changed_fields == []


def test_a_revert_can_itself_be_reverted():
    before = [V()]
    p1 = patch(before, {"summary": "Z"})
    r = plan_revert(_apply(before, p1), _edit(p1), lock_newer_edit=False)
    r2 = plan_revert(list(r.after), _edit(r), lock_newer_edit=False)
    assert r2.changes[0].updates["summary"] == "Z"
    assert r2.revision_after == p1.revision_after


def test_revert_skips_a_variant_that_disappeared_and_fails_when_none_remain():
    before = [V(), V(filter="cat2")]
    p = patch(before, {"summary": "Z"})
    edit = _edit(p)
    r = plan_revert([p.after[0]], edit, lock_newer_edit=False)
    assert [c.filter for c in r.changes] == ["all"]
    with pytest.raises(RevertConflict) as exc:
        plan_revert([V(filter="brand-new")], edit, lock_newer_edit=False)
    assert exc.value.fields == ["variants"]
