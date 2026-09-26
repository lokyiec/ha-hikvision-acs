"""Fixtures for the Hikvision Access Control tests."""

from __future__ import annotations

from collections.abc import Generator
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_SSL,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
)
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hikvision_acs.const import DOMAIN

from .helpers import DEVICE, FakeClient

ENTRY_DATA: dict[str, Any] = {
    CONF_HOST: "192.0.2.10",
    CONF_PORT: 443,
    CONF_SSL: True,
    CONF_VERIFY_SSL: False,
    CONF_USERNAME: "ha-integration",
    CONF_PASSWORD: "test-password",
}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load custom_components/ in every test."""


@pytest.fixture
def fake_client() -> Generator[FakeClient]:
    """Patch the client constructor used by setup and the config flow."""
    client = FakeClient()
    with patch("custom_components.hikvision_acs.HikvisionClient", return_value=client):
        yield client


@pytest.fixture
def config_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Access Control Device",
        data=ENTRY_DATA,
        unique_id=DEVICE.serial_number,
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
async def setup_integration(
    hass: HomeAssistant, config_entry: MockConfigEntry, fake_client: FakeClient
) -> MockConfigEntry:
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return config_entry
