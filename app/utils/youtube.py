"""YouTube video-id extraction (Lot A trailers).

Xtream panels return `youtube_trailer` in whatever shape the upstream
scraper captured it in — a bare 11-char video id, a full
`youtube.com/watch?v=...` URL, a `youtu.be/...` short link, or an
`/embed/`/`/v/`/`/shorts/` path. Storing/reading that value RAW (assuming
"bare id") makes `trailer_service`'s strict 11-char check silently reject
every URL-shaped value — the sync capture becomes inert for providers that
emit URLs (found in code review, BB-1). `extract_youtube_id` normalizes ALL
of these shapes to the bare id `trailer_service`/the file cache key
requires, mirroring the Android `YoutubeTrailerLink` parser so both sides
agree on the same input shapes.
"""
from __future__ import annotations

import re

# Bare YouTube video id shape (11 base64url-ish chars: A-Z a-z 0-9 - _).
# Also the exact `cache_key` shape `GET /api/media/trailer/file/{cache_key}`
# accepts (trailer_service.is_valid_youtube_id).
YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

# Search patterns for the id embedded in a URL — deliberately NOT anchored to
# a specific host (youtube.com/youtu.be/m.youtube.com/music.youtube.com all
# occur in the wild) so a `.search()` against the raw string is enough; each
# captures exactly 11 id chars, so trailing query params/fragments never leak
# into the result.
_URL_ID_PATTERNS = (
    re.compile(r"[?&]v=([A-Za-z0-9_-]{11})"),       # .../watch?v=ID (any param order)
    re.compile(r"youtu\.be/([A-Za-z0-9_-]{11})"),   # youtu.be/ID short link
    re.compile(r"/embed/([A-Za-z0-9_-]{11})"),      # .../embed/ID
    re.compile(r"/v/([A-Za-z0-9_-]{11})"),          # .../v/ID (legacy)
    re.compile(r"/shorts/([A-Za-z0-9_-]{11})"),     # .../shorts/ID
)


def extract_youtube_id(value: object) -> str | None:
    """Normalize a provider-supplied trailer value to a bare 11-char YouTube
    video id, or `None` if it isn't recognisable as one.

    Accepts: a bare id, a `watch?v=` URL (any host/param order/extra
    params), `youtu.be/`, `/embed/`, `/v/`, `/shorts/` links. Non-string /
    blank / unrecognisable input returns `None` (never raises — this runs
    on untrusted third-party provider data at sync time).
    """
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    if YOUTUBE_ID_RE.match(value):
        return value
    for pattern in _URL_ID_PATTERNS:
        m = pattern.search(value)
        if m:
            return m.group(1)
    return None
