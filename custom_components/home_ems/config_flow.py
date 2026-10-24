"""Config flow for Home EMS: pick the entities the EMS reads."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers import selector

from .const import (
    CONF_CHILL_CLIMATE,
    CONF_FLOW_RATE,
    CONF_FLOW_TEMPERATURE,
    CONF_GRID_POWER,
    CONF_HEAT_PUMP_POWER,
    CONF_KITCHEN_CLIMATE,
    CONF_LIVING_CLIMATE,
    CONF_OUTSIDE_TEMPERATURE,
    DOMAIN,
    NAME,
)

CLIMATE = selector.EntitySelector(selector.EntitySelectorConfig(domain="climate"))
POWER = selector.EntitySelector(
    selector.EntitySelectorConfig(domain="sensor", device_class="power")
)
TEMPERATURE = selector.EntitySelector(
    selector.EntitySelectorConfig(domain="sensor", device_class="temperature")
)

SCHEMA = vol.Schema(
    {
        vol.Required(CONF_LIVING_CLIMATE): CLIMATE,
        vol.Optional(CONF_KITCHEN_CLIMATE): CLIMATE,
        vol.Required(CONF_HEAT_PUMP_POWER): selector.EntitySelector(
            selector.EntitySelectorConfig(
                domain="sensor", device_class="power", multiple=True
            )
        ),
        vol.Required(CONF_FLOW_TEMPERATURE): TEMPERATURE,
        vol.Optional(CONF_FLOW_RATE): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor")
        ),
        vol.Required(CONF_GRID_POWER): POWER,
        vol.Optional(CONF_OUTSIDE_TEMPERATURE): TEMPERATURE,
        vol.Optional(CONF_CHILL_CLIMATE): CLIMATE,
    }
)


class HomeEmsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Single-instance config flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        if user_input is not None:
            return self.async_create_entry(title=NAME, data=user_input)
        return self.async_show_form(step_id="user", data_schema=SCHEMA)
