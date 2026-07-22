"""Tests for app/dav/propfind.py — WebDAV multistatus XML rendering."""
import xml.etree.ElementTree as ET

from app.dav.propfind import content_type_for, http_date, render_multistatus
from app.dav.vfs import DAV_STABLE_MTIME, DavEntry

NS = "{DAV:}"


class TestHttpDate:
    def test_stable_mtime_format(self):
        assert http_date(DAV_STABLE_MTIME) == "Mon, 01 Jan 2024 00:00:00 GMT"

    def test_epoch_zero(self):
        assert http_date(0) == "Thu, 01 Jan 1970 00:00:00 GMT"

    def test_arbitrary_epoch(self):
        # 2020-07-04T12:34:56Z
        assert http_date(1593866096) == "Sat, 04 Jul 2020 12:34:56 GMT"


class TestContentTypeFor:
    def test_mkv(self):
        assert content_type_for("Movie.mkv") == "video/x-matroska"

    def test_mp4(self):
        assert content_type_for("Movie.mp4") == "video/mp4"

    def test_avi(self):
        assert content_type_for("Movie.avi") == "video/x-msvideo"

    def test_ts(self):
        assert content_type_for("Movie.ts") == "video/mp2t"

    def test_m3u8(self):
        assert content_type_for("stream.m3u8") == "application/vnd.apple.mpegurl"

    def test_unknown_extension_defaults_to_octet_stream(self):
        assert content_type_for("Movie.xyz") == "application/octet-stream"

    def test_no_extension_defaults_to_octet_stream(self):
        assert content_type_for("Movie") == "application/octet-stream"

    def test_case_insensitive(self):
        assert content_type_for("Movie.MKV") == "video/x-matroska"
        assert content_type_for("Movie.Mp4") == "video/mp4"

    def test_multiple_dots_uses_last_extension(self):
        assert content_type_for("Dune (2021).mkv") == "video/x-matroska"


class TestRenderMultistatus:
    def _parse(self, xml_bytes: bytes) -> ET.Element:
        return ET.fromstring(xml_bytes)

    def test_file_entry_full_props(self):
        entry = DavEntry(
            name="Dune (2021).mkv", is_dir=False, size=123456789,
            server_id="xtream_a", rating_key="vod_1.mkv",
        )
        xml = render_multistatus("/dav", [("Films/Dune (2021)/Dune (2021).mkv", entry)])
        root = self._parse(xml)

        responses = root.findall(f"{NS}response")
        assert len(responses) == 1
        response = responses[0]

        href = response.find(f"{NS}href").text
        assert href == "/dav/Films/Dune%20%282021%29/Dune%20%282021%29.mkv"

        prop = response.find(f"{NS}propstat/{NS}prop")
        assert prop.find(f"{NS}displayname").text == "Dune (2021).mkv"

        resourcetype = prop.find(f"{NS}resourcetype")
        assert resourcetype is not None
        assert list(resourcetype) == []  # empty element for a file

        assert prop.find(f"{NS}getcontentlength").text == "123456789"
        assert prop.find(f"{NS}getcontenttype").text == "video/x-matroska"
        assert prop.find(f"{NS}getlastmodified").text == http_date(entry.mtime)

        status = response.find(f"{NS}propstat/{NS}status").text
        assert status == "HTTP/1.1 200 OK"

    def test_directory_entry(self):
        entry = DavEntry(name="Films", is_dir=True)
        xml = render_multistatus("/dav", [("Films", entry)])
        root = self._parse(xml)

        response = root.find(f"{NS}response")
        href = response.find(f"{NS}href").text
        assert href == "/dav/Films/"

        prop = response.find(f"{NS}propstat/{NS}prop")
        assert prop.find(f"{NS}displayname").text == "Films"

        resourcetype = prop.find(f"{NS}resourcetype")
        assert resourcetype.find(f"{NS}collection") is not None

        # No content length/type for a directory.
        assert prop.find(f"{NS}getcontentlength") is None
        assert prop.find(f"{NS}getcontenttype") is None
        # getlastmodified IS emitted for directories too.
        assert prop.find(f"{NS}getlastmodified") is not None

        status = response.find(f"{NS}propstat/{NS}status").text
        assert status == "HTTP/1.1 200 OK"

    def test_root_directory_href_has_trailing_slash(self):
        entry = DavEntry(name="", is_dir=True)
        xml = render_multistatus("/dav", [("", entry)])
        root = self._parse(xml)
        href = root.find(f"{NS}response/{NS}href").text
        assert href == "/dav/"

    def test_hrefs_percent_encoded_for_spaces_and_accents(self):
        entry = DavEntry(name="Les Misérables (1998).mkv", is_dir=False, size=1)
        xml = render_multistatus(
            "/dav",
            [("Films/Les Misérables (1998)/Les Misérables (1998).mkv", entry)],
        )
        root = self._parse(xml)
        href = root.find(f"{NS}response/{NS}href").text
        assert " " not in href
        assert "é" not in href
        assert "%20" in href
        assert "%C3%A9" in href  # UTF-8 percent-encoding of "é"

    def test_href_slash_not_percent_encoded(self):
        entry = DavEntry(name="Dune (2021).mkv", is_dir=False, size=1)
        xml = render_multistatus("/dav", [("Films/Dune (2021)/Dune (2021).mkv", entry)])
        root = self._parse(xml)
        href = root.find(f"{NS}response/{NS}href").text
        # "/dav" + "/Films" + "/Dune (2021)" + "/Dune (2021).mkv" = 4 separators.
        assert href.count("/") == 4
        assert href == "/dav/Films/Dune%20%282021%29/Dune%20%282021%29.mkv"

    def test_multiple_items_produce_multiple_responses(self):
        items = [
            ("Films", DavEntry(name="Films", is_dir=True)),
            ("Films/Dune (2021)", DavEntry(name="Dune (2021)", is_dir=True)),
            (
                "Films/Dune (2021)/Dune (2021).mkv",
                DavEntry(name="Dune (2021).mkv", is_dir=False, size=10),
            ),
        ]
        xml = render_multistatus("/dav", items)
        root = self._parse(xml)
        assert len(root.findall(f"{NS}response")) == 3

    def test_empty_items_yields_empty_multistatus(self):
        xml = render_multistatus("/dav", [])
        root = self._parse(xml)
        assert root.tag == f"{NS}multistatus"
        assert root.findall(f"{NS}response") == []

    def test_file_size_none_renders_zero(self):
        entry = DavEntry(name="unsized.ts", is_dir=False, size=None)
        xml = render_multistatus("/dav", [("unsized.ts", entry)])
        root = self._parse(xml)
        prop = root.find(f"{NS}response/{NS}propstat/{NS}prop")
        assert prop.find(f"{NS}getcontentlength").text == "0"

    def test_xml_declaration_present_and_utf8(self):
        entry = DavEntry(name="a.mkv", is_dir=False, size=1)
        xml = render_multistatus("/dav", [("a.mkv", entry)])
        assert xml.startswith(b"<?xml")
        assert b"utf-8" in xml.lower()

    def test_namespace_prefix_is_d(self):
        entry = DavEntry(name="a.mkv", is_dir=False, size=1)
        xml = render_multistatus("/dav", [("a.mkv", entry)])
        text = xml.decode("utf-8")
        assert "<D:multistatus" in text
        assert "<D:response>" in text
