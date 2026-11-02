"""Home EMS: energy management for the heat pump, appliances and (later) a battery.

From version 0.6 it can write its target setpoint to the living room thermostat, but
only while the 'Control' switch is on (off by default); otherwise it only publishes
what it would do (advice, plan, target setpoint).
"""
from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .coordinator import HomeEmsConfigEntry, HomeEmsCoordinator

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR, Platform.SWITCH]


async def async_setup_entry(hass: HomeAssistant, entry: HomeEmsConfigEntry) -> bool:
    coordinator = HomeEmsCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    # new settings take effect right away (and the plan is made again)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_reload(hass: HomeAssistant, entry: HomeEmsConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: HomeEmsConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
