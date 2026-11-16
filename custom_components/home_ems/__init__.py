"""Home EMS: energy management for the heat pump, appliances and (later) a battery.

From version 0.6 it can write its target setpoint to the living room thermostat, but
only while the 'Control' switch is on (off by default); otherwise it only publishes
what it would do (advice, plan, target setpoint).
"""
from __future__ import annotations

import voluptuous as vol

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN, NAME
from .coordinator import HomeEmsConfigEntry, HomeEmsCoordinator

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR, Platform.SWITCH]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SERVICE_APPLIANCE_ANSWER = "appliance_answer"
APPLIANCE_ANSWER_SCHEMA = vol.Schema({
    vol.Required("name"): cv.string,
    vol.Required("approve"): cv.boolean,
})


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """The answer to "finishing later is better" for a waiting appliance (v0.13), for
    example from the action buttons of a push message."""

    async def answer(call: ServiceCall) -> None:
        for entry in hass.config_entries.async_loaded_entries(DOMAIN):
            await entry.runtime_data.async_answer_appliance(call.data["name"], call.data["approve"])

    hass.services.async_register(DOMAIN, SERVICE_APPLIANCE_ANSWER, answer,
                                 schema=APPLIANCE_ANSWER_SCHEMA)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: HomeEmsConfigEntry) -> bool:
    coordinator = HomeEmsCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    # new settings take effect right away (and the plan is made again)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    # the main device first: the sub-devices (planner, solar, battery, overview) hang under it
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, entry.entry_id)}, name=NAME,
        entry_type=dr.DeviceEntryType.SERVICE)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_reload(hass: HomeAssistant, entry: HomeEmsConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: HomeEmsConfigEntry) -> bool:
    # keep the run state and the solar boost for the reload that follows (saving options)
    await entry.runtime_data.async_save_now()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
