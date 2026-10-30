"""Data coordinator for Home EMS: reads sensors and evaluates the model."""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import yaml

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    CONF_FLOW_TEMPERATURE,
    CONF_GRID_POWER,
    CONF_HEAT_PUMP_POWER,
    CONF_LIVING_CLIMATE,
    CONF_OUTSIDE_TEMPERATURE,
    DOMAIN,
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
    OVERVIEW_BAD_SETTINGS,
    OVERVIEW_NO_DATA,
    OVERVIEW_NOT_CONFIGURED,
    OVERVIEW_OK,
    OVERVIEW_REFRESH,
    PLAN_NO_PRICES,
    PLAN_NO_TEMPERATURES,
    PLAN_OK,
    REPLAN_INTERVAL,
    UPDATE_INTERVAL,
    WEATHER_REFRESH,
)
from .model import Evaluation, Inputs, RunTracker, Settings, evaluate
from .overview import Usage, YearSummary, build_overview, parse_settings, to_csv, years_csv
from .planner import Plan, PlannerSettings, Slot, build_slots, hour_of, plan

_LOGGER = logging.getLogger(__name__)

# overview role -> option with the statistic ids
STAT_ROLES = {
    "import": OPT_STAT_IMPORT, "export": OPT_STAT_EXPORT, "pv": OPT_STAT_PV, "gas": OPT_STAT_GAS,
    "hp": OPT_STAT_HEAT_PUMP, "ev": OPT_STAT_EV, "temp": OPT_STAT_OUTSIDE,
    "import_cost": OPT_STAT_IMPORT_COST, "export_revenue": OPT_STAT_EXPORT_REVENUE,
}


def _row_day(start: float | datetime) -> date:
    """Local day of a statistics row (start is a UTC timestamp, a datetime in older HA)."""
    moment = start if isinstance(start, datetime) else dt_util.utc_from_timestamp(start)
    return dt_util.as_local(moment).date()


def _write_files(folder: str, files: dict[str, str]) -> None:
    path = Path(folder)
    if not path.is_dir():
        _LOGGER.warning("Folder for the overview CSV files does not exist: %s", folder)
        return
    for name, text in files.items():
        # utf-8 with BOM so Excel shows m³ and € correctly
        (path / name).write_text(text, encoding="utf-8-sig")

STORAGE_VERSION = 1
SAVE_DELAY = 60  # seconds

type HomeEmsConfigEntry = ConfigEntry[HomeEmsCoordinator]


