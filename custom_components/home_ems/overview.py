"""Energy and cost overview per month and year (pure logic, no Home Assistant imports).

Replaces the hand-kept spreadsheet: per month gas, solar, export, import, use
(general / heat pump / wallbox), costs and advance payments, with a forecast for
the rest of the year. Input is one record per day, read from the long-term
statistics of Home Assistant; contracts and advances come from a small settings
dict (the YAML in the options).
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass, field, replace
from datetime import date, timedelta

# Monthly mean temperature De Bilt 1991-2020 (KNMI), used for the forecast.
NORMAL_TEMP = (3.6, 3.9, 6.5, 9.8, 13.4, 16.2, 18.3, 17.9, 14.7, 10.9, 7.0, 4.2)
# Typical share of the yearly solar yield per month in the Netherlands.
NORMAL_PV = (0.025, 0.043, 0.079, 0.115, 0.135, 0.135, 0.135, 0.121, 0.090, 0.060, 0.027, 0.017)
HDD_BASE = 18.0        # base temperature for heating degree days
HP_MIN_MONTH = 50.0    # kWh: months with at least this much heat pump use mark the start of the heat pump
LEARN_MONTHS = 12      # the forecast learns from the most recent complete months (habits change)
NEIGHBOURS = 3         # days on either side used to repair a glitch day


@dataclass
class Usage:
    """Energy of one day or one month. Costs only where a statistic provides them."""

    import_kwh: float = 0.0
    export_kwh: float = 0.0
    pv_kwh: float = 0.0
    gas_m3: float = 0.0
    hp_kwh: float = 0.0
    ev_kwh: float = 0.0
    import_cost: float | None = None      # actual cost from the supplier statistic (dynamic contract)
    export_revenue: float | None = None   # actual compensation from the supplier statistic
    temp_sum: float = 0.0                 # sum of daily mean outside temperatures
    temp_days: int = 0

    def __add__(self, other: Usage) -> Usage:
        def opt(a: float | None, b: float | None) -> float | None:
            return None if a is None and b is None else (a or 0.0) + (b or 0.0)

        return Usage(
            self.import_kwh + other.import_kwh, self.export_kwh + other.export_kwh,
            self.pv_kwh + other.pv_kwh, self.gas_m3 + other.gas_m3,
            self.hp_kwh + other.hp_kwh, self.ev_kwh + other.ev_kwh,
            opt(self.import_cost, other.import_cost), opt(self.export_revenue, other.export_revenue),
            self.temp_sum + other.temp_sum, self.temp_days + other.temp_days,
        )

    def scaled(self, factor: float) -> Usage:
        return Usage(
            self.import_kwh * factor, self.export_kwh * factor, self.pv_kwh * factor,
            self.gas_m3 * factor, self.hp_kwh * factor, self.ev_kwh * factor,
            None, None, self.temp_sum * factor, round(self.temp_days * factor),
        )

    @property
    def total_kwh(self) -> float:
        """All electricity used in the house: grid import plus solar used directly."""
        return self.import_kwh + self.pv_kwh - self.export_kwh

    @property
    def general_kwh(self) -> float:
        return self.total_kwh - self.hp_kwh - self.ev_kwh

    @property
    def mean_temp(self) -> float | None:
        return self.temp_sum / self.temp_days if self.temp_days else None


# ---------------------------------------------------------------- settings


@dataclass(frozen=True)
class Contract:
    start: date
    name: str = ""
    import_price: float = 0.0      # EUR/kWh all-in (incl. energy tax and VAT)
    export_price: float = 0.0      # EUR/kWh for export after net metering ends
    gas_price: float = 0.0         # EUR/m3 all-in
    fixed_per_day: float = 0.0     # supplier + grid operator - energy tax reduction, per day
    dynamic: bool = False          # prices above are then only expectations for the forecast


@dataclass(frozen=True)
class Period:
    start: date
    end: date | None
    value: float

    def active(self, day: date) -> bool:
        return self.start <= day and (self.end is None or day <= self.end)


@dataclass(frozen=True)
class CostSettings:
    contracts: tuple[Contract, ...] = ()
    net_metering_until: date = date(2026, 12, 31)
    advances: tuple[Period, ...] = ()      # EUR per month, from the start date on
    ev_refunds: tuple[Period, ...] = ()    # EUR per kWh charged
    cashback: dict[int, float] = field(default_factory=dict)
    settlements: dict[int, float] = field(default_factory=dict)   # actual final bill per year
    manual: dict[tuple[date, date], Usage] = field(default_factory=dict)  # periods entered by hand

    def contract(self, day: date) -> Contract | None:
        current = None
        for c in self.contracts:
            if c.start <= day:
                current = c
        return current

    def advance(self, month_start: date) -> float:
        current = 0.0
        for p in sorted(self.advances, key=lambda p: p.start):
            if p.start <= month_start:
                current = p.value
        return current

    def ev_refund(self, day: date) -> float:
        return next((p.value for p in self.ev_refunds if p.active(day)), 0.0)


def _date(value) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def _period_key(value) -> tuple[date, date]:
    """'2025-03' (a whole month) or '2026-01-01/2026-01-13' (inclusive)."""
    text = str(value).strip()
    if "/" in text:
        first, last = (date.fromisoformat(p.strip()) for p in text.split("/", 1))
        if last < first:
            raise ValueError(f"period ends before it starts: {text}")
        return first, last
    first = date.fromisoformat(f"{text}-01" if len(text) == 7 else text).replace(day=1)
    return first, first.replace(day=calendar.monthrange(first.year, first.month)[1])


def _manual_usage(span: tuple[date, date], v: dict) -> Usage:
    n = (span[1] - span[0]).days + 1
    temp = v.get("temp")
    return Usage(
        import_kwh=float(v.get("afname", 0)), export_kwh=float(v.get("teruglevering", 0)),
        pv_kwh=float(v.get("opwek", 0)), gas_m3=float(v.get("gas", 0)),
        hp_kwh=float(v.get("warmtepomp", 0)), ev_kwh=float(v.get("laadpaal", 0)),
        temp_sum=float(temp) * n if temp is not None else 0.0, temp_days=n if temp is not None else 0,
    )


def parse_settings(raw: dict | None) -> CostSettings:
    """Settings from the YAML in the options (Dutch keys). Raises ValueError on bad input."""
    raw = raw or {}
    if not isinstance(raw, dict):
        raise ValueError("expected a mapping")
    try:
        contracts = tuple(sorted((
            Contract(
                start=_date(c["vanaf"]), name=str(c.get("naam", "")),
                import_price=float(c.get("stroom", 0)), export_price=float(c.get("teruglevering", 0)),
                gas_price=float(c.get("gas", 0)), fixed_per_day=float(c.get("vast_per_dag", 0)),
                dynamic=bool(c.get("dynamisch", False)),
            ) for c in raw.get("contracten") or []), key=lambda c: c.start))
        advances = tuple(Period(_date(a["vanaf"]), None, float(a["bedrag"]))
                         for a in raw.get("voorschotten") or [])
        refunds = tuple(Period(_date(r["vanaf"]), _date(r["tot"]) if r.get("tot") else None,
                               float(r["per_kwh"]))
                        for r in raw.get("laadpaal_vergoeding") or [])
        return CostSettings(
            contracts=contracts,
            net_metering_until=_date(raw.get("saldering_tot", "2026-12-31")),
            advances=advances, ev_refunds=refunds,
            cashback={int(k): float(v) for k, v in (raw.get("cashback") or {}).items()},
            settlements={int(k): float(v) for k, v in (raw.get("afrekening") or {}).items()},
            manual={(span := _period_key(k)): _manual_usage(span, v)
                    for k, v in (raw.get("handmatig") or {}).items()},
        )
    except (KeyError, TypeError, ValueError) as err:
        raise ValueError(str(err)) from err


# ---------------------------------------------------------------- input cleaning


def clean_daily(values: dict[date, float], floor: float = 100.0,
                factor: float = 8.0) -> tuple[dict[date, float], list[tuple[date, float]]]:
    """Repair glitches in the daily change of one statistic: negative days (a meter
    that went back) and days far above the normal level (a sensor that briefly
    reported a wrong total). The limit is `factor` times the median of the positive
    days, but never below `floor`. Such a day gets the mean of the good days up to
    `NEIGHBOURS` days before and after it (the real use or yield of that day is lost
    in the glitch), or the median if there are none; the glitches are returned too."""
    positive = sorted(v for v in values.values() if v > 0)
    median = positive[len(positive) // 2] if positive else 0.0
    limit = max(floor, factor * median)
    clean: dict[date, float] = {}
    dropped = []
    for day, value in values.items():
        if value < -0.01 or value > limit:
            dropped.append((day, value))
        else:
            clean[day] = max(0.0, value)
    for day, _ in dropped:
        near = [clean[d] for i in range(1, NEIGHBOURS + 1)
                for d in (day - timedelta(days=i), day + timedelta(days=i)) if d in clean]
        clean[day] = sum(near) / len(near) if near else median
    return clean, sorted(dropped)


def apply_manual(days: dict[date, Usage],
                 manual: dict[tuple[date, date], Usage]) -> dict[date, Usage]:
    """Replace the days of every hand-entered period by that period spread evenly."""
    out = {d: u for d, u in days.items()
           if not any(first <= d <= last for first, last in manual)}
    for (first, last), u in manual.items():
        n = (last - first).days + 1
        per_day = Usage(
            u.import_kwh / n, u.export_kwh / n, u.pv_kwh / n, u.gas_m3 / n, u.hp_kwh / n, u.ev_kwh / n,
            temp_sum=u.temp_sum / n, temp_days=1 if u.temp_days else 0,
        )
        for i in range(n):
            out[first + timedelta(days=i)] = per_day
    return out


# ---------------------------------------------------------------- costs


@dataclass
class Costs:
    fixed: float = 0.0
    gas: float = 0.0
    power: float = 0.0
    ev_refund: float = 0.0     # negative: money back

    @property
    def total(self) -> float:
        return self.fixed + self.gas + self.power + self.ev_refund

    def __add__(self, other: Costs) -> Costs:
        return Costs(self.fixed + other.fixed, self.gas + other.gas,
                     self.power + other.power, self.ev_refund + other.ev_refund)


def day_costs(day: date, u: Usage, s: CostSettings) -> Costs:
    c = s.contract(day)
    if c is None:
        return Costs(ev_refund=-u.ev_kwh * s.ev_refund(day))
    if day <= s.net_metering_until:
        power = (u.import_kwh - u.export_kwh) * c.import_price
    elif c.dynamic and u.import_cost is not None:
        power = u.import_cost - (u.export_revenue or 0.0)
    else:
        power = u.import_kwh * c.import_price - u.export_kwh * c.export_price
    return Costs(c.fixed_per_day, u.gas_m3 * c.gas_price, power, -u.ev_kwh * s.ev_refund(day))


# ---------------------------------------------------------------- forecast


def month_start(day: date) -> date:
    return day.replace(day=1)


def month_days(m: date) -> int:
    return calendar.monthrange(m.year, m.month)[1]


def add_months(m: date, n: int) -> date:
    y, mo = divmod(m.month - 1 + n, 12)
    return date(m.year + y, mo + 1, 1)


def by_month(days: dict[date, Usage]) -> dict[date, Usage]:
    months: dict[date, Usage] = {}
    for d, u in days.items():
        k = month_start(d)
        months[k] = months.get(k, Usage()) + u
    return months


def _fit(points: list[tuple[float, float]]) -> tuple[float, float] | None:
    """Least squares y = a + b x; None with fewer than 3 points or no spread."""
    if len(points) < 3:
        return None
    n = len(points)
    mx = sum(x for x, _ in points) / n
    my = sum(y for _, y in points) / n
    sxx = sum((x - mx) ** 2 for x, _ in points)
    if sxx < 1e-9:
        return None
    b = sum((x - mx) * (y - my) for x, y in points) / sxx
    return my - b * mx, b


@dataclass(frozen=True)
class Forecaster:
    """Learns from complete months and predicts a month from normals."""

    months: dict[date, Usage]                 # complete months only
    hp_fit: tuple[float, float] | None        # kWh per day = a + b * HDD per day
    gas_fit: tuple[float, float] | None       # m3 per day = a + b * HDD per day
    pv_year: float | None                     # learned yearly solar yield

    @classmethod
    def learn(cls, months: dict[date, Usage]) -> Forecaster:
        hp_start = min((m for m, u in months.items() if u.hp_kwh >= HP_MIN_MONTH), default=None)
        usable = sorted(m for m, u in months.items()
                        if hp_start is not None and m >= hp_start and u.mean_temp is not None)
        recent = set(usable[-LEARN_MONTHS:])
        hp_pts, gas_pts = [], []
        for m, u in months.items():
            if m not in recent:
                continue
            hdd = max(0.0, HDD_BASE - u.mean_temp)
            n = month_days(m)
            hp_pts.append((hdd, u.hp_kwh / n))
            gas_pts.append((hdd, u.gas_m3 / n))
        shares = sum(NORMAL_PV)
        pv = [(u.pv_kwh, NORMAL_PV[m.month - 1] / shares) for m, u in months.items() if u.pv_kwh > 0]
        pv_year = sum(k for k, _ in pv) / sum(s for _, s in pv) if pv else None
        return cls(months, _fit(hp_pts), _fit(gas_pts), pv_year)

    def _same_month(self, m: date, attr: str) -> float | None:
        values = [getattr(u, attr) if attr != "general_kwh" else u.general_kwh
                  for k, u in self.months.items() if k.month == m.month and k < m]
        return sum(values) / len(values) if values else None

    def _recent(self, m: date, attr: str, n: int = 3) -> list[float]:
        keys = sorted(k for k in self.months if k < m)[-n:]
        return [self.months[k].general_kwh if attr == "general_kwh" else getattr(self.months[k], attr)
                for k in keys]

    def _trend(self, m: date) -> float:
        """This year's last 3 months of general use against the same months a year earlier."""
        keys = sorted(k for k in self.months if k < m)[-3:]
        now = sum(self.months[k].general_kwh for k in keys)
        before = [self.months.get(add_months(k, -12)) for k in keys]
        if not keys or any(b is None for b in before):
            return 1.0
        prev = sum(b.general_kwh for b in before)
        return min(1.5, max(0.67, now / prev)) if prev > 0 else 1.0

    def predict(self, m: date) -> Usage:
        n = month_days(m)
        temp = NORMAL_TEMP[m.month - 1]
        hdd = max(0.0, HDD_BASE - temp)

        def regress(fit, attr):
            if fit is not None:
                return max(0.0, (fit[0] + fit[1] * hdd) * n)
            return self._same_month(m, attr) or 0.0

        hp = regress(self.hp_fit, "hp_kwh")
        gas = regress(self.gas_fit, "gas_m3")
        # solar: the own yield of this month in earlier years (orientation and shade
        # differ per roof), else the Dutch profile scaled to the own yearly yield
        pv = self._same_month(m, "pv_kwh")
        if pv is None:
            pv = (self.pv_year * NORMAL_PV[m.month - 1] / sum(NORMAL_PV)) if self.pv_year else 0.0
        general = self._same_month(m, "general_kwh")
        if general is None:
            recent = self._recent(m, "general_kwh", 12)
            general = sum(recent) / len(recent) if recent else 0.0
        else:
            general *= self._trend(m)
        # wallbox: the last complete month (a car comes or goes at once)
        recent_ev = self._recent(m, "ev_kwh", 1)
        ev = recent_ev[0] if recent_ev else 0.0
        total = general + hp + ev
        # share of the solar yield used in the house, from the same month earlier
        def share(u: Usage) -> float:   # clipped: the meters do not always agree
            return min(1.0, max(0.0, (u.pv_kwh - u.export_kwh) / u.pv_kwh))

        shares = [share(u) for k, u in self.months.items() if k.month == m.month and u.pv_kwh > 0]
        if not shares:
            shares = [share(u) for u in self.months.values() if u.pv_kwh > 0]
        own = min(pv, total, pv * (sum(shares) / len(shares) if shares else 0.3))
        return Usage(import_kwh=total - own, export_kwh=pv - own, pv_kwh=pv, gas_m3=gas,
                     hp_kwh=hp, ev_kwh=ev, temp_sum=temp * n, temp_days=n)


