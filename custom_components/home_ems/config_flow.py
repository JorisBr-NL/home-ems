"""Config flow for Home EMS: pick the entities the EMS reads."""
from __future__ import annotations

from typing import Any

import voluptuous as vol
import yaml

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
    COST_SETTINGS_EXAMPLE,
    DOMAIN,
    NAME,
    OPT_BASE_LOAD,
    OPT_COMFORT_SETPOINT,
    OPT_COST_SETTINGS,
    OPT_EXPORT_DIR,
    OPT_FLOOR_MIN_MARGIN,
    OPT_FLOOR_RULE_BAND,
    OPT_MAX_GAP,
    OPT_MIN_RUN,
    OPT_OVERVIEW_FROM,
    OPT_PEAK_END,
    OPT_PEAK_START,
    OPT_PRICE_ENTITY,
    OPT_PV_RUN_THRESHOLD,
    OPT_SOLAR_ENTITY,
    OPT_STAT_EV,
    OPT_STAT_EXPORT,
    OPT_STAT_EXPORT_REVENUE,
    OPT_STAT_GAS,
    OPT_STAT_HEAT_PUMP,
    OPT_STAT_IMPORT,
    OPT_STAT_IMPORT_COST,
    OPT_STAT_OUTSIDE,
    OPT_STAT_PV,
    OPT_WEATHER_ENTITY,
    OPTION_DEFAULTS,
    OVERVIEW_OPTIONS,
)
from .overview import parse_settings

PLANNER_OPTIONS = (OPT_PRICE_ENTITY, OPT_SOLAR_ENTITY, OPT_WEATHER_ENTITY, *OPTION_DEFAULTS)

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


def _optional(current: dict[str, Any], key: str) -> vol.Optional:
    # suggested_value instead of default, so a value can also be cleared
    return vol.Optional(key, description={"suggested_value": current.get(key)})


def _statistic(multiple: bool = False) -> selector.StatisticSelector:
    return selector.StatisticSelector(selector.StatisticSelectorConfig(multiple=multiple))


class HomeEmsOptionsFlow(OptionsFlow):
    """Two screens: the planner and advice, and the energy and cost overview."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="init", menu_options=["planner", "overview"])

    def _save(self, keys: tuple[str, ...], user_input: dict[str, Any]) -> ConfigFlowResult:
        """Keep the options of the other screen; cleared fields are dropped."""
        options = {k: v for k, v in self.config_entry.options.items() if k not in keys}
        return self.async_create_entry(data={**options, **user_input})

    async def async_step_overview(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                parse_settings(yaml.safe_load(user_input.get(OPT_COST_SETTINGS) or "") or {})
            except (yaml.YAMLError, ValueError):
                errors[OPT_COST_SETTINGS] = "invalid_cost_settings"
            else:
                return self._save(OVERVIEW_OPTIONS, user_input)
        current = {OPT_COST_SETTINGS: COST_SETTINGS_EXAMPLE, OPT_EXPORT_DIR: "/share/energie",
                   **self.config_entry.options, **(user_input or {})}
        schema = vol.Schema({
            _optional(current, OPT_STAT_IMPORT): _statistic(multiple=True),
            _optional(current, OPT_STAT_EXPORT): _statistic(multiple=True),
            _optional(current, OPT_STAT_PV): _statistic(multiple=True),
            _optional(current, OPT_STAT_GAS): _statistic(),
            _optional(current, OPT_STAT_HEAT_PUMP): _statistic(multiple=True),
            _optional(current, OPT_STAT_EV): _statistic(),
            _optional(current, OPT_STAT_OUTSIDE): _statistic(),
            _optional(current, OPT_STAT_IMPORT_COST): _statistic(),
            _optional(current, OPT_STAT_EXPORT_REVENUE): _statistic(),
            _optional(current, OPT_OVERVIEW_FROM): selector.DateSelector(),
            _optional(current, OPT_COST_SETTINGS): selector.TextSelector(
                selector.TextSelectorConfig(multiline=True)),
            _optional(current, OPT_EXPORT_DIR): selector.TextSelector(),
        })
        return self.async_show_form(step_id="overview", data_schema=schema, errors=errors)

    async def async_step_planner(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self._save(PLANNER_OPTIONS, user_input)
        current = {**OPTION_DEFAULTS, **self.config_entry.options}

        def entity(key: str) -> vol.Optional:
            return _optional(current, key)

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
        return self.async_show_form(step_id="planner", data_schema=schema)
