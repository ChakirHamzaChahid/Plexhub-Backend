"""`extract_youtube_id` (Lot A trailers, `app/utils/youtube.py`) — pure,
no HTTP/DB. BB-1 code review: Xtream panels indifferently emit a bare
video id or a full URL of several shapes; every shape below MUST resolve
to the same bare id."""
from __future__ import annotations

import pytest

from app.utils.youtube import YOUTUBE_ID_RE, extract_youtube_id

ID = "dQw4w9WgXcQ"


class TestExtractYoutubeId:
    def test_bare_id_passthrough(self):
        assert extract_youtube_id(ID) == ID

    def test_watch_url(self):
        assert extract_youtube_id(f"https://www.youtube.com/watch?v={ID}") == ID

    def test_watch_url_http_no_www(self):
        assert extract_youtube_id(f"http://youtube.com/watch?v={ID}") == ID

    def test_watch_url_with_extra_trailing_params(self):
        assert extract_youtube_id(f"https://www.youtube.com/watch?v={ID}&t=42s") == ID

    def test_watch_url_with_leading_params(self):
        assert extract_youtube_id(f"https://www.youtube.com/watch?list=PL123&v={ID}&index=3") == ID

    def test_youtu_be_short_link(self):
        assert extract_youtube_id(f"https://youtu.be/{ID}") == ID

    def test_youtu_be_short_link_with_query(self):
        assert extract_youtube_id(f"https://youtu.be/{ID}?t=5") == ID

    def test_embed_url(self):
        assert extract_youtube_id(f"https://www.youtube.com/embed/{ID}") == ID

    def test_legacy_v_url(self):
        assert extract_youtube_id(f"https://www.youtube.com/v/{ID}") == ID

    def test_shorts_url(self):
        assert extract_youtube_id(f"https://www.youtube.com/shorts/{ID}") == ID

    def test_mobile_host_watch_url(self):
        assert extract_youtube_id(f"https://m.youtube.com/watch?v={ID}") == ID

    def test_music_host_watch_url(self):
        assert extract_youtube_id(f"https://music.youtube.com/watch?v={ID}") == ID

    def test_whitespace_stripped(self):
        assert extract_youtube_id(f"  {ID}  ") == ID

    @pytest.mark.parametrize("value", [
        None, "", "   ", "too_short", "way_too_long_id_here",
        "not a url at all", 12345, [], {},
        "https://vimeo.com/123456",
        "https://www.youtube.com/channel/UC123456789012345678901",
    ])
    def test_unrecognisable_input_returns_none(self, value):
        assert extract_youtube_id(value) is None

    def test_youtube_id_re_matches_only_bare_ids(self):
        assert YOUTUBE_ID_RE.match(ID)
        assert not YOUTUBE_ID_RE.match(f"https://youtu.be/{ID}")
