"""Config flow for Home EMS: pick the entities the EMS reads."""
from __future__ import annotations

from typing import Any

import voluptuous as vol
import yaml

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers import selector

from .const import (
    APPLIANCES_EXAMPLE,
    BEDROOMS_EXAMPLE,
    BEDROOM_DEFAULTS,
    BEDROOM_OPTIONS,
    CONF_BATTERY_CHARGE_POSITIVE,
    CONF_BATTERY_POWER,
    CONF_CHILL_CLIMATE,
    CONF_FLOW_RATE,
    CONF_FLOW_TEMPERATURE,
    CONF_GRID_POWER,
    CONF_HEAT_PUMP_POWER,
    CONF_KITCHEN_CLIMATE,
    CONF_LIVING_CLIMATE,
    CONF_OUTSIDE_TEMPERATURE,
    CONTROL_DEFAULTS,
    CONTROL_OPTIONS,
    COST_SETTINGS_EXAMPLE,
    DOMAIN,
    NAME,
    OPTION_DEFAULTS,
    OPT_APPLIANCES,
    OPT_BASE_LOAD,
    OPT_BOOST_MARGIN,
    OPT_BEDROOMS,
    OPT_BEDROOM_LEAD,
    OPT_BEDROOM_MIN,
    OPT_CAPACITY,
    OPT_COMFORT_MAX,
    OPT_COMFORT_MIN,
    OPT_COMFORT_SETPOINT,
    OPT_COST_SETTINGS,
    OPT_EXPENSIVE_SHARE,
    OPT_EXPORT_DIR,
    OPT_FLOOR_MIN_MARGIN,
    OPT_GAP_MAX,
    OPT_FLOOR_RULE_BAND,
    OPT_GAS_ENTITY,
    OPT_LOW_PRICE,
    OPT_MAX_GAP,
    OPT_MIN_PAUSE,
    OPT_MIN_RUN,
    OPT_OVERRIDE_HOURS,
    OPT_OVERVIEW_FROM,
    OPT_PRICE_ENTITY,
    OPT_PV_POWER,
    OPT_PV_RUN_THRESHOLD,
    OPT_HEAT_RATE,
    OPT_HEAT_RATE_SLOPE,
    OPT_LIVING_MIN,
    OPT_NIGHT_CEILING,
    OPT_OFF_MARGIN,
    OPT_START_BAND,
    OPT_NIGHT_END,
    OPT_NIGHT_START,
    OPT_RUN_HEADROOM,
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
    OPT_WRITE_INTERVAL,
    OPT_WRITE_STEP,
    OVERVIEW_OPTIONS,
    PV_APPLIANCE_OPTIONS,
)
from .appliances import parse_appliances
from .bedrooms import parse_rooms
from .overview import parse_settings

