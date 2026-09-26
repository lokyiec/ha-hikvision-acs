"""Tests for the config flow."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_SSL, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hikvision_acs.api import (
    HikvisionAuthError,
    HikvisionConnectionError,
    HikvisionPermissionError,
    HikvisionResponseError,
)
from custom_components.hikvision_acs.const import DOMAIN
from custom_components.hikvision_acs.parser import DeviceIdentity

from .conftest import ENTRY_DATA
from .helpers import DEVICE, FakeClient

USER_INPUT: dict[str, Any] = {**ENTRY_DATA, CONF_HOST: " 192.0.2.10 ", CONF_PORT: 443.0}


async def _start(hass: HomeAssistant) -> Any:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return result


async def test_user_flow_creates_entry(hass: HomeAssistant, fake_client: FakeClient) -> None:
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Access Control Device"
    assert result["result"].unique_id == DEVICE.serial_number
    # Host is trimmed and the number selector's float becomes an int.
    assert result["data"] == ENTRY_DATA


async def test_title_falls_back_to_model_then_host(
    hass: HomeAssistant, fake_client: FakeClient
) -> None:
    fake_client.get_device_info.return_value = DeviceIdentity(serial_number="X1")
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["title"] == "192.0.2.10"


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (HikvisionAuthError("x"), "invalid_auth"),
        (HikvisionPermissionError("x"), "invalid_auth"),
        (HikvisionConnectionError("x"), "cannot_connect"),
        (HikvisionResponseError("x"), "unexpected_response"),
        (RuntimeError("boom"), "unknown"),
    ],
)
async def test_user_flow_errors_then_recovers(
    hass: HomeAssistant, fake_client: FakeClient, error: Exception, code: str
) -> None:
    fake_client.get_device_info.side_effect = error
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": code}
    # One attempt per submit; the flow never retries a rejected login.
    assert fake_client.get_device_info.await_count == 1

    fake_client.get_device_info.side_effect = None
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_same_device_new_address_updates_entry(
    hass: HomeAssistant, config_entry: MockConfigEntry, fake_client: FakeClient
) -> None:
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {**USER_INPUT, CONF_HOST: "192.0.2.99", CONF_PORT: 80, CONF_SSL: False},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert config_entry.data[CONF_HOST] == "192.0.2.99"
    assert config_entry.data[CONF_PORT] == 80
    assert config_entry.data[CONF_SSL] is False


async def test_reauth_success(
    hass: HomeAssistant, config_entry: MockConfigEntry, fake_client: FakeClient
) -> None:
    result = await config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    assert result["description_placeholders"]["host"] == "192.0.2.10"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: "new-user", CONF_PASSWORD: "new-pass"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data[CONF_USERNAME] == "new-user"
    assert config_entry.data[CONF_PASSWORD] == "new-pass"
    # The entry is reloaded with the new credentials.
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.LOADED
    assert await hass.config_entries.async_unload(config_entry.entry_id)


async def test_reauth_invalid_auth_shows_error(
    hass: HomeAssistant, config_entry: MockConfigEntry, fake_client: FakeClient
) -> None:
    fake_client.get_device_info.side_effect = HikvisionAuthError("x")
    result = await config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: "u", CONF_PASSWORD: "bad"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
    assert config_entry.data[CONF_PASSWORD] == ENTRY_DATA[CONF_PASSWORD]


async def test_reauth_wrong_device(
    hass: HomeAssistant, config_entry: MockConfigEntry, fake_client: FakeClient
) -> None:
    fake_client.get_device_info.return_value = DeviceIdentity(serial_number="OTHER")
    result = await config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: "u", CONF_PASSWORD: "p"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_device"
