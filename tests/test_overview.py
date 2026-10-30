"""Tests for the energy and cost overview (no Home Assistant needed)."""
from __future__ import annotations

import importlib.util
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

_PATH = Path(__file__).parents[1] / "custom_components" / "home_ems" / "overview.py"
_SPEC = importlib.util.spec_from_file_location("home_ems_overview", _PATH)
ov = importlib.util.module_from_spec(_SPEC)
sys.modules["home_ems_overview"] = ov
_SPEC.loader.exec_module(ov)

SETTINGS = ov.parse_settings({
    "contracten": [
        {"naam": "vast 2025", "vanaf": "2025-01-01", "stroom": 0.28, "gas": 1.29, "vast_per_dag": 3.54},
        {"naam": "vast 2026", "vanaf": "2026-01-14", "stroom": 0.2285, "gas": 1.21, "vast_per_dag": 2.58},
        {"naam": "Tibber", "vanaf": "2027-01-16", "dynamisch": True, "stroom": 0.25,
         "teruglevering": 0.05, "gas": 1.25, "vast_per_dag": 2.6},
    ],
    "voorschotten": [{"vanaf": "2025-01-01", "bedrag": 261}, {"vanaf": "2026-02-01", "bedrag": 123}],
    "laadpaal_vergoeding": [{"vanaf": "2025-12-01", "tot": "2026-08-31", "per_kwh": 0.40}],
    "cashback": {2025: 400},
    "afrekening": {2025: 1854},
})


def flat_days(start: date, end: date, **values) -> dict:
    out, d = {}, start
    while d <= end:
        out[d] = ov.Usage(**values)
        d += timedelta(days=1)
    return out


def test_parse_settings_and_lookups():
    s = SETTINGS
    assert [c.name for c in s.contracts] == ["vast 2025", "vast 2026", "Tibber"]
    assert s.contract(date(2026, 1, 13)).name == "vast 2025"
    assert s.contract(date(2026, 1, 14)).name == "vast 2026"
    assert s.contract(date(2024, 12, 31)) is None
    assert s.advance(date(2026, 1, 1)) == 261 and s.advance(date(2026, 2, 1)) == 123
    assert s.ev_refund(date(2026, 8, 31)) == 0.40 and s.ev_refund(date(2026, 9, 1)) == 0.0
    with pytest.raises(ValueError):
        ov.parse_settings({"contracten": [{"stroom": 1}]})


def test_derived_usage_matches_the_spreadsheet():
    # 2025 in the spreadsheet: generated 7198, sold 5461, bought 6276 -> total 8014, own use 24%
    u = ov.Usage(import_kwh=6276, export_kwh=5461, pv_kwh=7198, hp_kwh=1817, ev_kwh=394)
    assert u.total_kwh == 8013
    assert u.general_kwh == 8013 - 1817 - 394
    row = ov.MonthRow(date(2025, 1, 1), "actual", u, ov.Costs(), 0).as_dict()
    assert row["own_use_pct"] == 24


def test_day_costs_net_metering_then_separate_prices():
    s = SETTINGS
    # under net metering export earns the purchase price, also below zero
    c = ov.day_costs(date(2026, 6, 1), ov.Usage(import_kwh=5, export_kwh=15, gas_m3=1), s)
    assert c.power == pytest.approx(-10 * 0.2285)
    assert c.gas == pytest.approx(1.21) and c.fixed == pytest.approx(2.58)
    # 1-15 Jan 2027: old fixed contract without net metering, export earns its export price (0 here)
    c = ov.day_costs(date(2027, 1, 5), ov.Usage(import_kwh=10, export_kwh=2), s)
    assert c.power == pytest.approx(10 * 0.2285)
    # dynamic contract: actual cost statistics win over the expected prices
    c = ov.day_costs(date(2027, 2, 1), ov.Usage(import_kwh=10, export_kwh=4, import_cost=2.0,
                                                export_revenue=0.3), s)
    assert c.power == pytest.approx(1.7)
    c = ov.day_costs(date(2027, 2, 1), ov.Usage(import_kwh=10, export_kwh=4), s)
    assert c.power == pytest.approx(10 * 0.25 - 4 * 0.05)
    # wallbox refund
    assert ov.day_costs(date(2025, 12, 5), ov.Usage(ev_kwh=10), s).ev_refund == pytest.approx(-4.0)


