"""Event platform: the access history, one state change per entry.

This is the entity to build lists and dashboards on: its recorded history is
the access log, each entry carrying the event type (card, PIN, remote…) and
who it was as attributes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.event import EventEntity, EventEntityDescription
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import signal_event
from .entity import HikvisionEntity
from .events import (
    ACCESS_CATEGORIES,
    ACCESS_EVENT_TYPES,
    AccessEvent,
    access_event_type,
    event_attributes,
)

if TYPE_CHECKING:
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from . import HikvisionConfigEntry

PARALLEL_UPDATES = 0

ACCESS = EventEntityDescription(
    key="access",
    translation_key="access",
    event_types=list(ACCESS_EVENT_TYPES),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HikvisionConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the access event entity."""
    async_add_entities([AccessEventEntity(entry, ACCESS)])


class AccessEventEntity(HikvisionEntity, EventEntity):
    """Fires once per live access: card, PIN, remote unlock or unknown code."""

    async def async_added_to_hass(self) -> None:
        """Subscribe to live events."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, signal_event(self._entry.entry_id), self._handle_event
            )
        )

    @callback
    def _handle_event(self, event: AccessEvent) -> None:
        if event.category not in ACCESS_CATEGORIES:
            return
        self._trigger_event(access_event_type(event), event_attributes(event))
        self.async_write_ha_state()
