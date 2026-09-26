"""Tests for the ISAPI client against a local fake device with Digest auth."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from unittest.mock import MagicMock

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from custom_components.hikvision_acs.api import (
    HikvisionAuthError,
    HikvisionClient,
    HikvisionConnectionError,
    HikvisionPermissionError,
    HikvisionResponseError,
)
from custom_components.hikvision_acs.parser import DecodedPart, PartKind

from .helpers import frame, load_bytes

# The HA test plugin blocks sockets by default; these tests use a real local
# server on 127.0.0.1 on purpose.
pytestmark = pytest.mark.usefixtures("socket_enabled")

USER = "ha-integration"
PASSWORD = "test-password"
REALM = "DS-TEST"
NONCE = "c2VjcmV0bm9uY2U="


def _md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()  # Digest auth is MD5 by definition


def _digest_ok(request: web.Request) -> bool:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Digest "):
        return False
    params: dict[str, str] = {}
    for item in header[len("Digest ") :].split(","):
        key, _, value = item.strip().partition("=")
        params[key] = value.strip('"')
    ha1 = _md5(f"{USER}:{REALM}:{PASSWORD}")
    ha2 = _md5(f"{request.method}:{params.get('uri', '')}")
    expected = _md5(
        f"{ha1}:{params.get('nonce')}:{params.get('nc')}:{params.get('cnonce')}:"
        f"{params.get('qop')}:{ha2}"
    )
    return params.get("username") == USER and params.get("response") == expected


def _challenge() -> web.Response:
    return web.Response(
        status=401,
        headers={
            "WWW-Authenticate": f'Digest qop="auth", realm="{REALM}", nonce="{NONCE}", '
            'stale="FALSE", algorithm="MD5"'
        },
    )


@dataclass
class FakeDevice:
    """Configurable behaviour of the fake ISAPI endpoints."""

    device_info: bytes = field(default_factory=lambda: load_bytes("synthetic_device_info.xml"))
    door_response: bytes = field(default_factory=lambda: load_bytes("synthetic_response_ok.xml"))
    status_override: int | None = None
    delay: float = 0.0
    stream_chunks: list[bytes] = field(default_factory=list)
    stream_idle: float = 0.0
    door_requests: list[tuple[str, bytes]] = field(default_factory=list)
    unauthenticated_requests: int = 0

    def app(self) -> web.Application:
        @web.middleware
        async def auth(
            request: web.Request, handler: Callable[[web.Request], object]
        ) -> web.StreamResponse:
            if not _digest_ok(request):
                self.unauthenticated_requests += 1
                return _challenge()
            if self.status_override is not None:
                return web.Response(status=self.status_override)
            if self.delay:
                await asyncio.sleep(self.delay)
            response = await handler(request)  # type: ignore[misc]
            assert isinstance(response, web.StreamResponse)
            return response

        app = web.Application(middlewares=[auth])
        app.router.add_get("/ISAPI/System/deviceInfo", self._device_info)
        app.router.add_put("/ISAPI/AccessControl/RemoteControl/door/{door}", self._door)
        app.router.add_get("/ISAPI/Event/notification/alertStream", self._stream)
        return app

    async def _device_info(self, _request: web.Request) -> web.Response:
        return web.Response(body=self.device_info, content_type="application/xml")

    async def _door(self, request: web.Request) -> web.Response:
        self.door_requests.append((request.match_info["door"], await request.read()))
        assert request.content_type == "application/xml"
        return web.Response(body=self.door_response, content_type="application/xml")

    async def _stream(self, request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(
            headers={"Content-Type": "multipart/mixed; boundary=MIME_boundary"}
        )
        await response.prepare(request)
        for chunk in self.stream_chunks:
            await response.write(chunk)
            await asyncio.sleep(0)
        if self.stream_idle:
            await asyncio.sleep(self.stream_idle)
        return response


@pytest.fixture
def device() -> FakeDevice:
    return FakeDevice()


@pytest.fixture
async def make_client(
    device: FakeDevice,
) -> AsyncIterator[Callable[..., HikvisionClient]]:
    server = TestServer(device.app())
    await server.start_server()
    session = aiohttp.ClientSession()

    def factory(password: str = PASSWORD, **kwargs: object) -> HikvisionClient:
        return HikvisionClient(
            session,
            host=server.host,
            port=server.port or 0,
            username=USER,
            password=password,
            ssl=False,
            **kwargs,  # type: ignore[arg-type]
        )

    yield factory
    await session.close()
    await server.close()


async def test_device_info(make_client: Callable[..., HikvisionClient]) -> None:
    client = make_client()
    info = await client.get_device_info()
    assert info.serial_number == "DS-K1T805MBFWX20240101AAWRTEST0001"
    assert client.base_url.scheme == "http"


async def test_digest_nonce_is_reused(
    make_client: Callable[..., HikvisionClient], device: FakeDevice
) -> None:
    client = make_client()
    await client.get_device_info()
    await client.get_device_info()
    # Only the very first request needed a challenge round trip.
    assert device.unauthenticated_requests == 1


async def test_wrong_password_is_not_retried(
    make_client: Callable[..., HikvisionClient], device: FakeDevice
) -> None:
    client = make_client(password="wrong")
    with pytest.raises(HikvisionAuthError):
        await client.get_device_info()
    # One challenge plus one rejected attempt, no retry loop.
    assert device.unauthenticated_requests == 2
    assert client.auth_failed

    # Every later call fails fast without touching the device.
    with pytest.raises(HikvisionAuthError):
        await client.open_door(1)
    with pytest.raises(HikvisionAuthError):
        await _collect(client, lambda: None)
    assert device.unauthenticated_requests == 2
    assert device.door_requests == []


@pytest.mark.parametrize(
    ("status", "error"),
    [(403, HikvisionPermissionError), (404, HikvisionResponseError), (500, HikvisionResponseError)],
)
async def test_http_errors(
    make_client: Callable[..., HikvisionClient],
    device: FakeDevice,
    status: int,
    error: type[Exception],
) -> None:
    device.status_override = status
    with pytest.raises(error):
        await make_client().get_device_info()


async def test_non_device_info_response(
    make_client: Callable[..., HikvisionClient], device: FakeDevice
) -> None:
    device.device_info = b"<html>login</html>"
    with pytest.raises(HikvisionResponseError):
        await make_client().get_device_info()


async def test_timeout(make_client: Callable[..., HikvisionClient], device: FakeDevice) -> None:
    device.delay = 1.0
    with pytest.raises(HikvisionConnectionError):
        await make_client(request_timeout=0.1).get_device_info()


async def test_unreachable_host() -> None:
    async with aiohttp.ClientSession() as session:
        client = HikvisionClient(session, "127.0.0.1", 1, USER, PASSWORD, ssl=False)
        with pytest.raises(HikvisionConnectionError):
            await client.get_device_info()


async def test_open_door(make_client: Callable[..., HikvisionClient], device: FakeDevice) -> None:
    await make_client().open_door(1)
    assert device.door_requests == [
        ("1", b"<RemoteControlDoor><cmd>open</cmd></RemoteControlDoor>")
    ]


async def test_open_door_refused(
    make_client: Callable[..., HikvisionClient], device: FakeDevice
) -> None:
    device.door_response = load_bytes("synthetic_response_error.xml")
    with pytest.raises(HikvisionResponseError, match="notSupport"):
        await make_client().open_door(1)


async def test_open_door_garbage_response(
    make_client: Callable[..., HikvisionClient], device: FakeDevice
) -> None:
    device.door_response = b"<<<"
    with pytest.raises(HikvisionResponseError):
        await make_client().open_door(1)


async def _collect(client: HikvisionClient, on_connect: Callable[[], None]) -> list[DecodedPart]:
    return [part async for part in client.stream(on_connect=on_connect)]


async def test_stream_yields_decoded_parts(
    make_client: Callable[..., HikvisionClient], device: FakeDevice
) -> None:
    card = load_bytes("derived_card_granted_live.json")
    wire = frame(load_bytes("synthetic_heartbeat.xml"), "application/xml") + frame(card)
    # Deliver it in awkward chunks to exercise the incremental parser.
    device.stream_chunks = [wire[i : i + 37] for i in range(0, len(wire), 37)]
    connected: list[bool] = []

    parts = await _collect(make_client(), lambda: connected.append(True))

    assert connected == [True]
    assert [p.kind for p in parts] == [PartKind.XML, PartKind.JSON]
    assert parts[1].payload is not None
    assert parts[1].payload["AccessControllerEvent"]["currentEvent"] is True


async def test_stream_read_timeout(
    make_client: Callable[..., HikvisionClient], device: FakeDevice
) -> None:
    device.stream_idle = 2.0
    with pytest.raises(HikvisionConnectionError):
        await _collect(make_client(stream_read_timeout=0.1), lambda: None)


async def test_stream_auth_failure(make_client: Callable[..., HikvisionClient]) -> None:
    connected: list[bool] = []
    with pytest.raises(HikvisionAuthError):
        await _collect(make_client(password="wrong"), lambda: connected.append(True))
    assert connected == []


@pytest.mark.parametrize(
    ("host", "port", "ssl", "expected"),
    [
        ("192.0.2.10", 443, True, "https://192.0.2.10"),
        ("192.0.2.10", 8443, True, "https://192.0.2.10:8443"),
        ("192.0.2.10", 80, False, "http://192.0.2.10"),
        ("2001:db8::10", 443, True, "https://[2001:db8::10]"),
    ],
)
def test_base_url(host: str, port: int, ssl: bool, expected: str) -> None:
    session = MagicMock(spec=aiohttp.ClientSession)
    client = HikvisionClient(session, host, port, USER, PASSWORD, ssl=ssl)
    assert str(client.base_url) == expected