# ---------------------------------------------------------------- overview


@dataclass(frozen=True)
class MonthRow:
    month: date
    status: str          # "actual", "running" (current month: actual so far + forecast) or "forecast"
    usage: Usage
    costs: Costs
    advance: float
    first_day: int = 1   # rows of a month split by a contract change cover only part of it
    last_day: int = 0    # 0 = until the end of the month

    @property
    def partial(self) -> bool:
        return self.first_day != 1 or self.last_day not in (0, month_days(self.month))

    def as_dict(self) -> dict:
        u, c = self.usage, self.costs
        own = (u.pv_kwh - u.export_kwh) / u.pv_kwh * 100 if u.pv_kwh > 0 else None
        last = self.last_day or month_days(self.month)
        return {
            "month": self.month.strftime("%Y-%m"),
            "days": f"{self.first_day}-{last}" if self.partial else None,
            "status": self.status,
            "gas_m3": round(u.gas_m3, 1), "pv_kwh": round(u.pv_kwh), "export_kwh": round(u.export_kwh),
            "import_kwh": round(u.import_kwh), "net_kwh": round(u.import_kwh - u.export_kwh),
            "total_kwh": round(u.total_kwh), "general_kwh": round(u.general_kwh),
            "heat_pump_kwh": round(u.hp_kwh), "ev_kwh": round(u.ev_kwh),
            "own_use_pct": round(own) if own is not None else None,
            "mean_temp": round(u.mean_temp, 1) if u.mean_temp is not None else None,
            "fixed": round(c.fixed, 2), "gas_cost": round(c.gas, 2), "power_cost": round(c.power, 2),
            "ev_refund": round(c.ev_refund, 2), "total": round(c.total, 2), "advance": round(self.advance, 2),
        }


