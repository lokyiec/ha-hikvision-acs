"""Binary sensor platform: momentary "access granted"."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_call_later

from .const import ACCESS_GRANTED_ON_TIME, signal_event
from .entity import HikvisionEntity
from .events import AccessEvent, EventCategory

if TYPE_CHECKING:
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from . import HikvisionConfigEntry

PARALLEL_UPDATES = 0

ACCESS_GRANTED = BinarySensorEntityDescription(
    key="access_granted",
    translation_key="access_granted",
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HikvisionConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the access-granted binary sensor."""
    async_add_entities([AccessGrantedBinarySensor(entry, ACCESS_GRANTED)])


class AccessGrantedBinarySensor(HikvisionEntity, BinarySensorEntity):
    """Turns on for a few seconds whenever a live event grants access.

    A new grant while already on restarts the timer rather than stacking.
    """

    _attr_is_on = False

    def __init__(
        self, entry: HikvisionConfigEntry, description: BinarySensorEntityDescription
    ) -> None:
        """Start in the off state."""
        super().__init__(entry, description)
        self._off_timer: CALLBACK_TYPE | None = None

    async def async_added_to_hass(self) -> None:
        """Subscribe to live events."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, signal_event(self._entry.entry_id), self._handle_event
            )
        )
        self.async_on_remove(self._cancel_off_timer)

    @callback
    def _handle_event(self, event: AccessEvent) -> None:
        if event.category is not EventCategory.ACCESS_GRANTED:
            return
        self._cancel_off_timer()
        self._attr_is_on = True
        self.async_write_ha_state()
        self._off_timer = async_call_later(self.hass, ACCESS_GRANTED_ON_TIME, self._turn_off)

    @callback
    def _turn_off(self, _now: datetime) -> None:
        self._off_timer = None
        self._attr_is_on = False
        self.async_write_ha_state()

    @callback
    def _cancel_off_timer(self) -> None:
        if self._off_timer is not None:
            self._off_timer()
            self._off_timer = None
