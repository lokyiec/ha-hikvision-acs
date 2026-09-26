"""Sensor platform: the last access event."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntityDescription,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.util import dt as dt_util

from .const import signal_event
from .entity import HikvisionEntity
from .events import ACCESS_CATEGORIES, EVENT_ATTRIBUTE_KEYS, AccessEvent, event_attributes

if TYPE_CHECKING:
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from . import HikvisionConfigEntry

PARALLEL_UPDATES = 0

LAST_ACCESS_EVENT = SensorEntityDescription(
    key="last_access_event",
    translation_key="last_access_event",
    device_class=SensorDeviceClass.TIMESTAMP,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HikvisionConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the last-access-event sensor."""
    async_add_entities([LastAccessEventSensor(entry, LAST_ACCESS_EVENT)])


class LastAccessEventSensor(HikvisionEntity, RestoreSensor):
    """Timestamp of the last live access event, with its details as attributes.

    The state is Home Assistant's receipt time, not the device's clock, which
    is often wrong. The device timestamp is kept as the ``device_time``
    attribute. Door relay and system events do not update it; they carry no
    person and would overwrite who came through the door.
    """

    _attr_native_value: datetime | None = None

    def __init__(self, entry: HikvisionConfigEntry, description: SensorEntityDescription) -> None:
        """Start empty; state is restored or filled by the first event."""
        super().__init__(entry, description)
        self._attr_extra_state_attributes: dict[str, Any] = {}

    async def async_added_to_hass(self) -> None:
        """Restore the previous event and subscribe to new ones."""
        await super().async_added_to_hass()
        if (last_data := await self.async_get_last_sensor_data()) is not None and isinstance(
            last_data.native_value, datetime
        ):
            self._attr_native_value = last_data.native_value
        if (last_state := await self.async_get_last_state()) is not None:
            self._attr_extra_state_attributes = {
                key: last_state.attributes[key]
                for key in EVENT_ATTRIBUTE_KEYS
                if key in last_state.attributes
            }
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, signal_event(self._entry.entry_id), self._handle_event
            )
        )

    @callback
    def _handle_event(self, event: AccessEvent) -> None:
        if event.category not in ACCESS_CATEGORIES:
            return
        self._attr_native_value = dt_util.utcnow()
        self._attr_extra_state_attributes = event_attributes(event)
        self.async_write_ha_state()