@dataclass(frozen=True)
class YearSummary:
    """One settlement year: from a contract start (or its anniversary) to the day before
    the next contract or anniversary, as on the yearly bill."""

    start: date
    end: date            # inclusive
    name: str
    rows: tuple[MonthRow, ...]
    cashback: float
    settlement: float | None

    @property
    def year(self) -> int:
        return self.start.year

    @property
    def usage(self) -> Usage:
        total = Usage()
        for r in self.rows:
            total = total + r.usage
        return total

    @property
    def costs(self) -> float:
        return sum(r.costs.total for r in self.rows) - self.cashback

    @property
    def advances(self) -> float:
        return sum(r.advance for r in self.rows)

    @property
    def complete(self) -> bool:
        return all(r.status == "actual" for r in self.rows)

    def as_dict(self) -> dict:
        u = self.usage
        own = (u.pv_kwh - u.export_kwh) / u.pv_kwh * 100 if u.pv_kwh > 0 else None
        cum_cost = cum_adv = 0.0
        months = []
        for r in self.rows:
            d = r.as_dict()
            cum_cost += r.costs.total
            cum_adv += r.advance
            d["cum_total"], d["cum_advance"] = round(cum_cost, 2), round(cum_adv, 2)
            months.append(d)
        return {
            "year": self.year, "from": self.start.isoformat(), "to": self.end.isoformat(),
            "contract": self.name, "complete": self.complete,
            "gas_m3": round(u.gas_m3), "pv_kwh": round(u.pv_kwh), "export_kwh": round(u.export_kwh),
            "import_kwh": round(u.import_kwh), "net_kwh": round(u.import_kwh - u.export_kwh),
            "total_kwh": round(u.total_kwh), "general_kwh": round(u.general_kwh),
            "heat_pump_kwh": round(u.hp_kwh), "ev_kwh": round(u.ev_kwh),
            "own_use_pct": round(own) if own is not None else None,
            "advances": round(self.advances, 2), "cashback": round(self.cashback, 2),
            "costs": round(self.costs, 2),
            # positive: pay extra at the final bill; negative: money back
            "expected_settlement": round(self.costs - self.advances, 2),
            "settlement": self.settlement, "months": months,
        }


