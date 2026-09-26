"""Readable Activity (logbook) entries for Hikvision access events."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from homeassistant.components.logbook.const import LOGBOOK_ENTRY_MESSAGE, LOGBOOK_ENTRY_NAME
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN, EVENT_ACCESS

FALLBACK_NAME = "Hikvision terminal"


def _sentence_case(text: str) -> str:
    """Lower-case only the first letter, keeping acronyms such as PIN intact."""
    return text[:1].lower() + text[1:]


@callback
def async_describe_events(
    hass: HomeAssistant,
    async_describe_event: Callable[[str, str, Callable[[Event], dict[str, str]]], None],
) -> None:
    """Describe access events as "<who> <what>", e.g. "Jane authenticated via PIN"."""

    @callback
    def async_describe(event: Event) -> dict[str, str]:
        data: Mapping[str, Any] = event.data
        name = data.get("person_name") or _device_name(hass, data.get("device_id"))
        description = str(data.get("description") or data.get("event") or "event")
        return {
            LOGBOOK_ENTRY_NAME: name,
            LOGBOOK_ENTRY_MESSAGE: _sentence_case(description),
        }

    async_describe_event(DOMAIN, EVENT_ACCESS, async_describe)


def _device_name(hass: HomeAssistant, device_id: object) -> str:
    if isinstance(device_id, str) and (device := dr.async_get(hass).async_get(device_id)):
        return device.name_by_user or device.name or FALLBACK_NAME
    return FALLBACK_NAME
