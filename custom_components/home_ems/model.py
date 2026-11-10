"""Pure EMS logic for Home EMS (no Home Assistant imports, so it is easy to test).

Observation mode: this module only computes state and advice. Nothing here writes
to devices.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Any


class Advice(StrEnum):
    """What the EMS would do right now if it were allowed to act."""

    UNKNOWN = "unknown"
    RUN = "run"            # start a heat pump run
    KEEP_RUNNING = "keep_running"  # run is shorter than the minimum: hold it (anti short-cycling)
    RUNNING = "running"    # run in progress, nothing to do
    WAIT = "wait"          # no run needed now


@dataclass(frozen=True)
class Settings:
    """Tunable thresholds. Defaults follow the analysis of 14-23 Oct 2026."""

    running_threshold_w: float = 100.0
    min_run: timedelta = timedelta(minutes=30)
    # Floor water (flowmeter) minus living room temperature. A test day without floor
    # guard showed the floor water can fall to room temperature without loss of
    # comfort, so a run is only needed once it drops below the room (margin < 0 K).
    floor_min_margin: float = 0.0
    floor_full_margin: float = 3.0
    # The floor rule only applies while the living room is below comfort + band. On
    # 26 Oct 2026 the floor guard started a run at 19.8 C with a 19.0 C comfort setpoint.
    comfort_setpoint: float = 19.0
    floor_rule_band: float = 0.5
    # Hard lower bound for the living room and the kitchen (the kitchen is only a
    # measuring point, it no longer asks for heat itself): below it a run is needed,
    # also in a price peak.
    comfort_min: float = 18.0
    # The kitchen is a measuring point: at night it is 0.3-0.5 K colder than the living
    # room, so it counts this far below the comfort minimum. A run for the comfort minimum
    # goes on until the rooms are `comfort_min_hysteresis` above their limit.
    kitchen_margin: float = 0.5
    comfort_min_hysteresis: float = 0.3
    # The floor rule is relative to the living room minimum of the moment (day: comfort
    # setpoint, night: night minimum) and off in the night window (the house may cool).
    floor_reference: float | None = None
    floor_enabled: bool = True
    # The flowmeter only shows the floor water while the floor pump circulates; below
    # this flow rate the floor margin is not used (standing water).
    pump_on_lph: float = 100.0
    pv_run_threshold_w: float = 700.0
    # Setpoint to start a run with: this far above the living room. tado modulates on
    # the gap between setpoint and room, so a small gap keeps the compressor at low
    # speed with a clearly higher COP than a large step.
    run_setpoint_offset: float = 0.2
    # Price peak: a quarter among this share of the most expensive quarters of the day.
    # Follows the real morning and evening peaks instead of a fixed window.
    expensive_share: float = 0.25
    # Gaps between evaluations longer than this (HA restart, outage) are not counted.
    max_step: timedelta = timedelta(minutes=5)
    # After a restart shorter than this, an ongoing run keeps its stored start time.
    resume_window: timedelta = timedelta(minutes=15)


@dataclass(frozen=True)
class Inputs:
    """One snapshot of the sensors the EMS reads. None = unavailable."""

    heat_pump_power_w: float | None
    flow_temp: float | None
    living_temp: float | None
    grid_power_w: float | None  # positive = import, negative = export
    pv_power_w: float | None = None  # solar production now (sum of the inverters), if known
    battery_charge_w: float = 0.0    # batteries charging (+) or discharging (-), 0 = none
    price_now: float | None = None          # all-in price of the current quarter
    day_prices: tuple[float, ...] = ()      # all known quarter prices of today
    market_now: float | None = None         # market price of the current quarter
    kitchen_temp: float | None = None
    flow_rate_lph: float | None = None      # None = no flow rate sensor


@dataclass
class DayStats:
    """Counters per day, used to compare the advice with what really happened."""

    day: date | None = None
    runs: int = 0
    short_runs: int = 0
    unadvised_starts: int = 0          # real starts while the EMS advised to wait
    advised_run_minutes: float = 0.0   # EMS advised a run while the heat pump was off
    running_minutes: float = 0.0
    pv_running_minutes: float = 0.0    # heat pump running while the house exports
    # export while the market price is negative: with a dynamic contract this costs
    # money, and it shows whether switching off the inverters is worth it
    negative_price_export_kwh: float = 0.0
    setpoint_writes: int = 0           # setpoints written by the executor (v0.6)


@dataclass
class RunTracker:
    """Tracks heat pump runs and comparison counters from successive snapshots."""

    running: bool = False
    run_start: datetime | None = None  # None while running = start not observed
    last_run_end: datetime | None = None
    last_eval: datetime | None = None
    last_advice: Advice | None = None
    initialized: bool = False
    stats: DayStats = field(default_factory=DayStats)
    settings: Settings = field(default_factory=Settings)

    def _roll_day(self, now: datetime) -> None:
        if self.stats.day != now.date():
            self.stats = DayStats(day=now.date())

    def observe(self, running: bool, now: datetime) -> bool:
        """Update the run state. Returns True on a real (observed) start."""
        self._roll_day(now)
        started = False
        if not self.initialized:
            # First snapshot after setup: a run that is already going is not a new start.
            self.initialized = True
            self.run_start = None
        elif running and not self.running:
            self.run_start = now
            self.stats.runs += 1
            started = True
        elif not running and self.running:
            if self.run_start is not None and now - self.run_start < self.settings.min_run:
                self.stats.short_runs += 1
            self.last_run_end = now
            self.run_start = None
        self.running = running
        return started

    def run_duration(self, now: datetime) -> timedelta | None:
        if not self.running or self.run_start is None:
            return None
        return now - self.run_start

    def account(self, advice: Advice, running: bool, grid_power_w: float | None,
                started: bool, now: datetime, market_now: float | None = None) -> None:
        """Add time-based counters for the step since the previous evaluation."""
        if started and self.last_advice is Advice.WAIT:
            self.stats.unadvised_starts += 1
        if self.last_eval is not None:
            step = now - self.last_eval
            if timedelta(0) < step <= self.settings.max_step:
                minutes = step.total_seconds() / 60
                if (grid_power_w is not None and grid_power_w < 0
                        and market_now is not None and market_now < 0):
                    self.stats.negative_price_export_kwh += -grid_power_w / 1000 * minutes / 60
                if running:
                    self.stats.running_minutes += minutes
                    if grid_power_w is not None and grid_power_w < 0:
                        self.stats.pv_running_minutes += minutes
                elif advice is Advice.RUN:
                    self.stats.advised_run_minutes += minutes
        self.last_eval = now
        self.last_advice = advice

    def to_dict(self) -> dict[str, Any]:
        def iso(value: datetime | None) -> str | None:
            return value.isoformat() if value else None

        stats = asdict(self.stats)
        stats["day"] = self.stats.day.isoformat() if self.stats.day else None
        return {
            "running": self.running,
            "run_start": iso(self.run_start),
            "last_run_end": iso(self.last_run_end),
            "last_eval": iso(self.last_eval),
            "last_advice": self.last_advice.value if self.last_advice else None,
            "stats": stats,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], now: datetime,
                  settings: Settings | None = None) -> RunTracker:
        """Restore after a restart. Counters survive within the same day; an ongoing
        run keeps its start time only if the restart was short."""
        settings = settings or Settings()

        def parse(value: str | None) -> datetime | None:
            return datetime.fromisoformat(value) if value else None

        tracker = cls(settings=settings)
        raw = dict(data.get("stats") or {})
        day = date.fromisoformat(raw["day"]) if raw.get("day") else None
        if day == now.date():
            raw["day"] = day
            tracker.stats = DayStats(**raw)
        last_eval = parse(data.get("last_eval"))
        tracker.last_run_end = parse(data.get("last_run_end"))
        if last_eval is not None and timedelta(0) <= now - last_eval <= settings.resume_window:
            tracker.initialized = True
            tracker.running = bool(data.get("running"))
            tracker.run_start = parse(data.get("run_start"))
            tracker.last_eval = last_eval
            advice = data.get("last_advice")
            tracker.last_advice = Advice(advice) if advice else None
        return tracker


@dataclass(frozen=True)
class Evaluation:
    """Result of one evaluation, exposed as sensors."""

    advice: Advice
    reason: str
    heat_pump_running: bool | None
    floor_margin: float | None   # only at rest; None while running
    pv_surplus_w: float | None
    in_peak: bool
    setpoint_hint: float | None = None  # living room + offset when a run is advised
    # why a run is advised: "comfort_min", "floor" or "pv" (None for other advice)
    cause: str | None = None


def is_running(inputs: Inputs, settings: Settings) -> bool | None:
    if inputs.heat_pump_power_w is None:
        return None
    return inputs.heat_pump_power_w > settings.running_threshold_w


def prices_today(rows: list[dict], now: datetime,
                 key: str = "price") -> tuple[float | None, tuple[float, ...]]:
    """Price of the current quarter and all quarter prices of today (the local day of
    `now`, an aware datetime). `rows` must cover the whole day, including quarters that
    are already over (the `today` attribute of the Jeroen.nl price sensor, not
    `prices`, which starts at now): otherwise the peak shifts to whatever is left.
    `key` picks the all-in "price" or the "market" price."""
    day: list[tuple[datetime, float]] = []
    for row in rows or []:
        try:
            start = datetime.fromisoformat(str(row["start"]))
            price = float(row[key])
        except (KeyError, TypeError, ValueError):
            continue
        if start.tzinfo is not None and start.astimezone(now.tzinfo).date() == now.date():
            day.append((start, price))
    day.sort()
    current = [p for s, p in day if s <= now < s + timedelta(minutes=15)]
    return (current[-1] if current else None), tuple(p for _, p in day)


def is_expensive(price_now: float | None, day_prices: tuple[float, ...], share: float) -> bool:
    """True when the current price is among the `share` most expensive quarters of today."""
    if price_now is None or not day_prices or share <= 0:
        return False
    ranked = sorted(day_prices, reverse=True)
    if ranked[0] - ranked[-1] < 1e-9:
        return False   # a flat price has no peak
    threshold = ranked[min(len(ranked), math.ceil(len(ranked) * share)) - 1]
    return price_now >= threshold


def evaluate(inputs: Inputs, tracker: RunTracker, now: datetime,
             settings: Settings | None = None) -> Evaluation:
    """Update the run tracker and decide what the EMS would do now."""
    settings = settings or tracker.settings
    running = is_running(inputs, settings)
    pump_on = inputs.flow_rate_lph is None or inputs.flow_rate_lph >= settings.pump_on_lph
    floor_margin = (
        round(inputs.flow_temp - inputs.living_temp, 2)
        if inputs.flow_temp is not None and inputs.living_temp is not None
        and running is False and pump_on
        else None
    )
    # Solar surplus for the heat pump: the export, plus what the heat pump itself uses while
    # it runs, plus what the batteries charge. Measured at the grid meter alone, the surplus
    # would vanish as soon as the heat pump (or a battery holding the meter at zero) takes it.
    own = inputs.heat_pump_power_w if running and inputs.heat_pump_power_w else 0.0
    surplus = (
        max(0.0, own + inputs.battery_charge_w - inputs.grid_power_w)
        if inputs.grid_power_w is not None else None
    )
    if surplus is not None and inputs.pv_power_w is not None:
        # never more than the solar production (guards against a wrong sensor choice)
        surplus = min(surplus, max(0.0, inputs.pv_power_w))
    peak = is_expensive(inputs.price_now, inputs.day_prices, settings.expensive_share)

    def result(advice: Advice, reason: str, cause: str | None = None) -> Evaluation:
        hint = (
            round(inputs.living_temp + settings.run_setpoint_offset, 1)
            if advice is Advice.RUN and inputs.living_temp is not None
            else None
        )
        return Evaluation(advice, reason, running, floor_margin, surplus, peak, hint, cause)

    if running is None:
        return result(Advice.UNKNOWN, "heat pump power unavailable")

    started = tracker.observe(running, now)
    evaluation = _decide(running, floor_margin, inputs, surplus, peak, tracker,
                         now, settings, result)
    tracker.account(evaluation.advice, running, inputs.grid_power_w, started, now,
                    inputs.market_now)
    return evaluation


def _decide(running, floor_margin, inputs, surplus, peak, tracker, now, settings,
            result) -> Evaluation:
    living_temp = inputs.living_temp
    limits = (("living room", living_temp, settings.comfort_min),
              ("kitchen", inputs.kitchen_temp, settings.comfort_min - settings.kitchen_margin))
    if running:
        duration = tracker.run_duration(now)
        if duration is not None and duration < settings.min_run:
            return result(Advice.KEEP_RUNNING, "run shorter than minimum run time")
        still_cold = [name for name, temp, limit in limits
                      if temp is not None and temp < limit + settings.comfort_min_hysteresis]
        if still_cold:
            return result(Advice.RUNNING, f"{' and '.join(still_cold)} just above the comfort "
                          "minimum: keep the run going", "comfort_min")
        return result(Advice.RUNNING, "run in progress")

    cold = [name for name, temp, limit in limits if temp is not None and temp < limit]
    if cold:
        return result(Advice.RUN, f"{' and '.join(cold)} below comfort minimum", "comfort_min")

    room_warm = (
        living_temp is not None
        and living_temp >= (settings.comfort_setpoint if settings.floor_reference is None
                            else settings.floor_reference) + settings.floor_rule_band
    )
    floor_low = (settings.floor_enabled and floor_margin is not None
                 and floor_margin < settings.floor_min_margin)

    if peak:
        if (
            settings.floor_enabled
            and floor_margin is not None
            and floor_margin < settings.floor_min_margin - 1.0
            and not room_warm
        ):
            return result(Advice.RUN, "floor far below limit during price peak", "floor")
        return result(Advice.WAIT, "price peak: avoid starting")

    if floor_low and not room_warm:
        return result(Advice.RUN, "floor below minimum margin", "floor")

    if (
        surplus is not None
        and surplus >= settings.pv_run_threshold_w
        and (floor_margin is None or floor_margin < settings.floor_full_margin)
    ):
        return result(Advice.RUN, "PV surplus available", "pv")

    if floor_low:
        return result(Advice.WAIT, "floor low but living room warm enough")
    return result(Advice.WAIT, "no run needed")


PV_START_WINDOW = timedelta(minutes=5)


def average_surplus(samples: list[tuple[datetime, float]], now: datetime,
                    window: timedelta = PV_START_WINDOW) -> float:
    """Average solar surplus (W) over the window, 0 while less than the window is known
    (after a restart a run on solar waits until five minutes of readings are in)."""
    recent = [(t, v) for t, v in samples if now - t <= window]
    if not recent or now - min(t for t, _ in recent) < window - timedelta(minutes=1):
        return 0.0
    return sum(v for _, v in recent) / len(recent)


def confirm_pv_start(evaluation: Evaluation, avg_surplus_w: float, threshold_w: float,
                     forecast_ok: bool) -> Evaluation:
    """A run on solar only starts on the 5-minute average surplus and when the forecast for
    the coming hour also gives a surplus. A short burst of sun between clouds would
    otherwise start a run that then goes on for the minimum run time on grid power."""
    if evaluation.advice != Advice.RUN or evaluation.cause != "pv":
        return evaluation
    if avg_surplus_w < threshold_w:
        return replace(evaluation, advice=Advice.WAIT, cause=None,
                       reason="solar surplus not steady yet (5-minute average below the threshold)")
    if not forecast_ok:
        return replace(evaluation, advice=Advice.WAIT, cause=None,
                       reason="solar surplus expected to drop within the hour")
    return evaluation


def pv_forecast_ok(pv_kw_next_hour: list[float], base_load_kw: float, threshold_w: float) -> bool:
    """The expected solar production for the coming hour minus the base load is at least
    half the start threshold (the forecast is coarse: hourly and often on the low side).
    Without a forecast the start is not held back."""
    if not pv_kw_next_hour:
        return True
    expected_w = (sum(pv_kw_next_hour) / len(pv_kw_next_hour) - base_load_kw) * 1000
    return expected_w >= threshold_w / 2
