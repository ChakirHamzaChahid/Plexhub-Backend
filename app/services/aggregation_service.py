"""Shared media aggregation / dedup logic.

Groups raw `media` rows that represent the SAME title across accounts (and across
quality/language variants within one account) by `unification_id`. Used by both
the Plex/Jellyfin library generator (`plex_generator.source`) and the REST API
(`api/media` unified endpoints) so the two paths dedup identically.

Pure functions on `Media` rows — no DB, no I/O — so they're trivially testable.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.models.database import Media
from app.utils.string_normalizer import parse_title_year_and_suffix


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


def aggregate_movies(rows: list[Media]) -> list[MovieGroup]:
    """Group movie rows by unification key into one MovieGroup each."""
    groups: dict[str, list[Media]] = {}
    for row in rows:
        groups.setdefault(group_key(row), []).append(row)
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