def settlement_years(s: CostSettings, until: date) -> list[tuple[date, date, str]]:
    """(start, end, contract name) per settlement year up to the one containing `until`.

    A contract runs to the day before the next one; a contract longer than a year
    (or the last one) is cut into years from its start date. A remainder of up to a
    month is added to the last year instead of becoming a year of its own."""
    out = []
    contracts = list(s.contracts)
    for i, c in enumerate(contracts):
        stop = contracts[i + 1].start - timedelta(days=1) if i + 1 < len(contracts) else None
        start = c.start
        while start <= until and (stop is None or start <= stop):
            nxt = start.replace(year=start.year + 1) - timedelta(days=1)
            if stop is not None and (nxt >= stop or (stop - nxt).days <= 31):
                nxt = stop
            out.append((start, nxt, c.name))
            start = nxt + timedelta(days=1)
    return out


def _net_metering_surplus(rows: list[MonthRow], s: CostSettings) -> list[MonthRow]:
    """Net metering pays the purchase price only up to the import of the settlement
    year; a net export surplus earns the (lower) export price. The rows credit every
    exported kWh at the purchase price, so correct the last net-metering row."""
    metered = [r for r in rows if r.month <= s.net_metering_until]
    if not metered:
        return rows
    surplus = sum(r.usage.export_kwh - r.usage.import_kwh for r in metered)
    last = metered[-1]
    last_day = min(s.net_metering_until, last.month.replace(day=last.last_day or month_days(last.month)))
    contract = s.contract(last_day)
    if surplus <= 0 or contract is None:
        return rows
    fix = surplus * (contract.import_price - contract.export_price)
    fixed = replace(last, costs=last.costs + Costs(power=fix))
    return [fixed if r is last else r for r in rows]


