"""Renders WebDAV `207 Multi-Status` PROPFIND responses from `DavEntry`
nodes (see `app/dav/vfs.py`). The DAV router (`app/api/dav.py`, out of this
ticket's scope) decides WHICH entries go into a response (Depth 0 = the
resource itself, Depth 1 = the resource + its direct children) — this module
only knows how to serialize a flat list of `(rel_path, DavEntry)` pairs.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from email.utils import formatdate
from urllib.parse import quote

from app.dav.vfs import DavEntry

_DAV_NS = "DAV:"
# Register the "D:" prefix (rclone/most WebDAV clients expect it) instead of
# ElementTree's default auto-generated "ns0:" prefix.
ET.register_namespace("D", _DAV_NS)

_CONTENT_TYPES: dict[str, str] = {
    "mkv": "video/x-matroska",
    "mp4": "video/mp4",
    "avi": "video/x-msvideo",
    "ts": "video/mp2t",
    "m3u8": "application/vnd.apple.mpegurl",
}
_DEFAULT_CONTENT_TYPE = "application/octet-stream"


def http_date(epoch: int) -> str:
    """RFC1123 date string for an HTTP header, e.g.
    "Mon, 01 Jan 2024 00:00:00 GMT" for `DAV_STABLE_MTIME`."""
    return formatdate(epoch, usegmt=True)


def content_type_for(name: str) -> str:
    """Best-effort `Content-Type` for a file name, by extension.
    Case-insensitive; unknown/missing extension -> `application/octet-stream`
    (never guessed further — rclone/Plex only need *a* value to pass through,
    the actual codec is whatever the upstream stream really is)."""
    if "." not in name:
        return _DEFAULT_CONTENT_TYPE
    ext = name.rsplit(".", 1)[-1].lower()
    return _CONTENT_TYPES.get(ext, _DEFAULT_CONTENT_TYPE)


def _tag(local_name: str) -> str:
    return f"{{{_DAV_NS}}}{local_name}"


def _build_href(base_href: str, rel_path: str, is_dir: bool) -> str:
    base = base_href.rstrip("/")
    encoded = quote(rel_path.strip("/"))
    href = f"{base}/{encoded}" if encoded else f"{base}/"
    if is_dir and not href.endswith("/"):
        href += "/"
    return href


def render_multistatus(base_href: str, items: list[tuple[str, DavEntry]]) -> bytes:
    """Render a WebDAV `207 Multi-Status` PROPFIND response body.

    `items` is a list of `(rel_path, entry)` pairs to describe — the caller
    (the DAV router) decides which entries belong in the response, this
    function just serializes them. `base_href` is the mount-relative URL
    prefix (e.g. "/dav") prepended to every percent-encoded resource path;
    directory hrefs always end in "/".

    Props emitted per response: `displayname`, `resourcetype` (`<D:
    collection/>` for a directory, empty for a file), `getcontentlength` +
    `getcontenttype` (files only), `getlastmodified` (all entries, RFC1123).
    Every `propstat` reports `status: HTTP/1.1 200 OK` — this renderer is
    only ever called with entries that actually resolved.
    """
    root = ET.Element(_tag("multistatus"))
    for rel_path, entry in items:
        response = ET.SubElement(root, _tag("response"))
        href = ET.SubElement(response, _tag("href"))
        href.text = _build_href(base_href, rel_path, entry.is_dir)

        propstat = ET.SubElement(response, _tag("propstat"))
        prop = ET.SubElement(propstat, _tag("prop"))

        displayname = ET.SubElement(prop, _tag("displayname"))
        displayname.text = entry.name

        resourcetype = ET.SubElement(prop, _tag("resourcetype"))
        if entry.is_dir:
            ET.SubElement(resourcetype, _tag("collection"))

        if not entry.is_dir:
            getcontentlength = ET.SubElement(prop, _tag("getcontentlength"))
            getcontentlength.text = str(entry.size or 0)
            getcontenttype = ET.SubElement(prop, _tag("getcontenttype"))
            getcontenttype.text = content_type_for(entry.name)

        getlastmodified = ET.SubElement(prop, _tag("getlastmodified"))
        getlastmodified.text = http_date(entry.mtime)

        status = ET.SubElement(propstat, _tag("status"))
        status.text = "HTTP/1.1 200 OK"

    return ET.tostring(root, encoding="utf-8", xml_declaration=True)
