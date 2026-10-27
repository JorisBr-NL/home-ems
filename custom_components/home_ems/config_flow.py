"""Config flow for Home EMS: pick the entities the EMS reads."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
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
    OPT_BASE_LOAD,
    OPT_COMFORT_SETPOINT,
    OPT_FLOOR_MIN_MARGIN,
    OPT_FLOOR_RULE_BAND,
    OPT_MAX_GAP,
    OPT_MIN_RUN,
    OPT_PEAK_END,
    OPT_PEAK_START,
    OPT_PRICE_ENTITY,
    OPT_PV_RUN_THRESHOLD,
    OPT_SOLAR_ENTITY,
    OPT_WEATHER_ENTITY,
    OPTION_DEFAULTS,
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

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return HomeEmsOptionsFlow()


def _number(min_: float, max_: float, step: float | str, unit: str | None = None) -> selector.NumberSelector:
    # HA only accepts a numeric step >= 0.001 (or "any")
    config = selector.NumberSelectorConfig(min=min_, max=max_, step=step,
                                           mode=selector.NumberSelectorMode.BOX)
    if unit:
        config["unit_of_measurement"] = unit
    return selector.NumberSelector(config)


class HomeEmsOptionsFlow(OptionsFlow):
    """Data sources for the planner and the thresholds of the advice."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        current = {**OPTION_DEFAULTS, **self.config_entry.options}

        def entity(key: str) -> vol.Optional:
            # suggested_value instead of default, so an entity can also be cleared
            return vol.Optional(key, description={"suggested_value": current.get(key)})

        schema = vol.Schema({
            entity(OPT_PRICE_ENTITY): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
            entity(OPT_SOLAR_ENTITY): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
            entity(OPT_WEATHER_ENTITY): selector.EntitySelector(selector.EntitySelectorConfig(domain="weather")),
            vol.Required(OPT_COMFORT_SETPOINT, default=current[OPT_COMFORT_SETPOINT]): _number(15, 23, 0.1, "°C"),
            vol.Required(OPT_FLOOR_RULE_BAND, default=current[OPT_FLOOR_RULE_BAND]): _number(0, 3, 0.1, "K"),
            vol.Required(OPT_FLOOR_MIN_MARGIN, default=current[OPT_FLOOR_MIN_MARGIN]): _number(-3, 5, 0.1, "K"),
            vol.Required(OPT_PV_RUN_THRESHOLD, default=current[OPT_PV_RUN_THRESHOLD]): _number(0, 5000, 50, "W"),
            vol.Required(OPT_MIN_RUN, default=current[OPT_MIN_RUN]): _number(0, 180, 5, "min"),
            vol.Required(OPT_PEAK_START, default=current[OPT_PEAK_START]): selector.TimeSelector(),
            vol.Required(OPT_PEAK_END, default=current[OPT_PEAK_END]): selector.TimeSelector(),
            vol.Required(OPT_MAX_GAP, default=current[OPT_MAX_GAP]): _number(2, 24, 0.5, "h"),
            vol.Required(OPT_BASE_LOAD, default=current[OPT_BASE_LOAD]): _number(0, 3, 0.05, "kW"),
        })
        return self.async_show_form(step_id="init", data_schema=schema)