def build_overview(days: dict[date, Usage], today: date, s: CostSettings) -> list[YearSummary]:
    """Settlement years (see `settlement_years`) from the first contract up to the one
    that contains today, each split in calendar months.

    Days before today are actual, the rest is forecast; the current month is actual so
    far plus the forecast for its remaining days. A month's advance payment is booked
    on its last day, so a month split by a contract change counts it once."""
    current = month_start(today)
    forecaster = Forecaster.learn(by_month({d: u for d, u in days.items() if d < current}))
    years = []
    for start, end, name in settlement_years(s, today):
        rows = []
        m = month_start(start)
        while m <= end:
            n = month_days(m)
            first = max(start, m)
            last = min(end, m + timedelta(days=n - 1))
            span = [first + timedelta(days=i) for i in range((last - first).days + 1)]
            if last < today and not any(d in days for d in span):
                m = add_months(m, 1)
                continue
            status = "actual" if last < today else "forecast" if first >= today else "running"
            per_day = forecaster.predict(m).scaled(1 / n) if last >= today else None
            usage, costs = Usage(), Costs()
            for d in span:
                u = days.get(d, Usage()) if d < today else per_day
                usage, costs = usage + u, costs + day_costs(d, u, s)
            month_end = m + timedelta(days=n - 1)
            advance = s.advance(m) if last == month_end else 0.0
            rows.append(MonthRow(m, status, usage, costs, advance, first.day,
                                 0 if last == month_end else last.day))
            m = add_months(m, 1)
        rows = _net_metering_surplus(rows, s)
        if rows:
            years.append(YearSummary(start, end, name, tuple(rows), s.cashback.get(start.year, 0.0),
                                     s.settlements.get(start.year)))
    return years