class HomeEmsCoordinator(DataUpdateCoordinator[Evaluation]):
    """Polls the configured entities and keeps the run tracker."""

    config_entry: HomeEmsConfigEntry

    def __init__(self, hass: HomeAssistant, entry: HomeEmsConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
        self.tracker = RunTracker()
        self._store: Store[dict] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.tracker"
        )
        self.plan: Plan | None = None
        self.plan_status = PLAN_NO_PRICES
        self._temps: dict[datetime, float] = {}   # hour start (UTC) -> forecast temperature
        self._temps_fetched: datetime | None = None
        self.overview: list[YearSummary] = []
        self.overview_status = OVERVIEW_NOT_CONFIGURED
        self._overview_made: datetime | None = None

    # ------------------------------------------------------------ settings

    def option(self, key: str) -> Any:
        return self.config_entry.options.get(key, OPTION_DEFAULTS.get(key))

    def settings(self) -> Settings:
        return Settings(
            min_run=timedelta(minutes=float(self.option(OPT_MIN_RUN))),
            floor_min_margin=float(self.option(OPT_FLOOR_MIN_MARGIN)),
            pv_run_threshold_w=float(self.option(OPT_PV_RUN_THRESHOLD)),
            peak_start=time.fromisoformat(self.option(OPT_PEAK_START)),
            peak_end=time.fromisoformat(self.option(OPT_PEAK_END)),
            comfort_setpoint=float(self.option(OPT_COMFORT_SETPOINT)),
            floor_rule_band=float(self.option(OPT_FLOOR_RULE_BAND)),
        )

    def planner_settings(self) -> PlannerSettings:
        return PlannerSettings(
            base_load_kw=float(self.option(OPT_BASE_LOAD)),
            max_gap=timedelta(hours=float(self.option(OPT_MAX_GAP))),
            peak_start=time.fromisoformat(self.option(OPT_PEAK_START)),
            peak_end=time.fromisoformat(self.option(OPT_PEAK_END)),
            tz=dt_util.get_default_time_zone(),
        )

    async def _async_setup(self) -> None:
        """Restore run state and day counters from the previous run of HA."""
        stored = await self._store.async_load()
        if stored:
            self.tracker = RunTracker.from_dict(stored, dt_util.now())

    def _float_state(self, entity_id: str | None) -> float | None:
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None
        try:
            return float(state.state)
        except ValueError:
            return None

    def _climate_temperature(self, entity_id: str | None) -> float | None:
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None:
            return None
        value = state.attributes.get("current_temperature")
        return float(value) if value is not None else None

    def _heat_pump_power(self) -> float | None:
        values = [
            self._float_state(entity_id)
            for entity_id in self.config_entry.data.get(CONF_HEAT_PUMP_POWER, [])
        ]
        known = [v for v in values if v is not None]
        return sum(known) if known else None

    def read_inputs(self) -> Inputs:
        data = self.config_entry.data
        return Inputs(
            heat_pump_power_w=self._heat_pump_power(),
            flow_temp=self._float_state(data.get(CONF_FLOW_TEMPERATURE)),
            living_temp=self._climate_temperature(data.get(CONF_LIVING_CLIMATE)),
            grid_power_w=self._float_state(data.get(CONF_GRID_POWER)),
        )

    async def _async_update_data(self) -> Evaluation:
        now = dt_util.now()
        settings = self.settings()
        self.tracker.settings = settings
        evaluation = evaluate(self.read_inputs(), self.tracker, now, settings)
        self._store.async_delay_save(self.tracker.to_dict, SAVE_DELAY)
        if self.plan is None or now - self.plan.made >= REPLAN_INTERVAL:
            await self._async_replan(now)
        if self._overview_made is None or now - self._overview_made >= OVERVIEW_REFRESH:
            self._overview_made = now
            try:
                await self._async_refresh_overview(now)
            except Exception:  # the overview must never stop the advice and the planner
                _LOGGER.exception("Could not build the energy overview")
        return evaluation

    # ------------------------------------------------------------ planner

    async def _async_replan(self, now: datetime) -> None:
        await self._async_refresh_temperatures(now)
        slots = self.build_slots()
        if not slots:
            self.plan, self.plan_status = None, PLAN_NO_PRICES
            return
        self.plan = plan(slots, now, self.planner_settings(), self.tracker.last_run_end)
        self.plan_status = PLAN_OK if self._temps else PLAN_NO_TEMPERATURES

    async def _async_refresh_temperatures(self, now: datetime) -> None:
        weather = self.option(OPT_WEATHER_ENTITY)
        if not weather or (self._temps_fetched and now - self._temps_fetched < WEATHER_REFRESH):
            return
        try:
            response = await self.hass.services.async_call(
                "weather", "get_forecasts", {"type": "hourly"},
                target={"entity_id": weather}, blocking=True, return_response=True)
        except HomeAssistantError as err:
            _LOGGER.debug("No hourly forecast from %s: %s", weather, err)
            return
        temps: dict[datetime, float] = {}
        for item in (response or {}).get(weather, {}).get("forecast", []):
            start = dt_util.parse_datetime(str(item.get("datetime")))
            if start is not None and item.get("temperature") is not None:
                temps[hour_of(start)] = float(item["temperature"])
        self._temps, self._temps_fetched = temps, now

    # ------------------------------------------------------------ overview

    async def _async_refresh_overview(self, now: datetime) -> None:
        options = self.config_entry.options
        roles = {role: self._stat_ids(key) for role, key in STAT_ROLES.items()}
        if not any(roles.values()):
            self.overview, self.overview_status = [], OVERVIEW_NOT_CONFIGURED
            return
        try:
            settings = parse_settings(yaml.safe_load(options.get(OPT_COST_SETTINGS) or "") or {})
        except (yaml.YAMLError, ValueError) as err:
            _LOGGER.warning("Invalid cost settings for the overview: %s", err)
            self.overview, self.overview_status = [], OVERVIEW_BAD_SETTINGS
            return
        first = options.get(OPT_OVERVIEW_FROM)
        start_day = date.fromisoformat(first) if first else date(now.year - 1, 1, 1)
        start = dt_util.start_of_local_day(start_day)
        days = await self._async_daily_usage(roles, start, now)
        if not days:
            self.overview, self.overview_status = [], OVERVIEW_NO_DATA
            return
        self.overview = build_overview(days, now.date(), settings, start_day.year)
        self.overview_status = OVERVIEW_OK
        folder = options.get(OPT_EXPORT_DIR)
        if folder:
            files = {f"energy_overzicht_{y.year}.csv": to_csv(y) for y in self.overview}
            files["energy_overzicht_jaren.csv"] = years_csv(self.overview)
            await self.hass.async_add_executor_job(_write_files, folder, files)

    def _stat_ids(self, key: str) -> list[str]:
        value = self.config_entry.options.get(key)
        if not value:
            return []
        return [value] if isinstance(value, str) else list(value)

    async def _async_daily_usage(self, roles: dict[str, list[str]], start: datetime,
                                 end: datetime) -> dict[date, Usage]:
        """Daily change of every statistic, summed per role into one Usage per local day."""
        ids = {i for role, stat_ids in roles.items() if role != "temp" for i in stat_ids}
        recorder = get_instance(self.hass)
        changes = await recorder.async_add_executor_job(
            statistics_during_period, self.hass, start, end, ids, "day",
            {"energy": "kWh", "volume": "m³"}, {"change"}) if ids else {}
        temps = await recorder.async_add_executor_job(
            statistics_during_period, self.hass, start, end, set(roles["temp"]), "day",
            {"temperature": "°C"}, {"mean"}) if roles["temp"] else {}
        days: dict[date, dict[str, float]] = {}
        for role, stat_ids in roles.items():
            source, field = (temps, "mean") if role == "temp" else (changes, "change")
            for stat_id in stat_ids:
                for row in source.get(stat_id, []):
                    value = row.get(field)
                    if value is None:
                        continue
                    day = _row_day(row["start"])
                    values = days.setdefault(day, {})
                    values[role] = values.get(role, 0.0) + float(value)
        out: dict[date, Usage] = {}
        for day, v in days.items():
            out[day] = Usage(
                import_kwh=v.get("import", 0.0), export_kwh=v.get("export", 0.0),
                pv_kwh=v.get("pv", 0.0), gas_m3=v.get("gas", 0.0), hp_kwh=v.get("hp", 0.0),
                ev_kwh=v.get("ev", 0.0), import_cost=v.get("import_cost"),
                export_revenue=v.get("export_revenue"),
                temp_sum=v.get("temp", 0.0), temp_days=1 if "temp" in v else 0,
            )
        return out

    def build_slots(self) -> list[Slot]:
        """Quarter slots from the Jeroen.nl price list, with solar and temperature per hour."""
        price_state = self.hass.states.get(self.option(OPT_PRICE_ENTITY) or "")
        solar_state = self.hass.states.get(self.option(OPT_SOLAR_ENTITY) or "")
        return build_slots(
            price_state.attributes.get("prices", []) if price_state else [],
            solar_state.attributes.get("hours", []) if solar_state else [],
            self._temps,
            self._float_state(self.config_entry.data.get(CONF_OUTSIDE_TEMPERATURE)),
        )
