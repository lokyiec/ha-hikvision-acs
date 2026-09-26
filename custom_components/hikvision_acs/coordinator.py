"""alertStream lifecycle: connect, reconnect, filter and dispatch events.

This is push, not poll: there is no update interval. A single background task
holds the stream open and fans live events out to entities via the
dispatcher and to automations via the event bus.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import Awaitable, Callable
from datetime import datetime
from time import monotonic
from typing import TYPE_CHECKING

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later

from .api import (
    HikvisionAuthError,
    HikvisionClient,
    HikvisionError,
    HikvisionPermissionError,
)
from .const import (
    DOMAIN,
    EVENT_ACCESS,
    RECONNECT_BASE_DELAY,
    RECONNECT_MAX_DELAY,
    STABLE_CONNECTION,
    UNAVAILABLE_GRACE,
    issue_id_stream_permission,
    signal_availability,
    signal_event,
)
from .events import SerialDeduplicator, event_attributes, parse_access_event
from .parser import DecodedPart, DeviceIdentity

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)


def backoff_delay(
    attempt: int,
    *,
    base: float = RECONNECT_BASE_DELAY,
    cap: float = RECONNECT_MAX_DELAY,
    rand: Callable[[], float] = random.random,
) -> float:
    """Exponential backoff with "equal jitter".

    Half the window is fixed and half is random, so reconnects from several
    devices spread out without ever collapsing to a zero delay.
    """
    window: float = min(cap, base * 2.0 ** max(attempt, 0))
    return window / 2 + rand() * window / 2


class HikvisionEventStream:
    """Owns the alertStream connection for one config entry."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: HikvisionClient,
        device: DeviceIdentity,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Set up state; nothing runs until :meth:`async_start`."""
        self._hass = hass
        self._entry = entry
        self._client = client
        self._device = device
        self._sleep = sleep
        self._dedup = SerialDeduplicator()
        self._available = True
        self._connected = False
        self._unavailable_timer: CALLBACK_TYPE | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def available(self) -> bool:
        """Whether entities fed by this stream should report as available."""
        return self._available

    @callback
    def async_start(self) -> None:
        """Start the background stream task."""
        self._task = self._entry.async_create_background_task(
            self._hass, self._run(), name=f"{DOMAIN} alertStream {self._device.serial_number}"
        )

    async def async_stop(self) -> None:
        """Stop the stream task and any pending timers."""
        self._cancel_unavailable_timer()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        attempt = 0
        while True:
            connected_at: float | None = None

            def mark_connected() -> None:
                nonlocal connected_at
                connected_at = monotonic()
                self._on_connected()

            try:
                async for part in self._client.stream(on_connect=mark_connected):
                    self._handle_part(part)
                _LOGGER.debug("alertStream closed by device")
            except HikvisionAuthError:
                # Never retry rejected credentials: the device locks the
                # account after a few failures. Hand over to reauth instead.
                _LOGGER.warning(
                    "Hikvision device %s rejected the stream credentials; "
                    "reauthentication required",
                    self._device.serial_number,
                )
                self._stop_permanently()
                self._entry.async_start_reauth(self._hass)
                return
            except HikvisionPermissionError:
                # The login works but the account may not subscribe to events.
                # Reauth would pass (it only reads deviceInfo) and loop, so
                # raise a repair issue for the user to fix permissions instead.
                _LOGGER.warning(
                    "Hikvision account on %s may not read the event stream; "
                    "grant it event/notification permission and reload",
                    self._device.serial_number,
                )
                self._stop_permanently()
                ir.async_create_issue(
                    self._hass,
                    DOMAIN,
                    issue_id_stream_permission(self._entry.entry_id),
                    is_fixable=False,
                    severity=ir.IssueSeverity.ERROR,
                    translation_key="stream_permission_denied",
                    translation_placeholders={"title": self._entry.title},
                )
                return
            except HikvisionError as err:
                _LOGGER.debug("alertStream dropped: %s", err)
            except Exception:
                # A bug must not silently kill the task while entities keep
                # looking healthy; log it and reconnect like any other drop.
                _LOGGER.exception("Unexpected error in the Hikvision alertStream")

            self._on_disconnected()
            if (
                connected_at is not None
                and monotonic() - connected_at >= STABLE_CONNECTION.total_seconds()
            ):
                attempt = 0
            delay = backoff_delay(attempt)
            attempt += 1
            _LOGGER.debug("Reconnecting alertStream in %.1fs (attempt %d)", delay, attempt)
            await self._sleep(delay)

    @callback
    def _handle_part(self, part: DecodedPart) -> None:
        if part.payload is None:
            return
        event = parse_access_event(part.payload)
        if event is None:
            return
        if not event.live:
            # Backlog flushed on connect. Must never trigger automations.
            _LOGGER.debug(
                "Ignoring replayed event %s/%s serial=%s", event.major, event.minor, event.serial_no
            )
            return
        if event.definition.internal:
            _LOGGER.debug("Subscriber bookkeeping event %s/%s", event.major, event.minor)
            return
        if self._dedup.is_duplicate(event):
            _LOGGER.debug("Ignoring duplicate event serial=%s", event.serial_no)
            return
        if not event.definition.confirmed:
            _LOGGER.debug(
                "Event code %s/%s is not confirmed (%s)",
                event.major,
                event.minor,
                event.definition.description,
            )

        async_dispatcher_send(self._hass, signal_event(self._entry.entry_id), event)
        self._hass.bus.async_fire(
            EVENT_ACCESS,
            {
                "device_id": self._device_id(),
                "serial_number": self._device.serial_number,
                **event_attributes(event),
            },
        )

    def _device_id(self) -> str | None:
        device = dr.async_get(self._hass).async_get_device_by_identifier(
            (DOMAIN, self._device.serial_number), self._entry.entry_id
        )
        return device.id if device else None

    @callback
    def _stop_permanently(self) -> None:
        self._connected = False
        self._cancel_unavailable_timer()
        self._set_available(False)

    @callback
    def _on_connected(self) -> None:
        self._connected = True
        self._cancel_unavailable_timer()
        self._set_available(True)

    @callback
    def _on_disconnected(self) -> None:
        self._connected = False
        if self._available and self._unavailable_timer is None:
            self._unavailable_timer = async_call_later(
                self._hass, UNAVAILABLE_GRACE, self._grace_expired
            )

    @callback
    def _grace_expired(self, _now: datetime) -> None:
        self._unavailable_timer = None
        if not self._connected:
            _LOGGER.debug("alertStream down for %s; marking unavailable", UNAVAILABLE_GRACE)
            self._set_available(False)

    @callback
    def _cancel_unavailable_timer(self) -> None:
        if self._unavailable_timer is not None:
            self._unavailable_timer()
            self._unavailable_timer = None

    @callback
    def _set_available(self, available: bool) -> None:
        if available == self._available:
            return
        self._available = available
        async_dispatcher_send(self._hass, signal_availability(self._entry.entry_id))
