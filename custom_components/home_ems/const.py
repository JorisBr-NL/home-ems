"""Constants for Home EMS."""
from __future__ import annotations

from datetime import timedelta

DOMAIN = "home_ems"
NAME = "Home EMS"

UPDATE_INTERVAL = timedelta(minutes=1)

CONF_LIVING_CLIMATE = "living_climate"
CONF_KITCHEN_CLIMATE = "kitchen_climate"
CONF_HEAT_PUMP_POWER = "heat_pump_power"
CONF_FLOW_TEMPERATURE = "flow_temperature"
CONF_FLOW_RATE = "flow_rate"
CONF_GRID_POWER = "grid_power"
CONF_OUTSIDE_TEMPERATURE = "outside_temperature"
CONF_CHILL_CLIMATE = "chill_climate"

# options (v0.3): data sources for the planner
OPT_PRICE_ENTITY = "price_entity"      # Jeroen.nl stroomprijs, attribute "prices"
OPT_SOLAR_ENTITY = "solar_entity"      # Jeroen.nl opwekvoorspelling vandaag, attribute "hours"
OPT_WEATHER_ENTITY = "weather_entity"  # hourly forecast via weather.get_forecasts

# options (v0.3): thresholds, defaults as in model.Settings / planner.PlannerSettings
OPT_COMFORT_SETPOINT = "comfort_setpoint"
OPT_FLOOR_RULE_BAND = "floor_rule_band"
OPT_FLOOR_MIN_MARGIN = "floor_min_margin"
OPT_PV_RUN_THRESHOLD = "pv_run_threshold_w"
OPT_MIN_RUN = "min_run_minutes"
OPT_PEAK_START = "peak_start"
OPT_PEAK_END = "peak_end"
OPT_MAX_GAP = "max_gap_hours"
OPT_BASE_LOAD = "base_load_kw"

OPTION_DEFAULTS = {
    OPT_COMFORT_SETPOINT: 19.0,
    OPT_FLOOR_RULE_BAND: 0.5,
    OPT_FLOOR_MIN_MARGIN: 0.5,
    OPT_PV_RUN_THRESHOLD: 700,
    OPT_MIN_RUN: 30,
    OPT_PEAK_START: "17:30:00",
    OPT_PEAK_END: "20:00:00",
    OPT_MAX_GAP: 8,
    OPT_BASE_LOAD: 0.4,
}

WEATHER_REFRESH = timedelta(minutes=30)
REPLAN_INTERVAL = timedelta(minutes=5)

# planner status (sensor "Planner status")
PLAN_OK = "ok"
PLAN_NO_TEMPERATURES = "no_temperature_forecast"
PLAN_NO_PRICES = "no_price_data"
PLAN_STATUSES = [PLAN_OK, PLAN_NO_TEMPERATURES, PLAN_NO_PRICES]
