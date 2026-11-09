"""Day planner for the heat pump (pure logic, no Home Assistant imports).

Estimates the heat the house needs until the end of the horizon from the outdoor
temperature forecast and the degree-hour model, and places that many run-hours in
the cheapest quarters. "Cheapest" is the cost per kWh of heat: the electricity
price where the heat pump draws from the grid, the lost feed-in value where it
runs on solar surplus, both divided by the COP at that outdoor temperature.

Observation mode: the plan is only shown, nothing is switched.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo

QUARTER = timedelta(minutes=15)


@dataclass(frozen=True)
class PlannerSettings:
    """Defaults from the analysis of 14-23 Oct 2026 (Energie notes)."""

    heat_base_kwh_day: float = -5.3   # heat per day = base + per_dh * degree-hours
    heat_per_dh: float = 0.217        # kWh heat per degree-hour (18 C base)
    base_temp: float = 18.0
    # Heat delivered per run-hour at 0 C outside, and how much less per degree warmer
    # (the heat pump follows its own heating curve: at 9 C a Quatt Duo gave ~3.4 kW,
    # at 2.5 C ~6 kW). Slope 0 = the same rate at every temperature.
    heat_rate_kw: float = 2.9
    heat_rate_slope: float = 0.0
    heat_rate_min: float = 1.0
    cop_intercept: float = 3.5        # COP = intercept + slope * outdoor temp, clipped
    cop_slope: float = 0.15
    cop_min: float = 2.5
    cop_max: float = 7.0
    base_load_kw: float = 0.4         # house load besides the heat pump
    block: timedelta = timedelta(hours=1)      # minimum length of a planned run
    max_gap: timedelta = timedelta(hours=8)    # longest time without a run
    # Pauses shorter than this between planned runs are filled, so the runs merge into
    # one long run. In the cold the heat need is close to what one heat pump gives at
    # low power, and running on is far more efficient than stopping and starting again
    # (restarts at high power, both heat pumps together, lower COP).
    min_pause: timedelta = timedelta(hours=2)
    horizon: timedelta = timedelta(hours=36)
    # State of the house: heat stored above (or missing below) the target temperature
    # changes the heat need by capacity x difference. Capacity from a cooling test
    # (the low side of the estimate, so the house is assumed to cool down faster).
    capacity_kwh_k: float = 15.0
    target_temp: float = 19.0
    max_state_k: float = 2.0          # cap on the difference that is counted
    # Hybrid: the heat pump decides itself between compressor and gas boiler, but only
    # when the thermostat asks for heat. So where the boiler gives cheaper heat (frost,
    # price peak) the quarter is valued at the gas cost (no electricity), not skipped.
    gas_price_m3: float | None = None  # all-in EUR per m3; None = ignore gas
    gas_kwh_m3: float = 9.77           # calorific value
    boiler_efficiency: float = 0.9
    # No fixed peak window: the quarter prices themselves carry the morning and
    # evening peaks, so the cheapest blocks already avoid them.
    tz: tzinfo = ZoneInfo("Europe/Amsterdam")


@dataclass(frozen=True)
class Slot:
    """One quarter hour of input. Prices in EUR/kWh."""

    start: datetime
    price: float                 # all-in purchase price
    market: float | None = None  # market price, used as the value of exported solar
    forecast: bool = False       # price is a forecast, not the auction result
    pv_kw: float = 0.0           # expected solar production
    temp: float | None = None    # expected outdoor temperature


@dataclass(frozen=True)
class SlotCost:
    slot: Slot
    cop: float
    el_kw: float
    pv_share: float              # part of the heat pump power covered by solar surplus
    cost: float                  # EUR for running this quarter
    cost_per_kwh_heat: float
    heat_kw: float = 2.9         # heat delivered while running this quarter


@dataclass(frozen=True)
class PlannedRun:
    start: datetime
    end: datetime
    heat_kwh: float
    el_kwh: float
    cost: float
    pv_share: float
    forecast_price: bool         # part of the run relies on forecast prices
    reason: str                  # "cheapest" or "max_gap"

    def to_dict(self) -> dict:
        return {
            "start": self.start.isoformat(), "end": self.end.isoformat(),
            "heat_kwh": round(self.heat_kwh, 2), "el_kwh": round(self.el_kwh, 2),
            "cost": round(self.cost, 3), "pv_share": round(self.pv_share, 2),
            "forecast_price": self.forecast_price, "reason": self.reason,
        }


@dataclass(frozen=True)
class Plan:
    made: datetime
    horizon_end: datetime
    heat_need_kwh: float
    runs: tuple[PlannedRun, ...] = field(default_factory=tuple)
    expected_cost: float = 0.0
    baseline_cost: float = 0.0   # same heat at the average cost per kWh heat of the horizon
    state_kwh: float = 0.0       # part of the heat need from the room being below target
    gas_cost_per_kwh_heat: float | None = None
    gas_cheaper_quarters: int = 0  # quarters where the boiler gives cheaper heat (valued at gas)

    @property
    def run_hours(self) -> float:
        return sum((r.end - r.start).total_seconds() for r in self.runs) / 3600

    @property
    def savings(self) -> float:
        return self.baseline_cost - self.expected_cost

    def next_run(self, now: datetime) -> PlannedRun | None:
        return next((r for r in self.runs if r.end > now), None)

    def running_now(self, now: datetime) -> bool:
        return any(r.start <= now < r.end for r in self.runs)

    def run_soon(self, now: datetime, within: timedelta) -> bool:
        """A planned run starts within `within` from now (not counting one in progress)."""
        return any(now < r.start <= now + within for r in self.runs)


def hour_of(moment: datetime) -> datetime:
    """Start of the hour in UTC (aware datetimes only)."""
    return moment.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def build_slots(price_rows: list[dict], solar_hours: list[dict] | None = None,
                temps: dict[datetime, float] | None = None,
                fallback_temp: float | None = None) -> list[Slot]:
    """Slots from the Jeroen.nl attributes: `prices` rows ({start, price, market,
    forecast}) and solar `hours` ({start, kwh}); `temps` maps UTC hour starts to the
    forecast temperature."""
    pv: dict[datetime, float] = {}
    for item in solar_hours or []:
        try:
            pv[hour_of(datetime.fromisoformat(str(item["start"])))] = float(item.get("kwh") or 0.0)
        except (KeyError, ValueError):
            continue
    temps = temps or {}
    slots: list[Slot] = []
    for row in price_rows or []:
        try:
            start = datetime.fromisoformat(str(row["start"]))
            price = float(row["price"])
        except (KeyError, TypeError, ValueError):
            continue
        hour = hour_of(start)
        market = row.get("market")
        slots.append(Slot(start=start, price=price,
                          market=float(market) if market is not None else None,
                          forecast=bool(row.get("forecast")), pv_kw=pv.get(hour, 0.0),
                          temp=temps.get(hour, fallback_temp)))
    return slots


def cop_at(temp: float | None, s: PlannerSettings) -> float:
    if temp is None:
        temp = 8.0
    return min(s.cop_max, max(s.cop_min, s.cop_intercept + s.cop_slope * temp))


def heat_rate_at(temp: float | None, s: PlannerSettings) -> float:
    """Heat (kW) the heat pump delivers while it runs at this outdoor temperature."""
    if temp is None:
        temp = 8.0
    return max(s.heat_rate_min, s.heat_rate_kw - s.heat_rate_slope * temp)


def slot_cost(slot: Slot, s: PlannerSettings) -> SlotCost:
    cop = cop_at(slot.temp, s)
    rate = heat_rate_at(slot.temp, s)
    el_kw = rate / cop
    surplus = max(0.0, slot.pv_kw - s.base_load_kw)
    pv_kw = min(el_kw, surplus)
    export_value = slot.market if slot.market is not None else 0.0
    hours = QUARTER.total_seconds() / 3600
    cost = (pv_kw * export_value + (el_kw - pv_kw) * slot.price) * hours
    return SlotCost(slot, cop, el_kw, pv_kw / el_kw if el_kw else 0.0, cost,
                    cost / (rate * hours), rate)


def gas_heat_cost(s: PlannerSettings) -> float | None:
    """EUR per kWh heat from the gas boiler, None when no gas price is known."""
    if s.gas_price_m3 is None:
        return None
    return s.gas_price_m3 / (s.gas_kwh_m3 * s.boiler_efficiency)


def heat_need(slots: list[Slot], s: PlannerSettings) -> float:
    """Heat needed over the slots (kWh): degree-hours times the model slope, plus
    the (negative) daily intercept spread over the time. Never below zero."""
    hours = QUARTER.total_seconds() / 3600
    dh = sum(max(0.0, s.base_temp - (x.temp if x.temp is not None else s.base_temp)) * hours
             for x in slots)
    span_days = len(slots) * hours / 24
    return max(0.0, s.heat_base_kwh_day * span_days + s.heat_per_dh * dh)


def state_heat(living_temp: float | None, s: PlannerSettings) -> float:
    """Extra heat (kWh, negative = stored heat to use up) to bring the house to target."""
    if living_temp is None:
        return 0.0
    diff = max(-s.max_state_k, min(s.max_state_k, s.target_temp - living_temp))
    return s.capacity_kwh_k * diff


def plan(slots: list[Slot], now: datetime, s: PlannerSettings | None = None,
         last_run_end: datetime | None = None, living_temp: float | None = None,
         keep_until: datetime | None = None) -> Plan | None:
    """Plan runs from `now` until the end of the horizon (or of the price data).

    `keep_until`: end of a run that is in progress (from the previous plan). Its quarters
    stay in the plan and count towards the heat need; the rest is planned after it. The
    plan is made again every few minutes, and without this a run in progress would drop
    out of the plan as soon as its first quarter had passed (stop after the minimum run
    time, start again a bit later)."""
    s = s or PlannerSettings()
    end = now + s.horizon
    slots = sorted((x for x in slots if x.start + QUARTER > now and x.start < end),
                   key=lambda x: x.start)
    if not slots:
        return None
    gas = gas_heat_cost(s)
    costs = [slot_cost(x, s) for x in slots]
    gas_quarters = 0
    if gas is not None:
        hours = QUARTER.total_seconds() / 3600
        for k, c in enumerate(costs):
            if c.cost_per_kwh_heat > gas:
                gas_quarters += 1
                costs[k] = SlotCost(c.slot, c.cop, 0.0, 0.0, gas * c.heat_kw * hours, gas, c.heat_kw)
    season_need = heat_need(slots, s)
    state = state_heat(living_temp, s) if season_need > 0 else 0.0   # only in the heating season
    need = max(0.0, season_need + state)
    kept = 0
    if keep_until is not None:
        kept = sum(1 for c in costs if c.slot.start < keep_until)
        need = max(0.0, need - sum(c.heat_kw for c in costs[:kept]) * QUARTER.total_seconds() / 3600)
    per_block = round(s.block / QUARTER)

    def contiguous(i: int) -> bool:
        return all(costs[i + k].slot.start == costs[i].slot.start + k * QUARTER
                   for k in range(per_block))

    starts = [i for i in range(kept, len(costs) - per_block + 1) if contiguous(i)]

    def block_cost(i: int) -> float:
        return sum(c.cost_per_kwh_heat for c in costs[i:i + per_block]) / per_block

    chosen: list[int] = []
    reasons: dict[int, str] = {}

    def free(i: int) -> bool:
        return all(abs(i - j) >= per_block for j in chosen)

    def block_heat(i: int) -> float:
        return sum(c.heat_kw for c in costs[i:i + per_block]) * QUARTER.total_seconds() / 3600

    planned = 0.0
    for i in sorted(starts, key=lambda i: (block_cost(i), i)):
        if planned >= need - 1e-9:
            break
        if free(i):
            chosen.append(i)
            reasons[i] = "cheapest"
            planned += block_heat(i)

    # comfort: no gap longer than max_gap, counted from the last run (or now); only
    # when there is heat demand at all, so not in summer
    previous = keep_until if kept else (last_run_end or now)
    while season_need > 0:   # also when stored heat covers the need, as a safety net
        runs_sorted = sorted(chosen)
        gap_start, gap_end = previous, None
        for i in runs_sorted:
            if costs[i].slot.start - gap_start > s.max_gap:
                gap_end = costs[i].slot.start
                break
            gap_start = costs[i].slot.start + per_block * QUARTER
        if gap_end is None and end - gap_start > s.max_gap and slots[-1].start - gap_start > s.max_gap:
            gap_end = slots[-1].start
        if gap_end is None:
            break
        window_end = gap_start + s.max_gap
        options = [i for i in starts if gap_start <= costs[i].slot.start
                   and costs[i].slot.start + per_block * QUARTER <= window_end and free(i)]
        if not options:
            break
        best = min(options, key=lambda i: (block_cost(i), i))
        chosen.append(best)
        reasons[best] = "max_gap"

    runs = _merge(sorted(chosen), costs, per_block, reasons, s, kept)
    avg_cost_heat = sum(c.cost_per_kwh_heat for c in costs) / len(costs)
    planned_heat = sum(r.heat_kwh for r in runs)
    return Plan(made=now, horizon_end=slots[-1].start + QUARTER, heat_need_kwh=round(need, 2),
                runs=tuple(runs), expected_cost=round(sum(r.cost for r in runs), 3),
                baseline_cost=round(avg_cost_heat * planned_heat, 3), state_kwh=round(state, 2),
                gas_cost_per_kwh_heat=round(gas, 4) if gas is not None else None,
                gas_cheaper_quarters=gas_quarters)


def _merge(chosen: list[int], costs: list[SlotCost], per_block: int, reasons: dict[int, str],
           s: PlannerSettings, kept: int = 0) -> list[PlannedRun]:
    """Chosen blocks to runs: adjacent quarters join, and pauses shorter than `min_pause`
    between runs are filled (only between contiguous quarters)."""
    hours = QUARTER.total_seconds() / 3600
    quarters_on = {q for i in chosen for q in range(i, i + per_block)} | set(range(kept))
    block_reason = {q: reasons.get(i) for i in chosen for q in range(i, i + per_block)}
    pause = round(s.min_pause / QUARTER)
    ordered = sorted(quarters_on)
    for a, b in zip(ordered, ordered[1:]):
        gap = b - a - 1
        contiguous = all(costs[q + 1].slot.start == costs[q].slot.start + QUARTER for q in range(a, b))
        if 0 < gap < pause and contiguous:
            quarters_on.update(range(a + 1, b))
    runs: list[PlannedRun] = []
    group: list[int] = []
    for q in sorted(quarters_on) + [None]:  # type: ignore[list-item]
        if group and (q is None or costs[q].slot.start != costs[group[-1]].slot.start + QUARTER):
            quarters = [costs[g] for g in group]
            el = sum(c.el_kw for c in quarters) * hours
            runs.append(PlannedRun(
                start=quarters[0].slot.start, end=quarters[-1].slot.start + QUARTER,
                heat_kwh=sum(c.heat_kw for c in quarters) * hours, el_kwh=el,
                cost=sum(c.cost for c in quarters),
                pv_share=sum(c.pv_share * c.el_kw for c in quarters) / max(el / hours, 1e-9),
                forecast_price=any(c.slot.forecast for c in quarters),
                reason="max_gap" if any(block_reason.get(g) == "max_gap" for g in group) else "cheapest",
            ))
            group = []
        if q is not None:
            group.append(q)
    return runs
