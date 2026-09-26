"""Tests for the pure multipart/payload parser."""

from __future__ import annotations

import json

import pytest

from custom_components.hikvision_acs.parser import (
    DEFAULT_BOUNDARY,
    MultipartStreamParser,
    ParseError,
    Part,
    PartKind,
    decode_part,
    extract_boundary,
    parse_device_info,
    parse_response_status,
    parse_xml,
)

from .helpers import frame, load_bytes

CARD = load_bytes("card_granted_replayed.json")
HEARTBEAT = load_bytes("synthetic_heartbeat.xml")


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("multipart/mixed; boundary=MIME_boundary", "MIME_boundary"),
        ('multipart/mixed; boundary="quoted"', "quoted"),
        ("multipart/mixed; charset=utf-8; BOUNDARY=other", "other"),
        ("multipart/mixed; boundary=", DEFAULT_BOUNDARY),
        ("multipart/mixed", DEFAULT_BOUNDARY),
        ("", DEFAULT_BOUNDARY),
        (None, DEFAULT_BOUNDARY),
    ],
)
def test_extract_boundary(header: str | None, expected: str) -> None:
    assert extract_boundary(header) == expected


class TestMultipartStreamParser:
    def test_single_part_with_content_length(self) -> None:
        parts = MultipartStreamParser().feed(frame(CARD))
        assert len(parts) == 1
        assert parts[0].body == CARD
        assert parts[0].content_type == "application/json"
        assert parts[0].headers["content-length"] == str(len(CARD))

    def test_byte_at_a_time(self) -> None:
        parser = MultipartStreamParser()
        stream = frame(CARD) + frame(HEARTBEAT, "application/xml; charset=UTF-8")
        parts: list[Part] = []
        for byte in stream:
            parts.extend(parser.feed(bytes([byte])))
        assert [p.body for p in parts] == [CARD, HEARTBEAT]

    def test_multiple_parts_in_one_chunk(self) -> None:
        parts = MultipartStreamParser().feed(frame(CARD) + frame(CARD) + frame(CARD))
        assert len(parts) == 3

    def test_part_without_content_length_waits_for_next_delimiter(self) -> None:
        parser = MultipartStreamParser()
        assert parser.feed(frame(CARD, with_length=False)) == []
        parts = parser.feed(b"--MIME_boundary\r\n")
        # Without a length the surrounding line breaks cannot be told apart
        # from the body, so they are stripped.
        assert [p.body for p in parts] == [CARD.strip()]

    def test_invalid_content_length_falls_back_to_delimiter_scan(self) -> None:
        raw = (
            b"--MIME_boundary\r\nContent-Type: application/json\r\nContent-Length: nope\r\n\r\n"
            + CARD
            + b"\r\n--MIME_boundary--\r\n"
        )
        assert [p.body for p in MultipartStreamParser().feed(raw)] == [CARD.strip()]

    def test_negative_content_length_is_ignored(self) -> None:
        raw = b"--MIME_boundary\r\nContent-Length: -5\r\n\r\n{}\r\n--MIME_boundary\r\n"
        assert [p.body for p in MultipartStreamParser().feed(raw)] == [b"{}"]

    def test_binary_part_containing_delimiter_bytes(self) -> None:
        jpeg = b"\xff\xd8\xff\xe0--MIME_boundary\r\n\r\n\x00\x01\xff\xd9"
        parts = MultipartStreamParser().feed(frame(jpeg, "image/jpeg") + frame(CARD))
        assert [p.body for p in parts] == [jpeg, CARD]

    def test_garbage_before_first_delimiter_is_skipped(self) -> None:
        parts = MultipartStreamParser().feed(b"\r\nnoise noise\r\n" + frame(CARD))
        assert [p.body for p in parts] == [CARD]

    def test_split_delimiter_across_chunks(self) -> None:
        parser = MultipartStreamParser()
        data = b"junk" * 10 + frame(CARD)
        cut = 40 + 5  # inside "--MIME_boundary"
        assert parser.feed(data[:cut]) == []
        assert [p.body for p in parser.feed(data[cut:])] == [CARD]

    def test_closing_delimiter_ends_stream(self) -> None:
        parser = MultipartStreamParser()
        assert parser.feed(b"--MIME_boundary--\r\n") == []
        assert parser.feed(frame(CARD)) != []  # parser keeps working afterwards

    def test_custom_boundary(self) -> None:
        parser = MultipartStreamParser(boundary="xyz")
        assert [p.body for p in parser.feed(frame(CARD, boundary="xyz"))] == [CARD]

    def test_headers_without_colon_are_ignored(self) -> None:
        raw = b"--MIME_boundary\r\nbogus line\r\nContent-Length: 2\r\n\r\n{}"
        (part,) = MultipartStreamParser().feed(raw)
        assert dict(part.headers) == {"content-length": "2"}
        assert part.content_type == ""

    def test_oversized_incomplete_part_is_discarded(self) -> None:
        parser = MultipartStreamParser(max_part_size=64)
        header = b"--MIME_boundary\r\nContent-Length: 1000\r\n\r\n"
        assert parser.feed(header + b"x" * 100) == []
        # After the reset the parser resynchronises on the next part.
        assert [p.body for p in parser.feed(frame(b"{}"))] == [b"{}"]


