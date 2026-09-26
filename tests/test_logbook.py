"""Tests for the Activity (logbook) descriptions."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.components.logbook.const import LOGBOOK_ENTRY_MESSAGE, LOGBOOK_ENTRY_NAME
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hikvision_acs.const import DOMAIN, EVENT_ACCESS
from custom_components.hikvision_acs.logbook import async_describe_events

from .helpers import DEVICE


def _describer(hass: HomeAssistant) -> Any:
    registered: dict[str, Any] = {}

    def register(domain: str, event_type: str, describe: Any) -> None:
        assert (domain, event_type) == (DOMAIN, EVENT_ACCESS)
        registered["describe"] = describe

    async_describe_events(hass, register)
    return registered["describe"]


async def test_person_entry(hass: HomeAssistant) -> None:
    describe = _describer(hass)
    entry = describe(
        Event(EVENT_ACCESS, {"person_name": "Jane Doe", "description": "Authenticated via PIN"})
    )
    assert entry == {
        LOGBOOK_ENTRY_NAME: "Jane Doe",
        LOGBOOK_ENTRY_MESSAGE: "authenticated via PIN",
    }


async def test_entry_without_person_uses_device_name(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, DEVICE.serial_number), setup_integration.entry_id
    )
    assert device is not None
    describe = _describer(hass)
    entry = describe(Event(EVENT_ACCESS, {"device_id": device.id, "description": "Remote unlock"}))
    assert entry[LOGBOOK_ENTRY_NAME] == "Access Control Device"
    assert entry[LOGBOOK_ENTRY_MESSAGE] == "remote unlock"


@pytest.mark.parametrize("data", [{}, {"device_id": "missing", "event": "unknown_5_1"}])
async def test_fallbacks(hass: HomeAssistant, data: dict[str, Any]) -> None:
    entry = _describer(hass)(Event(EVENT_ACCESS, data))
    assert entry[LOGBOOK_ENTRY_NAME] == "Hikvision terminal"
    assert entry[LOGBOOK_ENTRY_MESSAGE] in {"event", "unknown_5_1"}
