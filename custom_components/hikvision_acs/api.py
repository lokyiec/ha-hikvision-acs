"""Async ISAPI client for Hikvision access control terminals.

All HTTP traffic for the integration goes through :class:`HikvisionClient`.
Entities never talk to the device directly.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from http import HTTPStatus
from typing import Final

import aiohttp
from yarl import URL

from .parser import (
    DecodedPart,
    DeviceIdentity,
    MultipartStreamParser,
    ParseError,
    decode_part,
    extract_boundary,
    parse_device_info,
    parse_response_status,
)

_LOGGER = logging.getLogger(__name__)

PATH_DEVICE_INFO: Final = "/ISAPI/System/deviceInfo"
PATH_ALERT_STREAM: Final = "/ISAPI/Event/notification/alertStream"
PATH_REMOTE_DOOR: Final = "/ISAPI/AccessControl/RemoteControl/door/{door_no}"

DEFAULT_REQUEST_TIMEOUT: Final = 10.0
# The stream is idle between events. If nothing at all arrives for this long
# the connection is assumed dead (Wi-Fi drop without a TCP reset) and the
# caller reconnects.
DEFAULT_STREAM_READ_TIMEOUT: Final = 300.0

_OPEN_DOOR_BODY: Final = b"<RemoteControlDoor><cmd>open</cmd></RemoteControlDoor>"


class HikvisionError(Exception):
    """Base class for client errors."""


class HikvisionConnectionError(HikvisionError):
    """The device could not be reached or the connection dropped."""


class HikvisionAuthError(HikvisionError):
    """The device rejected the credentials (HTTP 401)."""


class HikvisionPermissionError(HikvisionError):
    """The account is valid but lacks permission for the operation (HTTP 403)."""


class HikvisionResponseError(HikvisionError):
    """The device answered, but not with what was expected."""


class HikvisionClient:
    """Thin async wrapper around the three ISAPI endpoints the integration uses."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        port: int,
        username: str,
        password: str,
        *,
        ssl: bool = True,
        verify_ssl: bool = False,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
        stream_read_timeout: float = DEFAULT_STREAM_READ_TIMEOUT,
    ) -> None:
        """Initialise the client. No I/O happens until a method is called."""
        self._session = session
        self._base_url = URL.build(scheme="https" if ssl else "http", host=host, port=port)
        self._ssl: bool = verify_ssl if ssl else True
        # One middleware instance per client, so the digest nonce is reused
        # and each request does not need a fresh 401 challenge round trip.
        self._auth = aiohttp.DigestAuthMiddleware(username, password)
        self._request_timeout = request_timeout
        self._stream_read_timeout = stream_read_timeout
        self._auth_failed = False

    @property
    def auth_failed(self) -> bool:
        """Whether the device has rejected these credentials.

        Once set, every call fails fast without contacting the device: the
        terminal locks the account after a few failed logins, so a rejected
        password must never be sent again. A new client (after reauth) starts
        clean.
        """
        return self._auth_failed

    @property
    def base_url(self) -> URL:
        """Scheme, host and port of the device."""
        return self._base_url

    async def get_device_info(self) -> DeviceIdentity:
        """Fetch device identity; doubles as the connection/credentials test."""
        body = await self._request("GET", PATH_DEVICE_INFO)
        try:
            return parse_device_info(body)
        except ParseError as err:
            raise HikvisionResponseError(f"unexpected deviceInfo response: {err}") from err

    async def open_door(self, door_no: int) -> None:
        """Pulse the relay for ``door_no``."""
        body = await self._request(
            "PUT",
            PATH_REMOTE_DOOR.format(door_no=door_no),
            data=_OPEN_DOOR_BODY,
            headers={"Content-Type": "application/xml"},
        )
        try:
            status = parse_response_status(body)
        except ParseError as err:
            raise HikvisionResponseError(f"unexpected door control response: {err}") from err
        if not status.ok:
            raise HikvisionResponseError(
                f"door control refused: statusCode={status.status_code} "
                f"subStatusCode={status.sub_status_code}"
            )

    async def stream(
        self, on_connect: Callable[[], None] | None = None
    ) -> AsyncIterator[DecodedPart]:
        """Open the alertStream and yield decoded parts until it ends.

        ``on_connect`` is called once the device accepts the request, before
        the first part arrives (the stream can be idle for a long time).

        Returns normally if the device closes the stream. Raises
        :class:`HikvisionConnectionError` on network failure or read timeout,
        and :class:`HikvisionAuthError` / :class:`HikvisionPermissionError`
        if the credentials stop working.
        """
        timeout = aiohttp.ClientTimeout(
            total=None,
            connect=self._request_timeout,
            sock_read=self._stream_read_timeout,
        )
        url = self._base_url.with_path(PATH_ALERT_STREAM)
        self._check_auth_latch()
        try:
            async with self._session.get(
                url, timeout=timeout, ssl=self._ssl, middlewares=(self._auth,)
            ) as response:
                self._raise_for_status(response)
                parser = MultipartStreamParser(
                    boundary=extract_boundary(response.headers.get(aiohttp.hdrs.CONTENT_TYPE))
                )
                _LOGGER.debug("alertStream connected to %s", self._base_url.host)
                if on_connect is not None:
                    on_connect()
                async for chunk in response.content.iter_any():
                    for part in parser.feed(chunk):
                        yield decode_part(part)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise HikvisionConnectionError(f"alertStream failed: {err}") from err

    async def _request(
        self,
        method: str,
        path: str,
        *,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> bytes:
        url = self._base_url.with_path(path)
        self._check_auth_latch()
        try:
            async with self._session.request(
                method,
                url,
                data=data,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=self._request_timeout),
                ssl=self._ssl,
                middlewares=(self._auth,),
            ) as response:
                self._raise_for_status(response)
                return await response.read()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise HikvisionConnectionError(f"{method} {path} failed: {err}") from err

    def _check_auth_latch(self) -> None:
        if self._auth_failed:
            raise HikvisionAuthError("credentials were already rejected; not retrying")

    def _raise_for_status(self, response: aiohttp.ClientResponse) -> None:
        status = response.status
        if status == HTTPStatus.UNAUTHORIZED:
            self._auth_failed = True
            raise HikvisionAuthError("device rejected the credentials")
        if status == HTTPStatus.FORBIDDEN:
            raise HikvisionPermissionError("account lacks permission for this operation")
        if status >= HTTPStatus.BAD_REQUEST:
            raise HikvisionResponseError(f"HTTP {status} from {response.url.path}")