def _history():
    """Two years with a heat pump that uses more when it is colder, and summer solar."""
    days = {}
    d = date(2025, 1, 1)
    while d < date(2026, 11, 1):
        temp = ov.NORMAL_TEMP[d.month - 1]
        hdd = max(0.0, 18 - temp)
        pv = 30 * ov.NORMAL_PV[d.month - 1]
        days[d] = ov.Usage(import_kwh=12 + 0.8 * hdd - 0.4 * pv, export_kwh=0.6 * pv, pv_kwh=pv,
                           gas_m3=0.5 + 0.05 * hdd, hp_kwh=0.8 * hdd, ev_kwh=0.0,
                           temp_sum=temp, temp_days=1)
        d += timedelta(days=1)
    return days


def test_forecaster_learns_heat_pump_and_gas_from_degree_days():
    months = ov.by_month(_history())
    f = ov.Forecaster.learn(months)
    assert f.hp_fit[1] == pytest.approx(0.8, rel=0.01)
    assert f.gas_fit[1] == pytest.approx(0.05, rel=0.01)
    dec = f.predict(date(2026, 12, 1))
    assert dec.hp_kwh == pytest.approx(0.8 * (18 - 4.2) * 31, rel=0.02)
    assert dec.gas_m3 == pytest.approx((0.5 + 0.05 * (18 - 4.2)) * 31, rel=0.02)
    # the yearly solar yield is learned from the months with data
    year = sum(30 * ov.NORMAL_PV[m - 1] * ov.month_days(date(2025, m, 1)) for m in range(1, 13))
    assert f.pv_year == pytest.approx(year, rel=0.03)
    # self-use share of solar from the same month earlier: 40%
    assert dec.export_kwh == pytest.approx(0.6 * dec.pv_kwh, rel=0.02)


def test_build_overview_actual_running_and_forecast_months():
    days = _history()
    today = date(2026, 10, 30)
    days = {d: u for d, u in days.items() if d < today}
    years = ov.build_overview(days, today, SETTINGS)
    assert [y.year for y in years] == [2025, 2026]
    y25, y26 = years
    assert y25.complete and len(y25.rows) == 12
    statuses = [r.status for r in y26.rows]
    assert statuses[:9] == ["actual"] * 9 and statuses[9] == "running" and statuses[10:] == ["forecast"] * 2
    # running month: 29 actual days plus 2 forecast days
    oct_row = y26.rows[9]
    assert oct_row.usage.temp_days == 31
    d = y25.as_dict()
    assert d["cashback"] == 400 and d["settlement"] == 1854
    assert d["costs"] == pytest.approx(sum(r.costs.total for r in y25.rows) - 400, abs=0.01)
    assert d["months"][-1]["cum_advance"] == 12 * 261
    assert d["expected_settlement"] == pytest.approx(d["costs"] - d["advances"], abs=0.01)


def test_csv_uses_semicolon_and_decimal_comma():
    years = ov.build_overview(_history(), date(2026, 10, 30), SETTINGS)
    text = ov.to_csv(years[-1])
    head = text.splitlines()[0].split(";")
    assert head[0] == "Maand" and "Warmtepomp kWh" in head
    first = text.splitlines()[1].split(";")
    assert first[0] == "2026-01" and "," in first[head.index("Totaal")]
    assert "Totale kosten" in text
    assert ov.years_csv(years).splitlines()[1].startswith("2025;ja;")
