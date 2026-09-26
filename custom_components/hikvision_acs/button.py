"""Button platform: open the door."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .api import HikvisionAuthError, HikvisionError, HikvisionPermissionError
from .const import DEFAULT_DOOR_NO, DOMAIN
from .entity import HikvisionEntity

if TYPE_CHECKING:
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from . import HikvisionConfigEntry

# One door command at a time per device.
PARALLEL_UPDATES = 1

OPEN_DOOR = ButtonEntityDescription(key="open_door", translation_key="open_door")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HikvisionConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the open-door button."""
    async_add_entities([OpenDoorButton(entry, OPEN_DOOR)])


class OpenDoorButton(HikvisionEntity, ButtonEntity):
    """Pulses the door relay.

    Unlike the event entities it does not depend on the stream, so it stays
    available while the stream reconnects. It becomes unavailable once the
    credentials are rejected, so presses cannot keep sending a bad password.
    """

    @property
    def available(self) -> bool:
        """Available unless the device has rejected the credentials."""
        return not self._entry.runtime_data.client.auth_failed

    async def async_press(self) -> None:
        """Send the open command."""
        try:
            await self._entry.runtime_data.client.open_door(DEFAULT_DOOR_NO)
        except HikvisionAuthError as err:
            self._entry.async_start_reauth(self.hass)
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="invalid_auth"
            ) from err
        except HikvisionPermissionError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="door_permission_denied"
            ) from err
        except HikvisionError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="door_open_failed"
            ) from err