CSV_COLUMNS = (
    ("Maand", "month"), ("Dagen", "days"), ("Status", "status"), ("Gas m3", "gas_m3"), ("Opwek kWh", "pv_kwh"),
    ("Teruglevering kWh", "export_kwh"), ("Afname kWh", "import_kwh"), ("Afname - teruglevering", "net_kwh"),
    ("Verbruik totaal kWh", "total_kwh"), ("Algemeen kWh", "general_kwh"), ("Warmtepomp kWh", "heat_pump_kwh"),
    ("Laadpaal kWh", "ev_kwh"), ("Eigen verbruik %", "own_use_pct"), ("Buitentemp gem", "mean_temp"),
    ("Vaste kosten", "fixed"), ("Gas EUR", "gas_cost"), ("Stroom EUR", "power_cost"),
    ("Retour laadpaal", "ev_refund"), ("Totaal", "total"), ("Voorschot", "advance"),
    ("Cum totaal", "cum_total"), ("Cum voorschot", "cum_advance"),
)


def to_csv(summary: YearSummary) -> str:
    """Semicolon separated with a decimal comma, so Dutch Excel opens it directly."""
    def cell(v) -> str:
        if v is None:
            return ""
        if isinstance(v, float):
            return f"{v:.2f}".replace(".", ",")
        return str(v)

    data = summary.as_dict()
    lines = [";".join(h for h, _ in CSV_COLUMNS)]
    lines += [";".join(cell(m.get(k)) for _, k in CSV_COLUMNS) for m in data["months"]]
    lines.append("")
    for label, key in (("Eigen verbruik %", "own_use_pct"), ("Voorschotten", "advances"),
                       ("Cashback", "cashback"), ("Totale kosten", "costs"),
                       ("Verwachte afrekening (+ bijbetalen)", "expected_settlement"),
                       ("Volgens afrekening", "settlement")):
        lines.append(f"{label};{cell(data.get(key))}")
    return "\n".join(lines) + "\n"


