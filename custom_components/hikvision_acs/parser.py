"""Multipart alertStream parsing and ISAPI payload decoding.

Pure module: no aiohttp, no Home Assistant imports. It takes bytes and returns
plain data, which keeps it trivially testable against captured fixtures.

The alertStream is a never-ending ``multipart/mixed`` response. Each part looks
roughly like this::

    --MIME_boundary
    Content-Type: application/json; charset="UTF-8"
    Content-Length: 1024

    {...}

Firmware differs in the details: some omit ``Content-Length``, some put the
body on the line right after the headers, and parts can be JSON, XML or JPEG.
The parser uses ``Content-Length`` when present and falls back to scanning for
the next boundary otherwise.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

DEFAULT_BOUNDARY = "MIME_boundary"

# A part that grows beyond this without completing is treated as garbage and
# discarded, so a misbehaving device cannot make us buffer unbounded memory.
MAX_PART_SIZE = 4 * 1024 * 1024

_HEADER_END = b"\r\n\r\n"


class PartKind(StrEnum):
    """What a multipart body turned out to contain."""

    JSON = "json"
    XML = "xml"
    BINARY = "binary"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Part:
    """One raw multipart part: lower-cased headers plus the undecoded body."""

    headers: Mapping[str, str]
    body: bytes

    @property
    def content_type(self) -> str:
        """Media type without parameters, lower-cased (``""`` if absent)."""
        return self.headers.get("content-type", "").split(";", 1)[0].strip().lower()


@dataclass(frozen=True, slots=True)
class DecodedPart:
    """A part after body decoding.

    ``payload`` is the parsed document for JSON and XML parts and ``None`` for
    anything else (JPEG snapshots, undecodable bodies).
    """

    kind: PartKind
    payload: Mapping[str, Any] | None
    content_type: str


@dataclass(frozen=True, slots=True)
class DeviceIdentity:
    """The subset of ``/ISAPI/System/deviceInfo`` the integration needs."""

    serial_number: str
    model: str | None = None
    name: str | None = None
    firmware_version: str | None = None
    firmware_build: str | None = None
    mac_address: str | None = None
    device_type: str | None = None


@dataclass(frozen=True, slots=True)
class ResponseStatus:
    """An ISAPI ``<ResponseStatus>`` document."""

    status_code: int | None
    status_string: str | None = None
    sub_status_code: str | None = None

    @property
    def ok(self) -> bool:
        """ISAPI uses status code 1 for success."""
        return self.status_code == 1


class ParseError(ValueError):
    """A payload could not be parsed into the expected structure."""


def extract_boundary(content_type: str | None) -> str:
    """Return the multipart boundary from a ``Content-Type`` header value.

    Falls back to Hikvision's usual ``MIME_boundary`` when the header is
    missing or carries no boundary parameter.
    """
    if not content_type:
        return DEFAULT_BOUNDARY
    for param in content_type.split(";")[1:]:
        key, _, value = param.strip().partition("=")
        if key.strip().lower() == "boundary":
            boundary = value.strip().strip('"')
            if boundary:
                return boundary
    return DEFAULT_BOUNDARY


def _parse_headers(block: bytes) -> dict[str, str]:
    headers: dict[str, str] = {}
    for line in block.decode("latin-1").split("\r\n"):
        name, sep, value = line.partition(":")
        if sep and name.strip():
            headers[name.strip().lower()] = value.strip()
    return headers


def _content_length(headers: Mapping[str, str]) -> int | None:
    raw = headers.get("content-length")
    if raw is None:
        return None
    try:
        length = int(raw)
    except ValueError:
        return None
    return length if length >= 0 else None


@dataclass(slots=True)
class MultipartStreamParser:
    """Incremental parser for an endless multipart stream.

    Feed it chunks as they arrive from the network; it returns every part that
    became complete. Chunk boundaries can fall anywhere, including inside a
    delimiter or header block.
    """

    boundary: str = DEFAULT_BOUNDARY
    max_part_size: int = MAX_PART_SIZE
    _buffer: bytearray = field(default_factory=bytearray, init=False, repr=False)

    @property
    def _delimiter(self) -> bytes:
        return b"--" + self.boundary.encode("latin-1")

    def feed(self, chunk: bytes) -> list[Part]:
        """Consume a chunk and return the parts it completed, in order."""
        self._buffer.extend(chunk)
        parts = list(self._drain())
        if len(self._buffer) > self.max_part_size:
            # Oversized or corrupt data: resynchronise on the next delimiter.
            self._buffer.clear()
        return parts

    def _drain(self) -> Iterator[Part]:
        while (part := self._next_part()) is not None:
            yield part

    def _next_part(self) -> Part | None:
        delimiter = self._delimiter
        start = self._buffer.find(delimiter)
        if start == -1:
            # Keep a tail that may hold the beginning of a split delimiter.
            keep = len(delimiter) - 1
            if len(self._buffer) > keep:
                del self._buffer[: len(self._buffer) - keep]
            return None
        if start:
            del self._buffer[:start]

        header_start = len(delimiter)
        # "--boundary--" closes the stream; nothing more will follow.
        if self._buffer[header_start : header_start + 2] == b"--":
            self._buffer.clear()
            return None

        header_end = self._buffer.find(_HEADER_END, header_start)
        if header_end == -1:
            return None
        headers = _parse_headers(bytes(self._buffer[header_start:header_end]).strip(b"\r\n"))
        body_start = header_end + len(_HEADER_END)

        length = _content_length(headers)
        if length is not None:
            body_end = body_start + length
            if len(self._buffer) < body_end:
                return None
            body = bytes(self._buffer[body_start:body_end])
            del self._buffer[:body_end]
        else:
            next_delimiter = self._buffer.find(delimiter, body_start)
            if next_delimiter == -1:
                return None
            body = bytes(self._buffer[body_start:next_delimiter]).strip(b"\r\n")
            del self._buffer[:next_delimiter]

        return Part(headers=headers, body=body)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _element_to_value(element: ET.Element) -> Any:
    children = list(element)
    if not children:
        return (element.text or "").strip()
    result: dict[str, Any] = {}
    for child in children:
        key = _local_name(child.tag)
        value = _element_to_value(child)
        if key in result:
            existing = result[key]
            result[key] = [*existing, value] if isinstance(existing, list) else [existing, value]
        else:
            result[key] = value
    return result


def parse_xml(body: bytes) -> dict[str, Any]:
    """Parse an ISAPI XML document into nested dicts, namespaces stripped.

    Leaf elements become strings. The root element name is returned under the
    ``"_root"`` key so callers can tell document types apart.

    Raises :class:`ParseError` on malformed XML.
    """
    try:
        root = ET.fromstring(body)
    except ET.ParseError as err:
        raise ParseError(f"invalid XML: {err}") from err
    value = _element_to_value(root)
    document: dict[str, Any] = value if isinstance(value, dict) else {"_text": value}
    return {"_root": _local_name(root.tag), **document}


def _sniff_kind(content_type: str, body: bytes) -> PartKind:
    if "json" in content_type:
        return PartKind.JSON
    if "xml" in content_type:
        return PartKind.XML
    if content_type.startswith(("image/", "video/", "application/octet-stream")):
        return PartKind.BINARY
    stripped = body.lstrip()
    if stripped.startswith(b"{"):
        return PartKind.JSON
    if stripped.startswith(b"<"):
        return PartKind.XML
    return PartKind.UNKNOWN


def decode_part(part: Part) -> DecodedPart:
    """Decode a part's body according to its declared (or sniffed) type.

    Never raises: undecodable bodies come back with ``payload=None`` so one
    bad part cannot break the event stream.
    """
    content_type = part.content_type
    kind = _sniff_kind(content_type, part.body)
    payload: Mapping[str, Any] | None = None

    if kind is PartKind.JSON:
        try:
            decoded = json.loads(part.body.decode("utf-8", errors="replace"))
        except ValueError:
            decoded = None
        payload = decoded if isinstance(decoded, dict) else None
    elif kind is PartKind.XML:
        try:
            payload = parse_xml(part.body)
        except ParseError:
            payload = None

    return DecodedPart(kind=kind, payload=payload, content_type=content_type)


def _optional_str(document: Mapping[str, Any], key: str) -> str | None:
    value = document.get(key)
    if isinstance(value, str) and value:
        return value
    return None


def parse_device_info(body: bytes) -> DeviceIdentity:
    """Parse a ``/ISAPI/System/deviceInfo`` response.

    Raises :class:`ParseError` when the document is not a ``DeviceInfo`` or
    lacks a serial number, which the integration needs for unique IDs.
    """
    document = parse_xml(body)
    if document.get("_root") != "DeviceInfo":
        raise ParseError(f"expected DeviceInfo, got {document.get('_root')}")
    serial = _optional_str(document, "serialNumber")
    if serial is None:
        raise ParseError("DeviceInfo has no serialNumber")
    return DeviceIdentity(
        serial_number=serial,
        model=_optional_str(document, "model"),
        name=_optional_str(document, "deviceName"),
        firmware_version=_optional_str(document, "firmwareVersion"),
        firmware_build=_optional_str(document, "firmwareReleasedDate"),
        mac_address=_optional_str(document, "macAddress"),
        device_type=_optional_str(document, "deviceType"),
    )


def parse_response_status(body: bytes) -> ResponseStatus:
    """Parse an ISAPI ``ResponseStatus`` document (XML or JSON flavour).

    Raises :class:`ParseError` if the body is neither.
    """
    stripped = body.lstrip()
    document: Mapping[str, Any]
    if stripped.startswith(b"{"):
        try:
            decoded = json.loads(stripped)
        except ValueError as err:
            raise ParseError(f"invalid JSON: {err}") from err
        if not isinstance(decoded, dict):
            raise ParseError("ResponseStatus JSON is not an object")
        document = decoded
    else:
        document = parse_xml(body)

    raw_code = document.get("statusCode")
    try:
        status_code = int(raw_code) if raw_code is not None else None
    except TypeError, ValueError:
        status_code = None
    return ResponseStatus(
        status_code=status_code,
        status_string=_optional_str(document, "statusString"),
        sub_status_code=_optional_str(document, "subStatusCode"),
    )