PLANNER_OPTIONS = (OPT_PRICE_ENTITY, OPT_SOLAR_ENTITY, OPT_WEATHER_ENTITY, OPT_GAS_ENTITY, *OPTION_DEFAULTS)

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
        vol.Optional(CONF_BATTERY_POWER): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor", device_class="power", multiple=True)
        ),
        vol.Optional(CONF_BATTERY_CHARGE_POSITIVE, default=True): selector.BooleanSelector(),
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

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the entities (for example another thermostat entity) without removing
        the integration: the options stay as they are."""
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            return self.async_update_reload_and_abort(entry, data=user_input)
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(SCHEMA, entry.data),
        )

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
    """For statistics that are not sensors (e.g. the cost statistics of Tibber)."""
    return selector.StatisticSelector(selector.StatisticSelectorConfig(multiple=multiple))


def _meter(multiple: bool = False) -> selector.EntitySelector:
    """A sensor with long-term statistics; its entity id is also the statistic id."""
    return selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor", multiple=multiple))


def _flatten(user_input: dict[str, Any]) -> dict[str, Any]:
    """Form sections arrive as nested dicts; the options stay flat."""
    out: dict[str, Any] = {}
    for key, value in user_input.items():
        if isinstance(value, dict):
            out.update(value)
        else:
            out[key] = value
    return out


class HomeEmsOptionsFlow(OptionsFlow):
    """Five screens: the planner and advice, control (the executor), the bedrooms, PV
    curtailment and appliances, and the energy and cost overview."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="init", menu_options=["planner", "control", "bedrooms", "pv_appliances", "overview"])

    def _save(self, keys: tuple[str, ...], user_input: dict[str, Any]) -> ConfigFlowResult:
        """Keep the options of the other screen; cleared fields are dropped."""
        options = {k: v for k, v in self.config_entry.options.items() if k not in keys}
        return self.async_create_entry(data={**options, **user_input})

    async def async_step_control(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self._save(CONTROL_OPTIONS, user_input)
        current = {**CONTROL_DEFAULTS, **self.config_entry.options}
        schema = vol.Schema({
            vol.Required(OPT_OVERRIDE_HOURS, default=current[OPT_OVERRIDE_HOURS]): _number(0, 24, 0.5, "h"),
            vol.Required(OPT_WRITE_INTERVAL, default=current[OPT_WRITE_INTERVAL]): _number(1, 60, 1, "min"),
            vol.Required(OPT_WRITE_STEP, default=current[OPT_WRITE_STEP]): _number(0.1, 1, 0.1, "K"),
        })
        return self.async_show_form(step_id="control", data_schema=schema)

    async def async_step_bedrooms(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                parse_rooms(yaml.safe_load(user_input.get(OPT_BEDROOMS) or "") or [])
            except (yaml.YAMLError, ValueError):
                errors[OPT_BEDROOMS] = "invalid_bedrooms"
            else:
                return self._save(BEDROOM_OPTIONS, user_input)
        current = {OPT_BEDROOMS: BEDROOMS_EXAMPLE, **BEDROOM_DEFAULTS, **self.config_entry.options}
        schema = vol.Schema({
            _optional(current, OPT_BEDROOMS): selector.TextSelector(
                selector.TextSelectorConfig(multiline=True)),
            vol.Required(OPT_BEDROOM_MIN, default=current[OPT_BEDROOM_MIN]): _number(5, 18, 0.5, "°C"),
            vol.Required(OPT_BEDROOM_LEAD, default=current[OPT_BEDROOM_LEAD]): _number(1, 12, 0.5, "h"),
        })
        return self.async_show_form(step_id="bedrooms", data_schema=schema, errors=errors)

    async def async_step_pv_appliances(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                parse_appliances(yaml.safe_load(user_input.get(OPT_APPLIANCES) or "") or [])
            except (yaml.YAMLError, ValueError):
                errors[OPT_APPLIANCES] = "invalid_appliances"
            else:
                return self._save(PV_APPLIANCE_OPTIONS, user_input)
        current = {OPT_APPLIANCES: APPLIANCES_EXAMPLE, **self.config_entry.options}
        schema = vol.Schema({
            _optional(current, OPT_PV_POWER): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor", device_class="power", multiple=True)),
            _optional(current, OPT_APPLIANCES): selector.TextSelector(
                selector.TextSelectorConfig(multiline=True)),
        })
        return self.async_show_form(step_id="pv_appliances", data_schema=schema, errors=errors)

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
            _optional(current, OPT_STAT_IMPORT): _meter(multiple=True),
            _optional(current, OPT_STAT_EXPORT): _meter(multiple=True),
            _optional(current, OPT_STAT_PV): _meter(multiple=True),
            _optional(current, OPT_STAT_GAS): _meter(),
            _optional(current, OPT_STAT_HEAT_PUMP): _meter(multiple=True),
            _optional(current, OPT_STAT_EV): _meter(),
            _optional(current, OPT_STAT_OUTSIDE): _meter(),
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
            return self._save(PLANNER_OPTIONS, _flatten(user_input))
        current = {**OPTION_DEFAULTS, **self.config_entry.options}

        def number(key: str, min_: float, max_: float, step: float, unit: str) -> tuple:
            return vol.Required(key, default=current[key]), _number(min_, max_, step, unit)

        def entity(key: str, domain: str = "sensor") -> tuple:
            return (_optional(current, key),
                    selector.EntitySelector(selector.EntitySelectorConfig(domain=domain)))

        def time(key: str) -> tuple:
            return vol.Required(key, default=current[key]), selector.TimeSelector()

        groups: dict[str, tuple[bool, list[tuple]]] = {
            "temperatures": (False, [
                number(OPT_COMFORT_SETPOINT, 15, 23, 0.1, "°C"),
                number(OPT_RUN_HEADROOM, 0, 3, 0.1, "K"),
                number(OPT_COMFORT_MAX, 19, 24, 0.1, "°C"),
                number(OPT_COMFORT_MIN, 15, 21, 0.1, "°C"),
                number(OPT_OFF_MARGIN, 0.2, 3, 0.1, "K"),
            ]),
            "night": (False, [
                number(OPT_LIVING_MIN, 15, 21, 0.1, "°C"),
                number(OPT_NIGHT_CEILING, 0, 23, 0.1, "°C"),
                time(OPT_NIGHT_START),
                time(OPT_NIGHT_END),
            ]),
            "runs": (True, [
                number(OPT_MIN_RUN, 0, 180, 5, "min"),
                number(OPT_START_BAND, 0, 1.5, 0.1, "K"),
                number(OPT_MIN_PAUSE, 0, 8, 0.25, "h"),
                number(OPT_MAX_GAP, 2, 24, 0.5, "h"),
            ]),
            "solar_price": (True, [
                number(OPT_PV_RUN_THRESHOLD, 0, 5000, 50, "W"),
                number(OPT_BOOST_MARGIN, 0, 2000, 10, "W"),
                number(OPT_GAP_MAX, 0.1, 2, 0.1, "K"),
                number(OPT_LOW_PRICE, 0, 1, 0.01, "EUR/kWh"),
                number(OPT_EXPENSIVE_SHARE, 0, 60, 5, "%"),
            ]),
            "floor": (True, [
                number(OPT_FLOOR_RULE_BAND, 0, 3, 0.1, "K"),
                number(OPT_FLOOR_MIN_MARGIN, -3, 5, 0.1, "K"),
            ]),
            "house_model": (True, [
                number(OPT_HEAT_RATE, 0.5, 20, 0.1, "kW"),
                number(OPT_HEAT_RATE_SLOPE, 0, 2, 0.05, "kW/°C"),
                number(OPT_CAPACITY, 0, 100, 0.5, "kWh/K"),
                number(OPT_BASE_LOAD, 0, 3, 0.05, "kW"),
            ]),
            "sources": (True, [
                entity(OPT_PRICE_ENTITY),
                entity(OPT_SOLAR_ENTITY),
                entity(OPT_WEATHER_ENTITY, "weather"),
                entity(OPT_GAS_ENTITY),
            ]),
        }
        schema = vol.Schema({
            vol.Required(name): section(vol.Schema(dict(fields)), {"collapsed": collapsed})
            for name, (collapsed, fields) in groups.items()
        })
        return self.async_show_form(step_id="planner", data_schema=schema)
