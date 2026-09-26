"""Tests for the alertStream lifecycle: filtering, reconnect, availability."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.hikvision_acs.api import (
    HikvisionAuthError,
    HikvisionConnectionError,
    HikvisionPermissionError,
)
from custom_components.hikvision_acs.const import (
    DOMAIN,
    EVENT_ACCESS,
    UNAVAILABLE_GRACE,
    issue_id_stream_permission,
    signal_availability,
    signal_event,
)
from custom_components.hikvision_acs.coordinator import HikvisionEventStream, backoff_delay
from custom_components.hikvision_acs.events import AccessEvent
from custom_components.hikvision_acs.parser import DecodedPart, PartKind

from .helpers import DEVICE, END_OF_STREAM, FakeClient, access_payload, json_part


async def pump(hass: HomeAssistant) -> None:
    """Let the background stream task run until it blocks again."""
    for _ in range(10):
        await asyncio.sleep(0)
    await hass.async_block_till_done()


class Gate:
    """A sleep replacement the test releases explicitly."""

    def __init__(self) -> None:
        self.delays: list[float] = []
        self._event = asyncio.Event()

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        await self._event.wait()
        self._event.clear()

    def release(self) -> None:
        self._event.set()


@pytest.fixture
def gate() -> Gate:
    return Gate()


@pytest.fixture
async def stream(hass: HomeAssistant, config_entry: MockConfigEntry, gate: Gate) -> Any:
    client = FakeClient()
    event_stream = HikvisionEventStream(hass, config_entry, client, DEVICE, sleep=gate)
    event_stream.async_start()
    await pump(hass)
    yield event_stream, client
    await event_stream.async_stop()


# --- backoff -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("attempt", "rand", "expected"),
    [
        (0, 0.0, 0.5),
        (0, 1.0, 1.0),
        (3, 0.5, 6.0),
        (10, 1.0, 60.0),
        (10, 0.0, 30.0),
        (-1, 0.0, 0.5),
    ],
)
def test_backoff_delay(attempt: int, rand: float, expected: float) -> None:
    assert backoff_delay(attempt, rand=lambda: rand) == pytest.approx(expected)


def test_backoff_never_zero() -> None:
    assert backoff_delay(0, rand=lambda: 0.0) > 0


# --- filtering and dispatch (through the real setup path) ---------------------


async def test_live_event_is_dispatched_and_fired(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_client: FakeClient
) -> None:
    bus_events = async_capture_events(hass, EVENT_ACCESS)
    dispatched: list[AccessEvent] = []
    async_dispatcher_connect(hass, signal_event(setup_integration.entry_id), dispatched.append)

    await fake_client.queue.put(json_part(access_payload(serial=7000)))
    await pump(hass)

    assert [e.serial_no for e in dispatched] == [7000]
    assert len(bus_events) == 1
    data = bus_events[0].data
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, DEVICE.serial_number), setup_integration.entry_id
    )
    assert device is not None
    assert data["device_id"] == device.id
    assert data["serial_number"] == DEVICE.serial_number
    assert data["event"] == "card_access_granted"
    assert data["person_name"] == "Jane Doe"


async def test_replayed_duplicate_and_irrelevant_parts_are_dropped(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_client: FakeClient
) -> None:
    bus_events: list[Event] = async_capture_events(hass, EVENT_ACCESS)
    for part in (
        json_part(access_payload(live=False, serial=1)),  # backlog flush
        json_part(access_payload(serial=2)),
        json_part(access_payload(serial=2)),  # duplicate
        json_part({"eventType": "videoloss", "eventState": "inactive"}),
        DecodedPart(kind=PartKind.BINARY, payload=None, content_type="image/jpeg"),
        json_part(access_payload(5, 12345, serial=3)),  # unknown code: kept
    ):
        await fake_client.queue.put(part)
    await pump(hass)

    assert [e.data["serial_no"] for e in bus_events] == [2, 3]
    assert bus_events[1].data["event"] == "unknown_5_12345"


async def test_bus_event_without_registered_device(hass: HomeAssistant, stream: Any) -> None:
    _, client = stream
    bus_events = async_capture_events(hass, EVENT_ACCESS)
    await client.queue.put(json_part(access_payload()))
    await pump(hass)
    assert bus_events[0].data["device_id"] is None


# --- reconnect ---------------------------------------------------------------


async def test_reconnects_after_error_with_backoff(
    hass: HomeAssistant, stream: Any, gate: Gate
) -> None:
    event_stream, client = stream
    assert client.connect_count == 1

    await client.queue.put(HikvisionConnectionError("wifi"))
    await pump(hass)
    assert len(gate.delays) == 1
    assert event_stream.available  # still inside the grace period

    gate.release()
    await pump(hass)
    assert client.connect_count == 2


async def test_clean_stream_end_also_reconnects(
    hass: HomeAssistant, stream: Any, gate: Gate
) -> None:
    _, client = stream
    await client.queue.put(END_OF_STREAM)
    await pump(hass)
    gate.release()
    await pump(hass)
    assert client.connect_count == 2


async def test_backoff_grows_while_failing_and_resets_after_stable_connection(
    hass: HomeAssistant, stream: Any, gate: Gate
) -> None:
    _, client = stream
    attempts: list[int] = []

    def fake_backoff(attempt: int) -> float:
        attempts.append(attempt)
        return 0.0

    with patch("custom_components.hikvision_acs.coordinator.backoff_delay", fake_backoff):
        # Two failed connects in a row.
        client.refuse_connect = [HikvisionConnectionError("a"), HikvisionConnectionError("b")]
        await client.queue.put(HikvisionConnectionError("drop"))
        for _ in range(2):
            await pump(hass)
            gate.release()
        await pump(hass)
        assert attempts == [0, 1, 2]

        # Now connected; pretend the connection lived long enough to be stable.
        with patch(
            "custom_components.hikvision_acs.coordinator.monotonic",
            side_effect=[0.0, 3600.0],
        ):
            gate.release()
            await pump(hass)
            await client.queue.put(HikvisionConnectionError("drop"))
            await pump(hass)
        assert attempts[-1] == 0


# --- availability ------------------------------------------------------------


async def test_unavailable_only_after_grace_period(
    hass: HomeAssistant, stream: Any, gate: Gate, config_entry: MockConfigEntry
) -> None:
    event_stream, client = stream
    changes: list[bool] = []
    async_dispatcher_connect(
        hass,
        signal_availability(config_entry.entry_id),
        lambda: changes.append(event_stream.available),
    )

    await client.queue.put(HikvisionConnectionError("wifi"))
    await pump(hass)
    async_fire_time_changed(hass, dt_util.utcnow() + UNAVAILABLE_GRACE - timedelta(seconds=1))
    await pump(hass)
    assert event_stream.available

    async_fire_time_changed(hass, dt_util.utcnow() + UNAVAILABLE_GRACE + timedelta(seconds=1))
    await pump(hass)
    assert not event_stream.available

    gate.release()
    await pump(hass)
    assert event_stream.available
    assert changes == [False, True]


async def test_reconnect_within_grace_keeps_available(
    hass: HomeAssistant, stream: Any, gate: Gate
) -> None:
    event_stream, client = stream
    await client.queue.put(HikvisionConnectionError("wifi"))
    await pump(hass)
    gate.release()
    await pump(hass)

    async_fire_time_changed(hass, dt_util.utcnow() + UNAVAILABLE_GRACE * 2)
    await pump(hass)
    assert event_stream.available


async def test_rejected_credentials_stop_stream_and_start_reauth(
    hass: HomeAssistant, stream: Any, gate: Gate, config_entry: MockConfigEntry
) -> None:
    event_stream, client = stream
    await client.queue.put(HikvisionAuthError("x"))
    await pump(hass)

    assert not event_stream.available
    assert gate.delays == []  # no retry
    flows = hass.config_entries.flow.async_progress()
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]
    assert flows[0]["context"]["entry_id"] == config_entry.entry_id


async def test_stream_permission_denied_raises_repair_not_reauth(
    hass: HomeAssistant, stream: Any, gate: Gate, config_entry: MockConfigEntry
) -> None:
    event_stream, client = stream
    await client.queue.put(HikvisionPermissionError("x"))
    await pump(hass)

    assert not event_stream.available
    assert gate.delays == []
    # Reauth would succeed (deviceInfo is readable) and loop forever.
    assert hass.config_entries.flow.async_progress() == []
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, issue_id_stream_permission(config_entry.entry_id)
    )
    assert issue is not None
    assert issue.translation_key == "stream_permission_denied"


async def test_unexpected_error_is_logged_and_reconnects(
    hass: HomeAssistant, stream: Any, gate: Gate, caplog: pytest.LogCaptureFixture
) -> None:
    _, client = stream
    await client.queue.put(RuntimeError("bug"))
    await pump(hass)
    assert "Unexpected error in the Hikvision alertStream" in caplog.text
    gate.release()
    await pump(hass)
    assert client.connect_count == 2


async def test_stop_is_idempotent(hass: HomeAssistant, stream: Any) -> None:
    event_stream, _ = stream
    await event_stream.async_stop()
    await event_stream.async_stop()
