"""Data coordinator for Home EMS: reads sensors and evaluates the model."""
from __future__ import annotations

import logging
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import yaml

from homeassistant.components import persistent_notification
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
    BEDROOM_DEFAULTS,
    CONF_BATTERY_CHARGE_POSITIVE,
    CONF_BATTERY_POWER,
    CONF_HEAT_OUTPUT,
    CONF_FLOW_RATE,
    CONF_FLOW_TEMPERATURE,
    CONF_GRID_POWER,
    CONF_HEAT_PUMP_POWER,
    CONF_KITCHEN_CLIMATE,
    CONF_LIVING_CLIMATE,
    CONF_OUTSIDE_TEMPERATURE,
    CONTROL_DEFAULTS,
    DOMAIN,
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
    OPT_DYNAMIC_FROM,
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
    OPT_RESERVE_CHARGE_OPTION,
    OPT_RESERVE_EXTERNAL,
    OPT_RESERVE_FORCE_MODE,
    OPT_RESERVE_GRID,
    OPT_RESERVE_HOLD,
    OPT_RESERVE_POWER,
    OPT_RESERVE_POWER_ENTITY,
    OPT_RESERVE_SOC,
    OPT_RESERVE_STANDBY_OPTION,
    OPT_RESERVE_START,
    OPT_RESERVE_STOP,
    OPT_RESERVE_TARGET_ENTITY,
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
    OVERVIEW_BAD_SETTINGS,
    OVERVIEW_NOT_CONFIGURED,
    OVERVIEW_NO_DATA,
    OVERVIEW_OK,
    OVERVIEW_REFRESH,
    PLAN_NO_PRICES,
    PLAN_NO_TEMPERATURES,
    PLAN_OK,
    REPLAN_INTERVAL,
    RESERVE_DEFAULTS,
    UPDATE_INTERVAL,
    WEATHER_REFRESH,
)
from .arbiter import (
    ArbiterSettings,
    PvBoost,
    RunLatch,
    Target,
    in_window,
    night_ceiling,
    target_setpoint,
    until_window_end,
)
from .appliances import StartAdvice, best_start, parse_appliances
from .bedrooms import Bedroom, BedroomSettings, RoomTarget, parse_rooms, room_targets
from .curtail import CurtailAdvice, CurtailState, advise as curtail_advise
from .executor import Action, ExecutorSettings, ExecutorState, action_is_retry, step
from .model import (
    PV_START_WINDOW,
    Evaluation,
    Inputs,
    RunTracker,
    Settings,
    average_power,
    average_surplus,
    confirm_pv_start,
    evaluate,
    prices_today,
    pv_forecast_ok,
)
from .overview import (
    Usage,
    YearSummary,
    apply_manual,
    build_overview,
    clean_daily,
    export_csv,
    parse_settings,
    years_csv,
)
from .planner import QUARTER, Plan, PlannerSettings, Slot, build_slots, hour_of, plan
from .learn import HeatPumpLearner
from .planner_model import ModelSettings, plan_with_model
from .watch import CLEAR, RAISE, DemandWatch, OutageWatch
from .reserve import START as RESERVE_START, STOP as RESERVE_STOP
from .reserve import Decision as ReserveDecision
from .reserve import ReserveSettings, ReserveState, decide as reserve_decide, valid as reserve_valid

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
        target = path / name
        # only when changed, so finished years keep their date and are not copied again
        if target.is_file() and target.read_text(encoding="utf-8-sig") == text:
            continue
        # utf-8 with BOM so Excel shows m³ and € correctly
        target.write_text(text, encoding="utf-8-sig")

