"""Home EMS: energy management for the heat pump, appliances and (later) a battery.

Version 0.1 runs in observation mode only: it reads sensors and publishes what it
would do, but never writes to devices.
"""
from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .coordinator import HomeEmsConfigEntry, HomeEmsCoordinator

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: HomeEmsConfigEntry) -> bool:
    coordinator = HomeEmsCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: HomeEmsConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