def years_csv(years: list[YearSummary]) -> str:
    head = ("Jaar;Van;Tot;Contract;Compleet;Gas m3;Opwek kWh;Teruglevering kWh;Afname kWh;Verbruik totaal kWh;"
            "Algemeen kWh;Warmtepomp kWh;Laadpaal kWh;Eigen verbruik %;Voorschotten;Cashback;"
            "Totale kosten;Verwachte afrekening;Volgens afrekening")
    keys = ("year", "from", "to", "contract", "complete", "gas_m3", "pv_kwh", "export_kwh", "import_kwh", "total_kwh",
            "general_kwh", "heat_pump_kwh", "ev_kwh", "own_use_pct", "advances", "cashback",
            "costs", "expected_settlement", "settlement")
    out = [head]
    for y in years:
        d = y.as_dict()
        out.append(";".join("" if d[k] is None else
                            (f"{d[k]:.2f}".replace(".", ",") if isinstance(d[k], float) else
                             ("ja" if d[k] is True else "nee" if d[k] is False else str(d[k])))
                            for k in keys))
    return "\n".join(out) + "\n"


__all__ = ["settlement_years", "clean_daily", "apply_manual", "Usage", "Contract", "Period", "CostSettings", "parse_settings", "Costs", "day_costs",
           "Forecaster", "MonthRow", "YearSummary", "build_overview", "to_csv", "years_csv", "replace"]