STORAGE_VERSION = 1
# arbiter sources that start or hold a run: warm water flows to the bedrooms
RUN_SOURCES = ("comfort_min", "floor", "minimum", "keep_running", "planner", "pv", "to_top")
SAVE_DELAY = 60  # seconds
BOOST_KEEP = timedelta(minutes=15)  # a boost older than this is not taken back after a restart
FLAT_PRICE = 0.25  # stand-in price per kWh while the tariff is fixed (only the COP then decides)
LATCH_GRACE = timedelta(minutes=10)  # a gap in the heat pump power this short is still a run
DAY_JOIN = timedelta(minutes=45)  # this close to the end of the night a run gets the day top
PRE_DAY = timedelta(hours=2, minutes=30)  # a night run this close to the end of the night gets the day top

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
        # arbiter in shadow mode: the setpoint it would write, and the EMS on/off switch
        self.target: Target | None = None
        self.enabled = True
        self.planner_model = False   # v0.11.1: switch, the house model planner steers
        self._price_now: float | None = None
        self.pv_boost = PvBoost()
        # executor (v0.6): writes the target to the living room thermostat, off by default
        self.control = False
        self.executor = ExecutorState()
        self.action: Action | None = None
        # bedrooms (v0.7, shadow mode): target per radiator thermostat
        self.room_targets: list[RoomTarget] = []
        # PV curtailment and appliances (v0.8, shadow mode)
        self.curtail_state = CurtailState()
        self.curtail: CurtailAdvice | None = None
        self.appliance_advice: list[StartAdvice] = []
        self._slots: list[Slot] = []
        self._surplus_samples: list[tuple[datetime, float]] = []
        self._pv_samples: list[tuple[datetime, float]] = []   # solar power, for the hold
        self._prev_raised = 0.0
        self.run_latch = RunLatch()
        self.learner = HeatPumpLearner()
        self._learned_at: datetime | None = None
        self.outage_watch = OutageWatch()
        self.demand_watch = DemandWatch()
        self._pv_hold: bool | None = None
        self.model_plan: Plan | None = None
        self.plan_status = PLAN_NO_PRICES
        self._temps: dict[datetime, float] = {}   # hour start (UTC) -> forecast temperature
        self._temps_fetched: datetime | None = None
        self.overview: list[YearSummary] = []
        self.overview_status = OVERVIEW_NOT_CONFIGURED
        self._overview_made: datetime | None = None
        self.overview_skipped: list[dict[str, Any]] = []   # glitch days left out of the overview
        # battery reserve guard (v0.12): own switch, off by default
        self.reserve_on = False
        self.reserve = ReserveState()
        self.reserve_decision: ReserveDecision | None = None

    # ------------------------------------------------------------ settings

    def option(self, key: str) -> Any:
        default = OPTION_DEFAULTS.get(key, CONTROL_DEFAULTS.get(
            key, BEDROOM_DEFAULTS.get(key, RESERVE_DEFAULTS.get(key))))
        return self.config_entry.options.get(key, default)

    def fixed_tariff(self, now: datetime | None = None) -> bool:
        """Before the first day of the dynamic contract the price is the same all day (and
        solar counts at the same price): the price rules are off and only the COP decides."""
        value = self.option(OPT_DYNAMIC_FROM)
        if not value:
            return False
        try:
            first = date.fromisoformat(str(value))
        except ValueError:
            return False
        return dt_util.as_local(now or dt_util.now()).date() < first

    def settings(self, now: datetime | None = None) -> Settings:
        night = self.night_now(now)
        floor_reference = (float(self.option(OPT_LIVING_MIN)) if night
                           else float(self.option(OPT_COMFORT_SETPOINT)))
        return Settings(
            floor_reference=floor_reference,
            floor_enabled=not night,
            min_run=timedelta(minutes=float(self.option(OPT_MIN_RUN))),
            floor_min_margin=float(self.option(OPT_FLOOR_MIN_MARGIN)),
            pv_run_threshold_w=float(self.option(OPT_PV_RUN_THRESHOLD)),
            expensive_share=(0.0 if self.fixed_tariff(now)
                             else float(self.option(OPT_EXPENSIVE_SHARE)) / 100),
            comfort_setpoint=float(self.option(OPT_COMFORT_SETPOINT)),
            floor_rule_band=float(self.option(OPT_FLOOR_RULE_BAND)),
            comfort_min=float(self.option(OPT_COMFORT_MIN)),
        )

    def planner_settings(self) -> PlannerSettings:
        return PlannerSettings(
            base_load_kw=float(self.option(OPT_BASE_LOAD)),
            max_gap=timedelta(hours=float(self.option(OPT_MAX_GAP))),
            min_pause=timedelta(hours=float(self.option(OPT_MIN_PAUSE))),
            tz=dt_util.get_default_time_zone(),
            capacity_kwh_k=float(self.option(OPT_CAPACITY)),
            target_temp=float(self.option(OPT_COMFORT_SETPOINT)),
            heat_rate_kw=float(self.option(OPT_HEAT_RATE)),
            heat_rate_slope=float(self.option(OPT_HEAT_RATE_SLOPE)),
            gas_price_m3=self._float_state(self.option(OPT_GAS_ENTITY)),
            # learned from the heat output sensor (if set and enough running in a bucket)
            cop_points=tuple((t, cop) for t, cop, _ in self.learner.points()),
            heat_points=tuple((t, kw) for t, _, kw in self.learner.points()),
        )

    async def _async_setup(self) -> None:
        """Restore run state and day counters from the previous run of HA."""
        stored = await self._store.async_load()
        if stored:
            now = dt_util.now()
            self.tracker = RunTracker.from_dict(stored, now)
            self._restore_boost(stored.get("boost"), now)
            self._restore_control(stored, now)
            self.learner = HeatPumpLearner.from_dict(stored.get("learned"))
            # a charge in progress stays ours after a restart, so it is finished or stopped
            self.reserve = ReserveState.from_dict(stored.get("reserve"))

    def _stored(self) -> dict[str, Any]:
        """Run state for the store, plus the solar boost and the raise on the thermostat,
        so saving the options (which reloads the integration) does not lose them."""
        return {**self.tracker.to_dict(), "boost": {
            "extra": self.pv_boost.extra, "raised": self._prev_raised,
            "at": dt_util.now().isoformat()},
            "executor": self.executor.to_dict(), "latch": self.run_latch.to_dict(),
            "learned": self.learner.to_dict(), "reserve": self.reserve.to_dict()}

    def _restore_boost(self, data: dict | None, now: datetime) -> None:
        """Take the boost back after a reload or a short restart (at most BOOST_KEEP old)."""
        try:
            at = datetime.fromisoformat(data["at"]) if data else None
        except (KeyError, TypeError, ValueError):
            return
        if at is None or now - at > BOOST_KEEP:
            return
        self.pv_boost.extra = float(data.get("extra") or 0.0)
        self._prev_raised = float(data.get("raised") or 0.0)

    def _restore_control(self, stored: dict, now: datetime) -> None:
        """Take back the executor and the run after a reload or a short restart: a pause after
        a change by hand stays a pause, and a run in progress stays a run."""
        try:
            at = datetime.fromisoformat(stored["boost"]["at"])
        except (KeyError, TypeError, ValueError):
            return
        recent = now - at <= BOOST_KEEP
        saved = ExecutorState.from_dict(stored.get("executor"))
        if saved.override_until is not None and saved.override_until > now:
            self.executor.override_until = saved.override_until
            self.executor.pause_status = saved.pause_status
        if recent:
            self.executor.last_written = saved.last_written
            self.executor.last_write = saved.last_write
            self.executor.last_source = saved.last_source
            self.executor.before = saved.before
            self.run_latch = RunLatch.from_dict(stored.get("latch"))

    async def async_save_now(self) -> None:
        """Write the store at once (before an unload or reload)."""
        await self._store.async_save(self._stored())

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

    def _power_state(self, entity_id: str | None) -> float | None:
        """A power sensor in W (sensors in kW or MW are converted)."""
        value = self._float_state(entity_id)
        if value is None:
            return None
        state = self.hass.states.get(entity_id)
        unit = str(state.attributes.get("unit_of_measurement") or "").strip() if state else ""
        return value * {"kW": 1000.0, "MW": 1_000_000.0}.get(unit, 1.0)

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
            self._power_state(entity_id)
            for entity_id in self.config_entry.data.get(CONF_HEAT_PUMP_POWER, [])
        ]
        known = [v for v in values if v is not None]
        return sum(known) if known else None

    def read_inputs(self, now: datetime) -> Inputs:
        data = self.config_entry.data
        # `today` holds every quarter of the day; `prices` starts at now (for planning)
        price_state = self.hass.states.get(self.option(OPT_PRICE_ENTITY) or "")
        today = price_state.attributes.get("today", []) if price_state else []
        price_now, day_prices = prices_today(today, now)
        return Inputs(
            heat_pump_power_w=self._heat_pump_power(),
            flow_temp=self._float_state(data.get(CONF_FLOW_TEMPERATURE)),
            living_temp=self._climate_temperature(data.get(CONF_LIVING_CLIMATE)),
            grid_power_w=self._power_state(data.get(CONF_GRID_POWER)),
            price_now=price_now,
            day_prices=day_prices,
            market_now=prices_today(today, now, "market")[0],
            kitchen_temp=self._climate_temperature(data.get(CONF_KITCHEN_CLIMATE)),
            flow_rate_lph=self._float_state(data.get(CONF_FLOW_RATE)),
            pv_power_w=self._pv_power(),
            battery_charge_w=self._battery_charge(),
        )

    def _battery_charge(self) -> float:
        """What the batteries charge now (W, + = charging, - = discharging); 0 without them."""
        data = self.config_entry.data
        entities = data.get(CONF_BATTERY_POWER) or []
        entities = [entities] if isinstance(entities, str) else entities
        known = [v for v in (self._power_state(e) for e in entities) if v is not None]
        sign = 1.0 if data.get(CONF_BATTERY_CHARGE_POSITIVE, True) else -1.0
        return sign * sum(known)

    def _pv_power(self) -> float | None:
        """Solar production now: the sum of the inverter sensors, None if not all known."""
        values = [self._power_state(e) for e in self._pv_entities()]
        if not values or any(v is None for v in values):
            return None
        return sum(values)

    async def _async_update_data(self) -> Evaluation:
        now = dt_util.now()
        settings = self.settings(now)
        self.tracker.settings = settings
        inputs = self.read_inputs(now)
        evaluation = evaluate(inputs, self.tracker, now, settings)
        if evaluation.pv_surplus_w is not None:
            self._surplus_samples.append((now, evaluation.pv_surplus_w))
        self._surplus_samples = [(t, v) for t, v in self._surplus_samples
                                 if now - t <= PV_START_WINDOW]
        if inputs.pv_power_w is not None:
            self._pv_samples.append((now, inputs.pv_power_w))
        self._pv_samples = [(t, v) for t, v in self._pv_samples if now - t <= PV_START_WINDOW]
        last_end = self.tracker.last_run_end
        evaluation = confirm_pv_start(
            evaluation, average_surplus(self._surplus_samples, now),
            settings.pv_run_threshold_w, self._pv_forecast_ok(now),
            since_run_end=now - last_end if last_end is not None else None)
        self._store.async_delay_save(self._stored, SAVE_DELAY)
        if self.plan is None or now - self.plan.made >= REPLAN_INTERVAL:
            try:
                await self._async_replan(now)
            except Exception:  # the planner must never stop the arbiter and the executor
                _LOGGER.exception("Could not make the plan")
        if self._overview_made is None or now - self._overview_made >= OVERVIEW_REFRESH:
            self._overview_made = now
            try:
                await self._async_refresh_overview(now)
            except Exception:  # the overview must never stop the advice and the planner
                _LOGGER.exception("Could not build the energy overview")
        self._price_now = inputs.price_now
        # the batteries charging is power the heat pump could use (heat comes first)
        signal = (inputs.grid_power_w - inputs.battery_charge_w
                  if inputs.grid_power_w is not None else None)
        # the run as the EMS sees it: a short gap in the heat pump power is still a run
        steer = replace(evaluation, heat_pump_running=self.run_latch.running(
            evaluation.heat_pump_running, now, LATCH_GRACE))
        s = self.arbiter_settings(now)
        if inputs.pv_power_w is not None and inputs.pv_power_w < 50:
            self.pv_boost.reset()   # no sun: no solar boost (also after a grid dropout)
        self.pv_boost.update(signal, steer.heat_pump_running, now,
                             timedelta(minutes=float(self.option(OPT_WRITE_INTERVAL))), s)
        self._pv_hold = self._solar_hold(inputs, now, s)
        self.target = self._target(steer, inputs.living_temp, now)
        self.run_latch.update(self.target.source, now, s.run_ceiling)
        try:
            self._curtail(inputs, now)
        except Exception:  # curtailment and appliances must never stop the executor
            _LOGGER.exception("Could not work out curtailment and appliances")
        await self._async_execute(now)
        try:
            await self._async_reserve(now)
        except Exception:  # the reserve guard must never stop the rest of the EMS
            _LOGGER.exception("Could not run the battery reserve guard")
        self._watch_demand(now, evaluation.heat_pump_running, inputs.living_temp)
        self._learn(now, evaluation.heat_pump_running, inputs.heat_pump_power_w)
        return evaluation

    def _solar_hold(self, inputs: Inputs, now: datetime, s: ArbiterSettings) -> bool:
        """A run may go on on solar: a steady 5-minute surplus with real sun, not at night.
        Small differences between the grid meter and the heat pump power gave a "surplus"
        of a few hundred watts in the dark."""
        if s.night:
            return False
        threshold = max(s.boost_margin_w, float(self.option(OPT_PV_RUN_THRESHOLD)) / 2)
        if average_surplus(self._surplus_samples, now) < threshold:
            return False
        # the solar power over the same five minutes, not this minute's reading: a passing
        # cloud stopped a run on solar at the top, and the sun back minutes later started
        # it again (12 Nov 2026)
        pv = average_power(self._pv_samples, now)
        if pv is None:
            pv = inputs.pv_power_w
        if pv is not None:
            base = float(self.option(OPT_BASE_LOAD)) * 1000
            return pv >= base + threshold
        return True

    # ------------------------------------------------------------ arbiter (shadow mode)

    def night_now(self, now: datetime | None = None) -> bool:
        """It is night now (local time, the night window; from == until = never night)."""
        try:
            start = time.fromisoformat(str(self.option(OPT_NIGHT_START)))
            end = time.fromisoformat(str(self.option(OPT_NIGHT_END)))
        except ValueError:
            return False
        moment = dt_util.as_local(now or dt_util.now()).time()
        return in_window(moment, start, end)

    def _to_night_end(self, now: datetime | None = None) -> timedelta:
        try:
            end = time.fromisoformat(str(self.option(OPT_NIGHT_END)))
        except ValueError:
            return timedelta(days=1)
        return until_window_end(dt_util.as_local(now or dt_util.now()).time(), end)

    def limits(self, now: datetime | None = None) -> tuple[float, float, bool]:
        """Lower limit, top and night for a moment: the control and the model planner both
        use this. Day: comfort setpoint to comfort setpoint + headroom; night: the night
        minimum to the night ceiling (the day top in the last part of the night)."""
        comfort_min = float(self.option(OPT_COMFORT_MIN))
        comfort = float(self.option(OPT_COMFORT_SETPOINT))
        ceiling = comfort + float(self.option(OPT_RUN_HEADROOM))
        living_min = comfort
        night = self.night_now(now)
        if night:
            living_min = float(self.option(OPT_LIVING_MIN))
            day_top = ceiling
            night_top = float(self.option(OPT_NIGHT_CEILING) or 0)
            if night_top > 0:
                ceiling = night_ceiling(ceiling, night_top, comfort_min)
            else:
                ceiling = min(ceiling, living_min + float(self.option(OPT_START_BAND)))
            # a run near the end of the night goes on to the level the day starts at (the
            # comfort setpoint), not higher: one run instead of a night run and a new start
            # when the day limits begin
            to_end = self._to_night_end(now)
            if to_end <= PRE_DAY:
                ceiling = min(day_top, max(ceiling, comfort))
            if to_end <= DAY_JOIN:
                # right before the day a run that is going is not stopped at the comfort
                # setpoint only to start again minutes later on the day limits
                ceiling = day_top
        ceiling = min(float(self.option(OPT_COMFORT_MAX)),
                      max(ceiling, living_min + float(self.option(OPT_START_BAND))))
        return living_min, ceiling, night

    def _store_top(self, moment: datetime, top: float, night: bool) -> float:
        """How high a planned storage run may heat: by day the comfort maximum; at night the
        day top, but only with a dynamic contract (heat at night when it is clearly cheaper;
        with a fixed tariff the night is for cooling down)."""
        if not night:
            return float(self.option(OPT_COMFORT_MAX))
        if self.fixed_tariff(moment):
            return top
        day_top = float(self.option(OPT_COMFORT_SETPOINT)) + float(self.option(OPT_RUN_HEADROOM))
        return max(top, day_top)

    def _model_limits(self, moment: datetime) -> tuple[float, float, float, bool]:
        """Lower limit, top, storage top and night for the model planner."""
        low, top, night = self.limits(moment)
        return low, top, self._store_top(moment, top, night), night

    def arbiter_settings(self, now: datetime | None = None) -> ArbiterSettings:
        comfort_min = float(self.option(OPT_COMFORT_MIN))
        living_min, ceiling, night = self.limits(now)
        night_store = False
        if self.plan is not None and self.plan.model and self.planner_model:
            moment = now or dt_util.now()
            current = next((r for r in self.plan.runs if r.start <= moment < r.end), None)
            if current is not None and current.reason == "store":
                # a planned storage run (cheap hours before dearer ones) goes on to the storage
                # top: by day the comfort maximum, at night (dynamic contract) the day top
                ceiling = max(ceiling, self._store_top(moment, ceiling, night))
                night_store = night
        if self.run_latch.since is not None and self.run_latch.ceiling is not None:
            # a run keeps the top it started with, but a day run that is still going in the
            # night gets the night top once it has had its minimum run time
            carried = (night and self.run_latch.ceiling > ceiling
                       and (now or dt_util.now()) - self.run_latch.since
                       >= timedelta(minutes=float(self.option(OPT_MIN_RUN))))
            if not carried:
                ceiling = max(ceiling, self.run_latch.ceiling)
        return ArbiterSettings(comfort_min=comfort_min,
                               comfort_max=float(self.option(OPT_COMFORT_MAX)),
                               run_ceiling=ceiling,
                               night=night,
                               night_store=night_store,
                               gap_max=float(self.option(OPT_GAP_MAX)),
                               boost_margin_w=float(self.option(OPT_BOOST_MARGIN)),
                               low_price=(0.0 if self.fixed_tariff(now)
                                          else float(self.option(OPT_LOW_PRICE))),
                               living_min=living_min,
                               off_margin=float(self.option(OPT_OFF_MARGIN)),
                               start_band=float(self.option(OPT_START_BAND)))

    def _target(self, evaluation: Evaluation, living_temp: float | None, now: datetime) -> Target:
        plan_now = bool(self.plan and self.plan.running_now(now))
        if not plan_now and evaluation.heat_pump_running and self.plan:
            # the heat pump runs and the next planned run starts within the shortest pause:
            # keep going instead of stopping and starting again (after a restart or a
            # change of the plan, the run in progress may not be in the new plan)
            plan_now = self.plan.run_soon(now, timedelta(hours=float(self.option(OPT_MIN_PAUSE))))
        args = (evaluation, living_temp, plan_now, self.enabled, self.arbiter_settings(now),
                self.current_setpoint())
        target = target_setpoint(*args, price_now=self._price_now, pv_extra=self.pv_boost.extra,
                                 prev_raised=self._prev_raised, pv_hold=self._pv_hold)
        rooms = self.bedrooms() if self.enabled else []
        if not rooms:
            self.room_targets = []
            return target
        flowing = bool(evaluation.heat_pump_running) or target.source in RUN_SOURCES
        temps = {r.entity_id: self._climate_temperature(r.entity_id) for r in rooms}
        bs = self.bedroom_settings()
        self.room_targets = room_targets(rooms, temps, now, flowing, bs)
        # a bedroom run goes on until the rooms are warm enough (not stop, restart, stop)
        bedroom_run = (self.run_latch.source == "bedroom" and evaluation.heat_pump_running
                       and any(r.setpoint > bs.min_temp and r.temperature is not None
                               and r.temperature < r.setpoint - bs.margin for r in self.room_targets))
        if bedroom_run or any(r.needs_run for r in self.room_targets):
            target = target_setpoint(*args, bedroom_call=True, price_now=self._price_now,
                                     pv_extra=self.pv_boost.extra,
                                     prev_raised=self._prev_raised, pv_hold=self._pv_hold)
        return target

    def bedrooms(self) -> list[Bedroom]:
        try:
            return parse_rooms(yaml.safe_load(self.option(OPT_BEDROOMS) or "") or [])
        except (yaml.YAMLError, ValueError) as err:
            _LOGGER.warning("Invalid bedroom settings: %s", err)
            return []

    def bedroom_settings(self) -> BedroomSettings:
        return BedroomSettings(min_temp=float(self.option(OPT_BEDROOM_MIN)),
                               lead=timedelta(hours=float(self.option(OPT_BEDROOM_LEAD))))

    def current_setpoint(self) -> float | None:
        state = self.hass.states.get(self.config_entry.data.get(CONF_LIVING_CLIMATE) or "")
        value = state.attributes.get("temperature") if state else None
        return float(value) if value is not None else None

    def set_planner_model(self, on: bool, replan: bool = True) -> None:
        """Switch: the planner with the house model steers (on) or only shows (off)."""
        self.planner_model = on
        if replan:
            self.plan = None   # the next update makes a new plan with the chosen planner

    def set_enabled(self, enabled: bool) -> None:
        """EMS switch: off = no target, the thermostat schedule leads."""
        self.enabled = enabled
        if self.data is not None:
            self.target = self._target(self.data, self.read_inputs(dt_util.now()).living_temp,
                                       dt_util.now())
        self.async_update_listeners()

    # ------------------------------------------------------------ PV curtailment (v0.8)

    def _pv_entities(self) -> list[str]:
        value = self.option(OPT_PV_POWER) or []
        return [value] if isinstance(value, str) else list(value)

    def _curtail(self, inputs: Inputs, now: datetime) -> None:
        entities = self._pv_entities()
        if not entities:
            self.curtail = None
            return
        grid = inputs.grid_power_w
        export = None if grid is None else max(0.0, -grid)
        powers = {e: self._float_state(e) for e in entities}
        self.curtail = curtail_advise(self.curtail_state, powers, export, inputs.market_now, now)

    def _advise_appliances(self, now: datetime) -> None:
        try:
            items = parse_appliances(yaml.safe_load(self.option(OPT_APPLIANCES) or "") or [])
        except (yaml.YAMLError, ValueError) as err:
            _LOGGER.warning("Invalid appliance settings: %s", err)
            items = []
        base = float(self.option(OPT_BASE_LOAD))
        self.appliance_advice = [best_start(self._slots, now, a, base) for a in items] if self._slots else []

    # ------------------------------------------------------------ executor (v0.6)

    def executor_settings(self) -> ExecutorSettings:
        return ExecutorSettings(
            min_interval=timedelta(minutes=float(self.option(OPT_WRITE_INTERVAL))),
            override_hold=timedelta(hours=float(self.option(OPT_OVERRIDE_HOURS))),
            fallback_setpoint=float(self.option(OPT_COMFORT_SETPOINT)),
            min_step=float(self.option(OPT_WRITE_STEP)),
        )

    def _thermostat_reachable(self) -> bool:
        state = self.hass.states.get(self.config_entry.data.get(CONF_LIVING_CLIMATE) or "")
        return state is not None and state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)

    def _watch_thermostat(self, now: datetime) -> bool:
        """True when the thermostat can be written now. An outage (also one that comes and
        goes) gives one notification, which ends after 30 minutes without a break."""
        reachable = self._thermostat_reachable()
        event = self.outage_watch.update(reachable, now, armed=self.control)
        notification_id = f"{DOMAIN}_thermostat_unreachable"
        if event == RAISE:
            persistent_notification.async_create(
                self.hass,
                "Home EMS cannot reach the living room thermostat (it may come and go). The "
                "thermostat keeps its last setpoint by itself; Home EMS writes when it can. / "
                "Home EMS kan de thermostaat in de woonkamer niet bereiken (hij kan komen en "
                "gaan). De thermostaat houdt zelf het laatste setpoint vast; Home EMS schrijft "
                "zodra het kan.",
                title="Home EMS: thermostat unreachable / thermostaat onbereikbaar",
                notification_id=notification_id)
        elif event == CLEAR:
            persistent_notification.async_dismiss(self.hass, notification_id)
        return reachable

    def _learn(self, now: datetime, running: bool | None, el_w: float | None) -> None:
        """Learn COP and heat per outdoor temperature from the heat output sensor."""
        data = self.config_entry.data
        minutes = 1.0
        if self._learned_at is not None:
            minutes = min(5.0, (now - self._learned_at).total_seconds() / 60)
        self._learned_at = now
        if not data.get(CONF_HEAT_OUTPUT):
            return
        self.learner.add(self._float_state(data.get(CONF_OUTSIDE_TEMPERATURE)),
                         self._power_state(data.get(CONF_HEAT_OUTPUT)), el_w, running, minutes, now)

    def _watch_demand(self, now: datetime, running: bool | None, living: float | None) -> None:
        """The thermostat asks for heat (setpoint above the room) but the heat pump does not
        run: tado does not pass the demand on (seen on 11 Nov, one hour without heat)."""
        current = self.current_setpoint()
        asking = (self.control and self._thermostat_reachable() and current is not None
                  and living is not None and current > living + 0.05)
        event = self.demand_watch.update(asking, running, now)
        notification_id = f"{DOMAIN}_no_heat_demand"
        if event == RAISE:
            persistent_notification.async_create(
                self.hass,
                f"The living room setpoint ({current:.1f} °C) is above the room ({living:.1f} °C) "
                "for 30 minutes, but the heat pump does not run. tado may not pass the heat "
                "demand on (check the tado bridge). / Het setpoint in de woonkamer staat al 30 "
                "minuten boven de kamer, maar de warmtepomp draait niet. Mogelijk geeft tado de "
                "warmtevraag niet door (kijk naar de tado-bridge).",
                title="Home EMS: no heat / geen warmte",
                notification_id=notification_id)
        elif event == CLEAR:
            persistent_notification.async_dismiss(self.hass, notification_id)

    async def _async_execute(self, now: datetime) -> None:
        target = self.target
        if target is None:
            return
        if not self._watch_thermostat(now):
            # no writes into an unreachable thermostat (they fail and fill the log); the
            # first cycle after it is back writes the target at once
            if self.control:
                self.action = Action(None, "error", "thermostat unreachable")
            return
        current = self.current_setpoint()
        living = self._climate_temperature(self.config_entry.data.get(CONF_LIVING_CLIMATE))
        before = self.executor.before if action_is_retry(self.executor, current) else current
        action = step(self.executor, target.setpoint, target.source, target.reason,
                      current, now, self.control, self.executor_settings(), living)
        if action.write is not None:
            if await self._async_write(action.write):
                self.executor.record_write(action.write, target.source, now,
                                           steering=action.status != "fallback",
                                           before=before,
                                           demand=None if living is None else action.write > living)
                self.tracker.stats.setpoint_writes += 1
                # the raise (solar boost, low price) carried by the setpoint now on the
                # thermostat: when it is taken back, it comes off the setpoint again
                self._prev_raised = target.boost_k if action.status != "fallback" else 0.0
            else:
                action = Action(None, "error", f"could not write {action.write} C")
        self.action = action

    async def _async_write(self, setpoint: float) -> bool:
        entity_id = self.config_entry.data.get(CONF_LIVING_CLIMATE)
        try:
            await self.hass.services.async_call(
                "climate", "set_temperature", {"entity_id": entity_id, "temperature": setpoint},
                blocking=True)
        except HomeAssistantError as err:
            _LOGGER.warning("Could not set %s to %s C: %s", entity_id, setpoint, err)
            return False
        _LOGGER.info("Home EMS set %s to %s C", entity_id, setpoint)
        return True

    async def async_set_control(self, on: bool, restore: bool = False) -> None:
        """Control switch. Turning it off while steering writes the comfort setpoint once,
        so the thermostat is not left at a low idle setpoint."""
        self.control = on
        if not on and not restore and self.executor.last_written is not None:
            fallback = float(self.option(OPT_COMFORT_SETPOINT))
            if await self._async_write(fallback):
                self.executor.record_write(fallback, "off", dt_util.now(), steering=False,
                                           before=self.current_setpoint())
                self.tracker.stats.setpoint_writes += 1
        if not on:
            self.executor.override_until = None
            self.executor.pause_status = "override"
            self.executor.retries = 0
            self.action = Action(None, "off", "control switched off")
        self.async_update_listeners()

    # ------------------------------------------------------------ battery reserve (v0.12)

    def reserve_settings(self) -> ReserveSettings:
        return ReserveSettings(start_soc=float(self.option(OPT_RESERVE_START)),
                               stop_soc=float(self.option(OPT_RESERVE_STOP)),
                               power_w=float(self.option(OPT_RESERVE_POWER)))

    def _entities(self, key: str) -> list[str]:
        value = self.option(key) or []
        return [value] if isinstance(value, str) else list(value)

    def reserve_configured(self) -> bool:
        return all(self.option(k) for k in (OPT_RESERVE_SOC, OPT_RESERVE_EXTERNAL,
                                             OPT_RESERVE_FORCE_MODE, OPT_RESERVE_POWER_ENTITY))

    def _grid_reports(self) -> bool:
        """The grid meter gives values: every configured sensor (the grid power sensor if
        none) has a number. Without a grid there is nothing to charge from."""
        entities = self._entities(OPT_RESERVE_GRID) or [self.config_entry.data.get(CONF_GRID_POWER)]
        return all(self._float_state(e) is not None for e in entities)

    async def _async_reserve(self, now: datetime) -> None:
        if not self.reserve_configured():
            self.reserve_decision = ReserveDecision(None, "not_configured",
                                                    "set the battery entities in the options")
            return
        s = self.reserve_settings()
        if not reserve_valid(s):
            self.reserve_decision = ReserveDecision(
                None, "error", "the stop level must be at least 2% above the start level")
            return
        soc = self._float_state(self.option(OPT_RESERVE_SOC))
        decision = reserve_decide(self.reserve, self.reserve_on, soc, self._grid_reports(), now, s)
        if decision.action == RESERVE_START:
            restore = {e: v for e in self._entities(OPT_RESERVE_HOLD)
                       if (v := self._float_state(e)) is not None}
            if await self._async_reserve_charge(s, restore):
                self.reserve.record_start(now, restore)
                _LOGGER.info("Battery reserve: %s", decision.reason)
            else:
                await self._async_reserve_release(restore)
                self.reserve.record_stop(now, fault=True, s=s)
                decision = ReserveDecision(None, "error", "could not start the charge: see the log")
        elif decision.action == RESERVE_STOP:
            ok = await self._async_reserve_release(dict(self.reserve.restore))
            self.reserve.record_stop(now, fault=decision.status == "error" or not ok, s=s)
            _LOGGER.info("Battery reserve: %s", decision.reason)
            if not ok:
                decision = ReserveDecision(None, "error",
                                           "could not stop the charge cleanly: see the log")
        elif decision.status == "charging":
            # keep the battery charging (for example after it restarted by itself)
            await self._async_reserve_charge(s, self.reserve.restore)
        self.reserve_decision = decision
        self._store.async_delay_save(self._stored, SAVE_DELAY)

    async def _async_call(self, domain: str, service: str, data: dict[str, Any]) -> bool:
        try:
            await self.hass.services.async_call(domain, service, data, blocking=True)
        except HomeAssistantError as err:
            _LOGGER.warning("Battery reserve: %s.%s %s failed: %s", domain, service, data, err)
            return False
        return True

    def _state_is(self, entity_id: str, value: str) -> bool:
        state = self.hass.states.get(entity_id)
        return state is not None and state.state == value

    def _number_is(self, entity_id: str, value: float) -> bool:
        current = self._float_state(entity_id)
        return current is not None and abs(current - value) < 0.5

    async def _async_reserve_charge(self, s: ReserveSettings, hold: dict[str, float]) -> bool:
        """Put the battery in a forced charge, writing only what is not already so: the held
        numbers to 0 (no other battery discharging into this one), external control on, the
        charge power, the charge-to level (the battery then stops by itself, also without
        Home Assistant), forced mode charge."""
        ok = True
        for entity_id in hold:
            if not self._number_is(entity_id, 0):
                ok &= await self._async_call("number", "set_value",
                                             {"entity_id": entity_id, "value": 0})
        external = self.option(OPT_RESERVE_EXTERNAL)
        if not self._state_is(external, "on"):
            ok &= await self._async_call("switch", "turn_on", {"entity_id": external})
        power = self.option(OPT_RESERVE_POWER_ENTITY)
        if not self._number_is(power, s.power_w):
            ok &= await self._async_call("number", "set_value",
                                         {"entity_id": power, "value": s.power_w})
        target = self.option(OPT_RESERVE_TARGET_ENTITY)
        if target and not self._number_is(target, s.stop_soc):
            ok &= await self._async_call("number", "set_value",
                                         {"entity_id": target, "value": s.stop_soc})
        mode = self.option(OPT_RESERVE_FORCE_MODE)
        option = str(self.option(OPT_RESERVE_CHARGE_OPTION))
        if not self._state_is(mode, option):
            ok &= await self._async_call("select", "select_option",
                                         {"entity_id": mode, "option": option})
        return ok

    async def _async_reserve_release(self, restore: dict[str, float]) -> bool:
        """End the forced charge: forced mode standby, external control off (the battery
        goes back to its own work mode), the held numbers back to their value."""
        ok = await self._async_call("select", "select_option", {
            "entity_id": self.option(OPT_RESERVE_FORCE_MODE),
            "option": str(self.option(OPT_RESERVE_STANDBY_OPTION))})
        ok &= await self._async_call("switch", "turn_off",
                                     {"entity_id": self.option(OPT_RESERVE_EXTERNAL)})
        for entity_id, value in restore.items():
            ok &= await self._async_call("number", "set_value",
                                         {"entity_id": entity_id, "value": value})
        return ok

    async def async_set_reserve(self, on: bool, restore: bool = False) -> None:
        """Reserve guard switch. Turning it off during a charge stops the charge at once."""
        self.reserve_on = on
        if not restore:
            try:
                await self._async_reserve(dt_util.now())
            except Exception:  # the switch must always switch
                _LOGGER.exception("Could not run the battery reserve guard")
        self.async_update_listeners()

    # ------------------------------------------------------------ planner

    async def _async_replan(self, now: datetime) -> None:
        await self._async_refresh_temperatures(now)
        slots = self.build_slots()
        self._slots = slots
        self._advise_appliances(now)
        if not slots:
            self.plan, self.plan_status = None, PLAN_NO_PRICES
            return
        living = self._climate_temperature(self.config_entry.data.get(CONF_LIVING_CLIMATE))
        # a run in progress stays in the plan until its planned end
        current = next((r for r in self.plan.runs if r.start <= now < r.end), None) if self.plan else None
        budget = plan(slots, now, self.planner_settings(), self.tracker.last_run_end, living,
                      keep_until=current.end if current else None)
        try:
            self.model_plan = plan_with_model(
                slots, now, living, self._model_limits,
                self.planner_settings(),
                ModelSettings(min_run=timedelta(minutes=float(self.option(OPT_MIN_RUN))),
                              start_band=float(self.option(OPT_START_BAND)),
                              reference_temp=float(self.option(OPT_COMFORT_SETPOINT))),
                running=self.run_latch.since is not None)
        except Exception:  # the model planner is new: it must never take the old one down
            _LOGGER.exception("Could not make the plan with the house model")
            self.model_plan = None
        use_model = self.planner_model and self.model_plan is not None
        self.plan = self.model_plan if use_model else budget
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
        # statistics from `overview_from` on; hand-entered periods may lie before it
        days = apply_manual(await self._async_daily_usage(roles, start, now), settings.manual)
        if not days:
            self.overview, self.overview_status = [], OVERVIEW_NO_DATA
            return
        self.overview = build_overview(days, now.date(), settings)
        self.overview_status = OVERVIEW_OK
        folder = options.get(OPT_EXPORT_DIR)
        if folder:
            # no year in the names: the same files are overwritten, the workbook keeps the history
            files = {"energy_overzicht_export.csv": export_csv(self.overview),
                     "energy_overzicht_jaren.csv": years_csv(self.overview)}
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
        skipped: list[dict[str, Any]] = []
        for role, stat_ids in roles.items():
            source, field = (temps, "mean") if role == "temp" else (changes, "change")
            for stat_id in stat_ids:
                series = {_row_day(row["start"]): float(row[field])
                          for row in source.get(stat_id, []) if row.get(field) is not None}
                if role not in ("temp", "import_cost", "export_revenue"):
                    series, dropped = clean_daily(series)
                    skipped += [{"statistic": stat_id, "day": d.isoformat(), "value": round(v, 1)}
                                for d, v in dropped]
                for day, value in series.items():
                    values = days.setdefault(day, {})
                    values[role] = values.get(role, 0.0) + value
        self.overview_skipped = skipped
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

    def _pv_forecast_ok(self, now: datetime) -> bool:
        """The solar forecast for the coming hour still gives a surplus (True without one)."""
        if not self.option(OPT_SOLAR_ENTITY) or not self._slots:
            return True
        hour = [s.pv_kw for s in self._slots if now - QUARTER < s.start <= now + timedelta(hours=1)]
        return pv_forecast_ok(hour, float(self.option(OPT_BASE_LOAD)),
                              float(self.option(OPT_PV_RUN_THRESHOLD)))

    def build_slots(self) -> list[Slot]:
        """Quarter slots from the Jeroen.nl price list, with solar and temperature per hour."""
        price_state = self.hass.states.get(self.option(OPT_PRICE_ENTITY) or "")
        solar_state = self.hass.states.get(self.option(OPT_SOLAR_ENTITY) or "")
        slots = build_slots(
            price_state.attributes.get("prices", []) if price_state else [],
            solar_state.attributes.get("hours", []) if solar_state else [],
            self._temps,
            self._float_state(self.config_entry.data.get(CONF_OUTSIDE_TEMPERATURE)),
        )
        if self.fixed_tariff():
            # fixed tariff: every quarter costs the same and solar is worth the same, so the
            # cost per kWh heat only depends on the COP (the warmest hours are the cheapest)
            slots = [replace(s, price=FLAT_PRICE, market=FLAT_PRICE, forecast=False) for s in slots]
        return slots
