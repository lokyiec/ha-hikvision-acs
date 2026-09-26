"""Hikvision Access Control integration (local ISAPI push)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_SSL,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    HikvisionAuthError,
    HikvisionClient,
    HikvisionConnectionError,
    HikvisionError,
    HikvisionPermissionError,
)
from .const import DEFAULT_SSL, DEFAULT_VERIFY_SSL, DOMAIN, issue_id_stream_permission
from .coordinator import HikvisionEventStream
from .parser import DeviceIdentity

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.EVENT,
    Platform.SENSOR,
]


@dataclass(slots=True)
class HikvisionRuntimeData:
    """Per-entry objects shared by the platforms."""

    client: HikvisionClient
    device: DeviceIdentity
    stream: HikvisionEventStream


type HikvisionConfigEntry = ConfigEntry[HikvisionRuntimeData]


def create_client(hass: HomeAssistant, data: Mapping[str, Any]) -> HikvisionClient:
    """Build a client from config entry (or config flow) data."""
    verify_ssl = bool(data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL))
    return HikvisionClient(
        async_get_clientsession(hass, verify_ssl=verify_ssl),
        host=data[CONF_HOST],
        port=int(data[CONF_PORT]),
        username=data[CONF_USERNAME],
        password=data[CONF_PASSWORD],
        ssl=bool(data.get(CONF_SSL, DEFAULT_SSL)),
        verify_ssl=verify_ssl,
    )


async def async_setup_entry(hass: HomeAssistant, entry: HikvisionConfigEntry) -> bool:
    """Connect to the device, create entities and start the event stream."""
    client = create_client(hass, entry.data)
    try:
        device = await client.get_device_info()
    except (HikvisionAuthError, HikvisionPermissionError) as err:
        raise ConfigEntryAuthFailed(
            translation_domain=DOMAIN, translation_key="invalid_auth"
        ) from err
    except HikvisionConnectionError as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN, translation_key="cannot_connect"
        ) from err
    except HikvisionError as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN, translation_key="unexpected_response"
        ) from err

    # A reload means the user may have fixed permissions; the stream raises
    # the issue again if not.
    ir.async_delete_issue(hass, DOMAIN, issue_id_stream_permission(entry.entry_id))

    stream = HikvisionEventStream(hass, entry, client, device)
    entry.runtime_data = HikvisionRuntimeData(client=client, device=device, stream=stream)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    stream.async_start()
    entry.async_on_unload(stream.async_stop)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: HikvisionConfigEntry) -> bool:
    """Unload platforms; the stream task is stopped via ``async_on_unload``."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
