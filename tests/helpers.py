"""Shared test helpers: fixture loading, multipart framing and a fake client."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

from custom_components.hikvision_acs.parser import (
    DecodedPart,
    DeviceIdentity,
    PartKind,
    parse_device_info,
)

FIXTURES = Path(__file__).parent / "fixtures"
BOUNDARY = "MIME_boundary"


def load_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def load_json(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(load_bytes(name))
    return data


def frame(
    body: bytes,
    content_type: str = 'application/json; charset="UTF-8"',
    *,
    with_length: bool = True,
    boundary: str = BOUNDARY,
) -> bytes:
    """Wrap a body in one multipart part the way Hikvision firmware does."""
    headers = f"--{boundary}\r\nContent-Type: {content_type}\r\n"
    if with_length:
        headers += f"Content-Length: {len(body)}\r\n"
    return headers.encode() + b"\r\n" + body + b"\r\n"


def access_payload(
    major: int = 5,
    minor: int = 38,
    *,
    live: bool = True,
    serial: int | None = 6000,
    **overrides: Any,
) -> dict[str, Any]:
    """The real card-read capture with selected fields replaced."""
    payload = load_json("card_granted_replayed.json")
    body = {
        **payload["AccessControllerEvent"],
        "majorEventType": major,
        "subEventType": minor,
        "currentEvent": live,
        **overrides,
    }
    if serial is None:
        body.pop("serialNo", None)
    else:
        body["serialNo"] = serial
    return {**payload, "AccessControllerEvent": body}


def json_part(payload: dict[str, Any]) -> DecodedPart:
    return DecodedPart(kind=PartKind.JSON, payload=payload, content_type="application/json")


DEVICE: DeviceIdentity = parse_device_info(load_bytes("synthetic_device_info.xml"))

END_OF_STREAM = object()


class FakeClient:
    """Stands in for HikvisionClient; the stream is driven through a queue.

    Put DecodedPart objects on ``queue`` to emit them, an exception instance
    to raise it from the stream, or END_OF_STREAM to end the stream cleanly.
    Each call to ``stream()`` consumes from the same queue, so a test can
    script a sequence of connections.
    """

    def __init__(self, device: DeviceIdentity = DEVICE) -> None:
        self.base_url = "https://192.0.2.10:443"
        self.get_device_info = AsyncMock(return_value=device)
        self.open_door = AsyncMock(return_value=None)
        self.queue: asyncio.Queue[object] = asyncio.Queue()
        self.auth_failed = False
        self.connect_count = 0
        self.refuse_connect: list[BaseException] = []

    async def stream(
        self, on_connect: Callable[[], None] | None = None
    ) -> AsyncIterator[DecodedPart]:
        if self.refuse_connect:
            raise self.refuse_connect.pop(0)
        self.connect_count += 1
        if on_connect is not None:
            on_connect()
        while True:
            item = await self.queue.get()
            if item is END_OF_STREAM:
                return
            if isinstance(item, BaseException):
                raise item
            assert isinstance(item, DecodedPart)
            yield item