class TestDecodePart:
    def test_json(self) -> None:
        decoded = decode_part(Part({"content-type": "application/json"}, CARD))
        assert decoded.kind is PartKind.JSON
        assert decoded.payload == json.loads(CARD)

    def test_xml(self) -> None:
        decoded = decode_part(Part({"content-type": "application/xml"}, HEARTBEAT))
        assert decoded.kind is PartKind.XML
        assert decoded.payload is not None
        assert decoded.payload["_root"] == "EventNotificationAlert"
        assert decoded.payload["eventType"] == "videoloss"

    def test_jpeg_is_binary_without_payload(self) -> None:
        decoded = decode_part(Part({"content-type": "image/jpeg"}, b"\xff\xd8\xff"))
        assert decoded.kind is PartKind.BINARY
        assert decoded.payload is None

    @pytest.mark.parametrize(
        ("body", "kind"),
        [(b'  {"a": 1}', PartKind.JSON), (b"<a>1</a>", PartKind.XML), (b"hello", PartKind.UNKNOWN)],
    )
    def test_sniffs_type_when_header_missing(self, body: bytes, kind: PartKind) -> None:
        assert decode_part(Part({}, body)).kind is kind

    @pytest.mark.parametrize("body", [b"{not json", b"[1, 2]"])
    def test_bad_json_yields_no_payload(self, body: bytes) -> None:
        decoded = decode_part(Part({"content-type": "application/json"}, body))
        assert decoded.kind is PartKind.JSON
        assert decoded.payload is None

    def test_bad_xml_yields_no_payload(self) -> None:
        decoded = decode_part(Part({"content-type": "text/xml"}, b"<unclosed>"))
        assert decoded.payload is None


class TestXml:
    def test_repeated_elements_become_lists(self) -> None:
        doc = parse_xml(b"<r><a>1</a><a>2</a><a>3</a><b><c>x</c></b></r>")
        assert doc == {"_root": "r", "a": ["1", "2", "3"], "b": {"c": "x"}}

    def test_leaf_root(self) -> None:
        assert parse_xml(b"<r> text </r>") == {"_root": "r", "_text": "text"}

    def test_invalid(self) -> None:
        with pytest.raises(ParseError):
            parse_xml(b"not xml")


class TestDeviceInfo:
    def test_parses_fixture(self) -> None:
        info = parse_device_info(load_bytes("synthetic_device_info.xml"))
        assert info.serial_number == "DS-K1T805MBFWX20240101AAWRTEST0001"
        assert info.model == "DS-K1T805MBFWX"
        assert info.firmware_version == "V1.9.1"
        assert info.firmware_build == "build 240909"
        assert info.mac_address == "00:00:5e:00:53:01"
        assert info.device_type == "ACS"
        assert info.name == "Access Control Device"

    def test_wrong_document(self) -> None:
        with pytest.raises(ParseError, match="expected DeviceInfo"):
            parse_device_info(b"<ResponseStatus/>")

    def test_missing_serial(self) -> None:
        with pytest.raises(ParseError, match="serialNumber"):
            parse_device_info(b"<DeviceInfo><model>X</model><serialNumber/></DeviceInfo>")


class TestResponseStatus:
    def test_ok_xml(self) -> None:
        status = parse_response_status(load_bytes("synthetic_response_ok.xml"))
        assert status.ok
        assert status.status_string == "OK"

    def test_error_xml(self) -> None:
        status = parse_response_status(load_bytes("synthetic_response_error.xml"))
        assert not status.ok
        assert status.status_code == 4
        assert status.sub_status_code == "notSupport"

    def test_real_not_supported_capture(self) -> None:
        status = parse_response_status(load_bytes("security_users_capabilities_not_supported.xml"))
        assert not status.ok
        assert status.status_code == 4
        assert status.status_string == "Invalid Operation"
        assert status.sub_status_code == "notSupport"

    def test_json(self) -> None:
        status = parse_response_status(b'{"statusCode": 1, "statusString": "OK"}')
        assert status.ok

    @pytest.mark.parametrize("body", [b"{broken", b"[1]"])
    def test_bad_json(self, body: bytes) -> None:
        with pytest.raises(ParseError):
            parse_response_status(body)

    @pytest.mark.parametrize(
        "body",
        [b"<ResponseStatus><statusCode>x</statusCode></ResponseStatus>", b"<ResponseStatus/>"],
    )
    def test_missing_or_bad_code(self, body: bytes) -> None:
        assert parse_response_status(body).status_code is None
