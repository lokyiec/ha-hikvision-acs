"""Tests for config entry setup and unload."""

from __future__ import annotations

import pytest
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hikvision_acs.api import (
    HikvisionAuthError,
    HikvisionConnectionError,
    HikvisionPermissionError,
    HikvisionResponseError,
)
from custom_components.hikvision_acs.const import DOMAIN, issue_id_stream_permission

from .helpers import DEVICE, FakeClient


async def test_setup_creates_device_and_entities(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    assert setup_integration.state is ConfigEntryState.LOADED

    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, DEVICE.serial_number), setup_integration.entry_id
    )
    assert device is not None
    assert device.manufacturer == "Hikvision"
    assert device.model == "DS-K1T805MBFWX"
    assert device.sw_version == "V1.9.1 build 240909"
    assert device.serial_number == DEVICE.serial_number
    assert (dr.CONNECTION_NETWORK_MAC, "00:00:5e:00:53:01") in device.connections

    entities = er.async_entries_for_config_entry(er.async_get(hass), setup_integration.entry_id)
    assert sorted(e.unique_id for e in entities) == sorted(
        f"{DEVICE.serial_number}_{key}"
        for key in ("access", "access_granted", "open_door", "last_access_event")
    )


async def test_setup_clears_stale_permission_issue(
    hass: HomeAssistant, config_entry: MockConfigEntry, fake_client: FakeClient
) -> None:
    issue_id = issue_id_stream_permission(config_entry.entry_id)
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key="stream_permission_denied",
    )
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_unload(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_client: FakeClient
) -> None:
    assert await hass.config_entries.async_unload(setup_integration.entry_id)
    await hass.async_block_till_done()
    assert setup_integration.state is ConfigEntryState.NOT_LOADED


@pytest.mark.parametrize(
    ("error", "state"),
    [
        (HikvisionConnectionError("down"), ConfigEntryState.SETUP_RETRY),
        (HikvisionResponseError("weird"), ConfigEntryState.SETUP_RETRY),
        (HikvisionAuthError("no"), ConfigEntryState.SETUP_ERROR),
        (HikvisionPermissionError("no"), ConfigEntryState.SETUP_ERROR),
    ],
)
async def test_setup_failures(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_client: FakeClient,
    error: Exception,
    state: ConfigEntryState,
) -> None:
    fake_client.get_device_info.side_effect = error
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is state
    # Exactly one login attempt: never hammer a device that locks accounts.
    assert fake_client.get_device_info.await_count == 1
    reauth = [
        f
        for f in hass.config_entries.flow.async_progress()
        if f["context"]["source"] == SOURCE_REAUTH
    ]
    assert bool(reauth) is (state is ConfigEntryState.SETUP_ERROR)
