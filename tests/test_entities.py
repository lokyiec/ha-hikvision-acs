"""Tests for the sensor, binary sensor and button entities."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN
from homeassistant.components.button import SERVICE_PRESS
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.const import (
    ATTR_ENTITY_ID,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    mock_restore_cache_with_extra_data,
)

from custom_components.hikvision_acs.api import (
    HikvisionAuthError,
    HikvisionConnectionError,
    HikvisionPermissionError,
    HikvisionResponseError,
)
from custom_components.hikvision_acs.const import ACCESS_GRANTED_ON_TIME, UNAVAILABLE_GRACE

from .helpers import FakeClient, access_payload, json_part

SENSOR = "sensor.access_control_device_last_access_event"
BINARY = "binary_sensor.access_control_device_access_granted"
BUTTON = "button.access_control_device_open_door"
EVENT = "event.access_control_device_access"


async def pump(hass: HomeAssistant) -> None:
    for _ in range(10):
        await asyncio.sleep(0)
    await hass.async_block_till_done()


def _state(hass: HomeAssistant, entity_id: str) -> State:
    state = hass.states.get(entity_id)
    assert state is not None
    return state


async def test_initial_states(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    assert _state(hass, SENSOR).state == STATE_UNKNOWN
    assert _state(hass, BINARY).state == STATE_OFF
    assert _state(hass, BUTTON).state == STATE_UNKNOWN


async def test_sensor_uses_receipt_time_not_device_clock(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    fake_client: FakeClient,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to("2026-09-26T10:00:00+00:00")
    await fake_client.queue.put(json_part(access_payload()))
    await pump(hass)

    state = _state(hass, SENSOR)
    assert state.state == "2026-09-26T10:00:00+00:00"
    assert state.attributes["device_time"] == "2026-07-12T12:57:33+01:00"
    assert state.attributes["person_name"] == "Jane Doe"
    assert state.attributes["card_no"] == "0000000001"
    assert state.attributes["verify_mode"] == "cardOrFpOrPw"
    assert state.attributes["event"] == "card_access_granted"
    assert state.attributes["confirmed"] is True


async def test_sensor_ignores_door_and_system_events(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_client: FakeClient
) -> None:
    await fake_client.queue.put(json_part(access_payload(serial=1)))
    await fake_client.queue.put(json_part(access_payload(5, 21, serial=2)))
    await fake_client.queue.put(json_part(access_payload(3, 1029, serial=3)))
    await pump(hass)
    assert _state(hass, SENSOR).attributes["serial_no"] == 1


async def test_access_event_entity_records_each_entry(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_client: FakeClient
) -> None:
    assert _state(hass, EVENT).state == STATE_UNKNOWN

    # The sequence observed on the reference device: PIN entry, then a remote
    # open with its door relay events and a subscriber (re)connect.
    await fake_client.queue.put(json_part(access_payload(5, 181, serial=1, name="Person B")))
    await pump(hass)
    state = _state(hass, EVENT)
    assert state.attributes["event_type"] == "pin_access_granted"
    assert state.attributes["person_name"] == "Person B"
    first_state = state.state

    for serial, (major, minor) in enumerate(((3, 121), (5, 21), (5, 22)), start=2):
        await fake_client.queue.put(
            json_part(access_payload(major, minor, serial=serial, name=None, cardNo=None))
        )
    await pump(hass)
    # Relay and subscriber events are not access entries.
    assert _state(hass, EVENT).state == first_state

    await fake_client.queue.put(
        json_part(access_payload(3, 1024, serial=9, name=None, cardNo=None))
    )
    await pump(hass)
    state = _state(hass, EVENT)
    assert state.state != first_state
    assert state.attributes["event_type"] == "remote_unlock"
    assert state.attributes["person_name"] is None
    assert state.attributes["event_types"] == [
        "card_access_granted",
        "pin_access_granted",
        "remote_unlock",
        "unknown",
    ]


async def test_access_event_entity_maps_unknown_codes(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_client: FakeClient
) -> None:
    await fake_client.queue.put(json_part(access_payload(5, 777, serial=1)))
    await pump(hass)
    state = _state(hass, EVENT)
    assert state.attributes["event_type"] == "unknown"
    assert state.attributes["event"] == "unknown_5_777"


async def test_sensor_ignores_relay_and_subscriber_events(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_client: FakeClient
) -> None:
    await fake_client.queue.put(json_part(access_payload(5, 181, serial=1)))
    for serial, code in enumerate(((3, 121), (5, 21), (5, 22), (3, 999)), start=2):
        await fake_client.queue.put(json_part(access_payload(*code, serial=serial, name=None)))
    await pump(hass)
    assert _state(hass, SENSOR).attributes["serial_no"] == 1


async def test_sensor_tracks_unknown_codes(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_client: FakeClient
) -> None:
    await fake_client.queue.put(json_part(access_payload(5, 777, serial=9)))
    await pump(hass)
    assert _state(hass, SENSOR).attributes["event"] == "unknown_5_777"
    # Unknown codes must not count as a grant.
    assert _state(hass, BINARY).state == STATE_OFF


async def test_replayed_events_touch_nothing(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_client: FakeClient
) -> None:
    await fake_client.queue.put(json_part(access_payload(live=False)))
    await pump(hass)
    assert _state(hass, SENSOR).state == STATE_UNKNOWN
    assert _state(hass, BINARY).state == STATE_OFF


async def test_sensor_restores_last_event(
    hass: HomeAssistant, config_entry: MockConfigEntry, fake_client: FakeClient
) -> None:
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(
                    SENSOR,
                    "2026-09-25T08:00:00+00:00",
                    {"person_name": "Jane Doe", "serial_no": 5, "friendly_name": "stale"},
                ),
                {
                    "native_value": {
                        "__type": "<class 'datetime.datetime'>",
                        "isoformat": "2026-09-25T08:00:00+00:00",
                    },
                    "native_unit_of_measurement": None,
                },
            )
        ],
    )
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    state = _state(hass, SENSOR)
    assert state.state == "2026-09-25T08:00:00+00:00"
    assert state.attributes["person_name"] == "Jane Doe"
    assert state.attributes["friendly_name"] != "stale"


async def test_binary_sensor_is_momentary_and_retriggers(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    fake_client: FakeClient,
    freezer: FrozenDateTimeFactory,
) -> None:
    await fake_client.queue.put(json_part(access_payload(serial=1)))
    await pump(hass)
    assert _state(hass, BINARY).state == STATE_ON

    # A second grant just before auto-off restarts the timer.
    freezer.tick(ACCESS_GRANTED_ON_TIME - timedelta(seconds=1))
    async_fire_time_changed(hass)
    await fake_client.queue.put(json_part(access_payload(serial=2)))
    await pump(hass)

    freezer.tick(timedelta(seconds=2))
    async_fire_time_changed(hass)
    await pump(hass)
    assert _state(hass, BINARY).state == STATE_ON

    freezer.tick(ACCESS_GRANTED_ON_TIME)
    async_fire_time_changed(hass)
    await pump(hass)
    assert _state(hass, BINARY).state == STATE_OFF


async def test_binary_sensor_ignores_non_grants(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_client: FakeClient
) -> None:
    await fake_client.queue.put(json_part(access_payload(5, 21)))
    await pump(hass)
    assert _state(hass, BINARY).state == STATE_OFF


async def test_event_entities_follow_stream_availability_but_button_does_not(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_client: FakeClient
) -> None:
    # Make every reconnect fail so the stream stays down.
    fake_client.refuse_connect = [HikvisionConnectionError("down")] * 100
    await fake_client.queue.put(HikvisionConnectionError("wifi"))
    await pump(hass)
    async_fire_time_changed(hass, dt_util.utcnow() + UNAVAILABLE_GRACE + timedelta(seconds=1))
    await pump(hass)

    assert _state(hass, SENSOR).state == STATE_UNAVAILABLE
    assert _state(hass, BINARY).state == STATE_UNAVAILABLE
    assert _state(hass, BUTTON).state != STATE_UNAVAILABLE


async def test_button_unavailable_once_credentials_rejected(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_client: FakeClient
) -> None:
    fake_client.auth_failed = True
    await fake_client.queue.put(HikvisionAuthError("x"))
    await pump(hass)
    assert _state(hass, BUTTON).state == STATE_UNAVAILABLE


async def test_button_opens_door_one(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_client: FakeClient
) -> None:
    await hass.services.async_call(
        BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: BUTTON}, blocking=True
    )
    fake_client.open_door.assert_awaited_once_with(1)


@pytest.mark.parametrize(
    ("error", "translation_key", "reauth"),
    [
        (HikvisionAuthError("x"), "invalid_auth", True),
        (HikvisionPermissionError("x"), "door_permission_denied", False),
        (HikvisionResponseError("x"), "door_open_failed", False),
        (HikvisionConnectionError("x"), "door_open_failed", False),
    ],
)
async def test_button_errors(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    fake_client: FakeClient,
    error: Exception,
    translation_key: str,
    reauth: bool,
) -> None:
    fake_client.open_door.side_effect = error
    with pytest.raises(HomeAssistantError) as exc_info:
        await hass.services.async_call(
            BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: BUTTON}, blocking=True
        )
    assert exc_info.value.translation_key == translation_key
    flows = [
        f
        for f in hass.config_entries.flow.async_progress()
        if f["context"]["source"] == SOURCE_REAUTH
    ]
    assert bool(flows) is reauth
