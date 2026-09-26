"""Config flow for Hikvision Access Control."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_SSL,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
)
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from . import create_client
from .api import (
    HikvisionAuthError,
    HikvisionConnectionError,
    HikvisionError,
    HikvisionPermissionError,
)
from .const import DEFAULT_PORT_HTTPS, DEFAULT_SSL, DEFAULT_VERIFY_SSL, DOMAIN
from .parser import DeviceIdentity

_LOGGER = logging.getLogger(__name__)

_PASSWORD_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))


def _user_schema(defaults: Mapping[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, vol.UNDEFINED)): str,
            vol.Required(CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_PORT_HTTPS)): (
                NumberSelector(
                    NumberSelectorConfig(min=1, max=65535, step=1, mode=NumberSelectorMode.BOX)
                )
            ),
            vol.Required(CONF_SSL, default=defaults.get(CONF_SSL, DEFAULT_SSL)): bool,
            vol.Required(
                CONF_VERIFY_SSL, default=defaults.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL)
            ): bool,
            vol.Required(CONF_USERNAME, default=defaults.get(CONF_USERNAME, vol.UNDEFINED)): str,
            vol.Required(CONF_PASSWORD): _PASSWORD_SELECTOR,
        }
    )


class HikvisionAcsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up a terminal from host and credentials, with a live connection test."""

    VERSION = 1

    async def _async_validate(
        self, data: Mapping[str, Any]
    ) -> tuple[DeviceIdentity | None, dict[str, str]]:
        """Test the connection exactly once.

        Rejected credentials are reported back to the user, never retried:
        the device locks the account after a handful of failures.
        """
        client = create_client(self.hass, data)
        try:
            return await client.get_device_info(), {}
        except HikvisionAuthError, HikvisionPermissionError:
            return None, {"base": "invalid_auth"}
        except HikvisionConnectionError:
            return None, {"base": "cannot_connect"}
        except HikvisionError:
            return None, {"base": "unexpected_response"}
        except Exception:
            _LOGGER.exception("Unexpected error while validating Hikvision connection")
            return None, {"base": "unknown"}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for connection details and validate them against the device."""
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {
                **user_input,
                CONF_HOST: user_input[CONF_HOST].strip(),
                CONF_PORT: int(user_input[CONF_PORT]),
            }
            device, errors = await self._async_validate(data)
            if device is not None:
                await self.async_set_unique_id(device.serial_number)
                # Same device at a new address: update it rather than duplicate.
                self._abort_if_unique_id_configured(
                    updates={
                        key: data[key] for key in (CONF_HOST, CONF_PORT, CONF_SSL, CONF_VERIFY_SSL)
                    }
                )
                return self.async_create_entry(
                    title=device.name or device.model or data[CONF_HOST], data=data
                )
            user_input = {key: value for key, value in data.items() if key != CONF_PASSWORD}

        return self.async_show_form(
            step_id="user",
            data_schema=_user_schema(user_input or {}),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Credentials stopped working; ask for new ones."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Validate replacement credentials for the existing entry."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**entry.data, **user_input}
            device, errors = await self._async_validate(data)
            if device is not None:
                await self.async_set_unique_id(device.serial_number)
                self._abort_if_unique_id_mismatch(reason="wrong_device")
                return self.async_update_reload_and_abort(entry, data_updates=user_input)

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_USERNAME,
                        default=(user_input or entry.data).get(CONF_USERNAME, vol.UNDEFINED),
                    ): str,
                    vol.Required(CONF_PASSWORD): _PASSWORD_SELECTOR,
                }
            ),
            description_placeholders={"host": entry.data[CONF_HOST]},
            errors=errors,
        )
