"""Shared base class for Hikvision Access Control entities."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.core import callback
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity, EntityDescription

from .const import DOMAIN, MANUFACTURER, signal_availability

if TYPE_CHECKING:
    from . import HikvisionConfigEntry


class HikvisionEntity(Entity):
    """Device-backed entity whose availability follows the event stream."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry: HikvisionConfigEntry, description: EntityDescription) -> None:
        """Bind the entity to its config entry and device."""
        self.entity_description = description
        self._entry = entry
        device = entry.runtime_data.device
        # Unique IDs derive from the serial number: IP addresses change.
        self._attr_unique_id = f"{device.serial_number}_{description.key}"
        connections = (
            {(CONNECTION_NETWORK_MAC, device.mac_address)} if device.mac_address else set()
        )
        firmware = " ".join(p for p in (device.firmware_version, device.firmware_build) if p)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.serial_number)},
            connections=connections,
            manufacturer=MANUFACTURER,
            model=device.model,
            name=device.name or device.model or "Hikvision access terminal",
            serial_number=device.serial_number,
            sw_version=firmware or None,
            configuration_url=str(entry.runtime_data.client.base_url),
        )

    @property
    def available(self) -> bool:
        """Available while the alertStream is (or recently was) connected."""
        return self._entry.runtime_data.stream.available

    async def async_added_to_hass(self) -> None:
        """Follow stream availability changes."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_availability(self._entry.entry_id),
                self._handle_availability,
            )
        )

    @callback
    def _handle_availability(self) -> None:
        self.async_write_ha_state()
