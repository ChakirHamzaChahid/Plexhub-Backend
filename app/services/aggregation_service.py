"""Shared media aggregation / dedup logic.

Groups raw `media` rows that represent the SAME title across accounts (and across
quality/language variants within one account) by `unification_id`. Used by both
the Plex/Jellyfin library generator (`plex_generator.source`) and the REST API
(`api/media` unified endpoints) so the two paths dedup identically.

Pure functions on `Media` rows — no DB, no I/O — so they're trivially testable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from app.models.database import Media
from app.utils.string_normalizer import parse_title_year_and_suffix
from app.utils.unification import calculate_unification_id


def group_key(row: Media) -> str:
    """Dedup key. Prefers the cross-source `unification_id`
    (imdb://… > tmdb://… > title_…_year). Falls back to a per-row key so a row
    with no resolvable identity stays its own group (never a false merge)."""
    return row.unification_id or f"{row.server_id}:{row.rating_key}"


def is_enriched(row: Media) -> bool:
    return bool(row.imdb_id) or bool(row.tmdb_id and str(row.tmdb_id).isdigit())


def best_row(rows: list[Media]) -> Media:
    """Pick the row whose metadata best represents the group (NFO/poster/API
    card). Preference: enriched (imdb/tmdb) > has poster > higher rating > has a
    year > longer title. Deterministic tie-break on (server_id, rating_key)."""
    def key(r: Media):
        return (
            is_enriched(r),
            bool(r.resolved_thumb_url or r.thumb_url),
            r.display_rating or r.scraped_rating or 0.0,
            r.year is not None,
            len(r.title or ""),
        )
    return max(rows, key=lambda r: (key(r), r.server_id or "", r.rating_key or ""))


def canonical_title_year(row: Media) -> tuple[str, int | None]:
    """Clean display title + year for a group's representative row.

    Strips the version qualifier (VF/HD/VOSTFR/…) and any embedded year so the
    deduped entry shows a single clean title — e.g. "Terminator (1984) (VF)" →
    ("Terminator", 1984). The qualifier still lives on each version's label."""
    clean, parsed_year, _ = parse_title_year_and_suffix(row.title or "")
    year = row.year if row.year is not None else parsed_year
    return clean, year


def version_label(row: Media, account_label: str) -> str:
    """Human-readable version label, e.g. "VF · Compte 1" / "Compte 1".

    Qualifier (VF/HD/VOSTFR…) comes from the source title; the account label
    disambiguates the same qualifier across providers."""
    _, _, suffix = parse_title_year_and_suffix(row.title or "")
    acc = (account_label or "").strip()
    if suffix and acc:
        return f"{suffix} · {acc}"
    if suffix:
        return suffix
    return acc or "v"


def dedup_labels(labels: list[str]) -> list[str]:
    """Ensure version labels are unique within a group (append #n on collision)."""
    seen: dict[str, int] = {}
    out: list[str] = []
    for label in labels:
        n = seen.get(label, 0) + 1
        seen[label] = n
        out.append(label if n == 1 else f"{label} #{n}")
    return out


def build_versions(
    members: list[Media],
    label_for: Callable[[Media], str],
) -> list[tuple[Media, str]]:
    """Sort members by stable identity, label each, then dedup-suffix collisions.

    CR-A07: this is the determinism-critical sequence that USED to be
    copy-pasted between the REST `/unified` endpoints (`api/media.py`) and the
    Plex/Jellyfin generator (`plex_generator/source.py.DatabaseSource`), with
    in-code comments in both requiring them to stay byte-identical. It now
    lives here ONCE, and both callers delegate to it.

    Members are sorted by ``(server_id, rating_key)`` BEFORE labelling so
    `dedup_labels`' ``#n`` collision suffix always lands on the same physical
    version regardless of DB row order — otherwise a version's label (and
    therefore its `.strm` filename in the generator) could flip between runs
    whenever a sync/enrichment pass rewrites rows in a different physical
    order.

    `label_for(row)` resolves the account-level label component for a member
    row — a `server_id -> label` dict lookup for the API, an `XtreamAccount`
    attribute for the generator. Callers differ only in that lookup and in
    what output type they map the returned ``(row, label)`` pairs to
    (`MediaVersionResponse` vs a stream-URL triple); they may also filter
    `members` differently before/after calling this (e.g. the generator drops
    unresolvable/broken sources) — but the sort/label/dedup sequence itself
    must stay identical, and this function is the single source of truth for
    it.
    """
    ordered = sorted(members, key=lambda m: (m.server_id or "", m.rating_key or ""))
    raw_labels = [version_label(m, label_for(m)) for m in ordered]
    return list(zip(ordered, dedup_labels(raw_labels)))


@dataclass
class MovieGroup:
    key: str
    best: Media
    members: list[Media] = field(default_factory=list)


@dataclass
class EpisodeSlot:
    season: int
    episode: int
    best: Media
    members: list[Media] = field(default_factory=list)


@dataclass
class SeriesGroup:
    key: str
    best: Media
    slots: list[EpisodeSlot] = field(default_factory=list)


def _key_rank(k: str) -> tuple[int, str]:
    """Priority for choosing a cluster's representative key: imdb > tmdb > title > fallback."""
    if k.startswith("imdb://"):
        return (0, k)
    if k.startswith("tmdb://"):
        return (1, k)
    if k.startswith("title_"):
        return (2, k)
    return (3, k)


def _id_tokens(row: Media) -> list[str]:
    """Stable external-id tokens for a row (`imdb:tt…`, `tmdb:123`).

    Used to merge rows that designate the SAME entity even though their derived
    `unification_id` strings differ (imdb takes priority over tmdb in
    calculate_unification_id, so a row carrying imdb+tmdb keys as `imdb://…`
    while its tmdb-only twin keys as `tmdb://…` — same film, different key)."""
    toks: list[str] = []
    if row.imdb_id and str(row.imdb_id).strip():
        toks.append(f"imdb:{str(row.imdb_id).strip()}")
    tmdb = str(row.tmdb_id).strip() if row.tmdb_id is not None else ""
    if tmdb.isdigit() and tmdb != "0":
        toks.append(f"tmdb:{tmdb}")
    return toks


def _merge_by_shared_ids(groups: dict[str, list[Media]]) -> dict[str, list[Media]]:
    """Pass A — fold together groups that share a non-null imdb_id OR tmdb_id.

    Precise (no homonym risk): two rows linked only if they carry the same
    physical external id. Repairs the "imdb vs tmdb" split where the same film
    is keyed `imdb://…` on one row and `tmdb://…` on another. The surviving key
    per cluster is the strongest one (imdb > tmdb > title)."""
    if len(groups) < 2:
        return groups
    parent = {k: k for k in groups}

    def find(x: str) -> str:
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    seen: dict[str, str] = {}
    for k, rows in groups.items():
        for row in rows:
            for token in _id_tokens(row):
                if token in seen:
                    union(k, seen[token])
                else:
                    seen[token] = k

    clusters: dict[str, list[str]] = {}
    for k in groups:
        clusters.setdefault(find(k), []).append(k)
    if len(clusters) == len(groups):
        return groups  # nothing merged

    merged: dict[str, list[Media]] = {}
    for member_keys in clusters.values():
        rep = min(member_keys, key=_key_rank)
        bucket = merged.setdefault(rep, [])
        for k in member_keys:
            bucket.extend(groups[k])
    return merged


def _absorb_title_groups(groups: dict[str, list[Media]]) -> dict[str, list[Media]]:
    """Pass B — fold an unresolved `title_…` group into an id-based group of the
    SAME canonical title+year (one side resolved via TMM/TMDB, its twin didn't).

    Reuses calculate_unification_id for an identical title normalization, and
    skips degenerate (empty) titles — e.g. non-latin titles that normalize to
    `title__<year>` — so unrelated foreign films never false-merge.

    CR-F09: when TWO (or more) distinct id-based groups normalize to the same
    title+year, they are all *candidates* to absorb the same `title_…` twin.
    Collect every candidate first (order in which `groups` is iterated must not
    matter), then pick the absorbing group with `min(..., key=_key_rank)` — the
    same total, input-order-independent order Pass A already uses to pick a
    cluster's representative (imdb > tmdb > title > fallback, then lexicographic
    key). This makes the resulting grouping reproducible regardless of DB/query
    row order."""
    if len(groups) < 2:
        return groups
    candidates: dict[str, list[str]] = {}
    for k, rows in groups.items():
        if "://" not in k:
            continue  # only id-based groups absorb a title twin
        best = best_row(rows)
        base = calculate_unification_id(best.title or "", None)  # 'title_<norm>' / ''
        if not base.startswith("title_") or not any(c.isalnum() for c in base[len("title_"):]):
            continue  # degenerate / Unknown title — don't absorb
        tkey = calculate_unification_id(best.title or "", best.year)
        if tkey in groups:
            candidates.setdefault(tkey, []).append(k)
    if not candidates:
        return groups
    # Deterministic winner per title-twin key: smallest by _key_rank, independent
    # of the order `groups`/`candidates` were populated in.
    remap: dict[str, str] = {tkey: min(ks, key=_key_rank) for tkey, ks in candidates.items()}
    merged: dict[str, list[Media]] = {}
    for k, rows in groups.items():
        merged.setdefault(remap.get(k, k), []).extend(rows)
    return merged


def _converge(groups: dict[str, list[Media]]) -> dict[str, list[Media]]:
    """Repair split identities so one entity = one group (see Pass A / Pass B).

    Shared by movies and series; runs on the unification_id→rows map BEFORE the
    final groups are built, so both the REST `/unified` endpoints and the Plex
    generator dedup identically."""
    return _absorb_title_groups(_merge_by_shared_ids(groups))


def aggregate_movies(rows: list[Media]) -> list[MovieGroup]:
    """Group movie rows by unification key into one MovieGroup each."""
    groups: dict[str, list[Media]] = {}
    for row in rows:
        groups.setdefault(group_key(row), []).append(row)
    groups = _converge(groups)
    return [MovieGroup(key=k, best=best_row(rs), members=rs) for k, rs in groups.items()]


def aggregate_series(shows: list[Media], episodes: list[Media]) -> list[SeriesGroup]:
    """Group shows by unification key, then merge every member show's episodes
    into per-(season, episode) slots so the same SxxEyy across accounts unifies.

    `episodes` are matched to their owning show by (server_id, grandparent_rating_key)
    — scoped per server because a ratingKey is only unique within one server."""
    episodes_by_show: dict[tuple[str, str], list[Media]] = {}
    for ep in episodes:
        episodes_by_show.setdefault((ep.server_id, ep.grandparent_rating_key or ""), []).append(ep)

    show_groups: dict[str, list[Media]] = {}
    for show in shows:
        show_groups.setdefault(group_key(show), []).append(show)
    show_groups = _converge(show_groups)

    result: list[SeriesGroup] = []
    for key, member_shows in show_groups.items():
        slots: dict[tuple[int, int], list[Media]] = {}
        for show in member_shows:
            for ep in episodes_by_show.get((show.server_id, show.rating_key), []):
                if not ep.parent_index or not ep.index:
                    continue
                slots.setdefault((ep.parent_index, ep.index), []).append(ep)

        episode_slots = [
            EpisodeSlot(season=s, episode=e, best=best_row(eps), members=eps)
            for (s, e), eps in slots.items()
        ]
        result.append(SeriesGroup(key=key, best=best_row(member_shows), slots=episode_slots))
    return result
