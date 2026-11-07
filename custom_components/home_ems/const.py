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
OPT_GAS_ENTITY = "gas_price_entity"    # v0.8: all-in gas price per m3 (Jeroen.nl gasprijs)

# options (v0.3): thresholds, defaults as in model.Settings / planner.PlannerSettings
OPT_COMFORT_SETPOINT = "comfort_setpoint"
OPT_FLOOR_RULE_BAND = "floor_rule_band"
OPT_FLOOR_MIN_MARGIN = "floor_min_margin"
OPT_PV_RUN_THRESHOLD = "pv_run_threshold_w"
OPT_MIN_RUN = "min_run_minutes"
OPT_EXPENSIVE_SHARE = "expensive_share_pct"  # % most expensive quarters of the day = price peak
OPT_MAX_GAP = "max_gap_hours"
OPT_BASE_LOAD = "base_load_kw"
# options (v0.5): comfort band for the arbiter, heat capacity of the house for the planner
OPT_COMFORT_MIN = "comfort_min"
OPT_COMFORT_MAX = "comfort_max"
OPT_CAPACITY = "capacity_kwh_k"
# options (v0.9): steady runs in the cold
OPT_MIN_PAUSE = "min_pause_hours"       # shorter pauses between planned runs are filled
OPT_RUN_HEADROOM = "run_headroom_k"     # run ceiling = comfort setpoint + this
# options (v0.10): larger gap with solar surplus or a low price
OPT_GAP_MAX = "gap_max_k"                # largest gap setpoint - room
OPT_FULL_SURPLUS = "full_surplus_w"      # surplus at which the gap is largest
OPT_LOW_PRICE = "low_price_eur"          # all-in price at or below which the gap is largest

OPTION_DEFAULTS = {
    OPT_COMFORT_SETPOINT: 19.0,
    OPT_FLOOR_RULE_BAND: 0.5,
    OPT_FLOOR_MIN_MARGIN: 0.0,
    OPT_PV_RUN_THRESHOLD: 700,
    OPT_MIN_RUN: 30,
    OPT_EXPENSIVE_SHARE: 25,
    OPT_MAX_GAP: 8,
    OPT_BASE_LOAD: 0.4,
    OPT_COMFORT_MIN: 18.0,
    OPT_COMFORT_MAX: 21.5,
    OPT_CAPACITY: 15.0,
    OPT_MIN_PAUSE: 2.0,
    OPT_RUN_HEADROOM: 1.0,
    OPT_GAP_MAX: 0.6,
    OPT_FULL_SURPLUS: 1500,
    OPT_LOW_PRICE: 0.20,
}

# options (v0.4): energy and cost overview, sources are long-term statistics
OPT_STAT_IMPORT = "stat_import"            # grid import kWh (one or more, e.g. tariff 1 and 2)
OPT_STAT_EXPORT = "stat_export"            # grid export kWh
OPT_STAT_PV = "stat_pv"                    # solar production kWh (one per inverter)
OPT_STAT_GAS = "stat_gas"                  # gas m3
OPT_STAT_HEAT_PUMP = "stat_heat_pump"      # heat pump electricity kWh
OPT_STAT_EV = "stat_ev"                    # wallbox kWh
OPT_STAT_OUTSIDE = "stat_outside"          # outside temperature (mean per day)
OPT_STAT_IMPORT_COST = "stat_import_cost"  # actual purchase cost EUR (dynamic contract, e.g. Tibber)
OPT_STAT_EXPORT_REVENUE = "stat_export_revenue"  # actual export compensation EUR
OPT_COST_SETTINGS = "cost_settings"        # YAML: contracts, advances, wallbox refund, cashback
OPT_OVERVIEW_FROM = "overview_from"        # first day of the overview
OPT_EXPORT_DIR = "export_dir"              # folder for the CSV files

OVERVIEW_OPTIONS = (
    OPT_STAT_IMPORT, OPT_STAT_EXPORT, OPT_STAT_PV, OPT_STAT_GAS, OPT_STAT_HEAT_PUMP, OPT_STAT_EV,
    OPT_STAT_OUTSIDE, OPT_STAT_IMPORT_COST, OPT_STAT_EXPORT_REVENUE, OPT_COST_SETTINGS,
    OPT_OVERVIEW_FROM, OPT_EXPORT_DIR,
)

COST_SETTINGS_EXAMPLE = """\
# Prijzen all-in per kWh / m3 (incl. energiebelasting en btw).
# vast_per_dag: vaste leveringskosten + netbeheer - vermindering energiebelasting, per dag.
contracten:
  - naam: Vast contract
    vanaf: 2025-01-01
    stroom: 0.28
    gas: 1.25
    vast_per_dag: 3.0
saldering_tot: 2026-12-31
voorschotten:
  - vanaf: 2025-01-01
    bedrag: 150
laadpaal_vergoeding: []
cashback: {}
afrekening: {}
"""

# options (v0.6): control, the executor that writes the target setpoint (own screen,
# so they are kept apart from OPTION_DEFAULTS, which belong to the planner screen)
OPT_OVERRIDE_HOURS = "override_hours"            # pause after a setpoint changed by hand
OPT_WRITE_INTERVAL = "write_interval_minutes"    # minimum time between two changes
CONTROL_DEFAULTS = {OPT_OVERRIDE_HOURS: 2.0, OPT_WRITE_INTERVAL: 5}
CONTROL_OPTIONS = tuple(CONTROL_DEFAULTS)

# options (v0.7): bedrooms (own screen); rooms as YAML, see bedrooms.parse_rooms
OPT_BEDROOMS = "bedrooms"
OPT_BEDROOM_MIN = "bedroom_min_temp"
OPT_BEDROOM_LEAD = "bedroom_lead_hours"
BEDROOM_DEFAULTS = {OPT_BEDROOM_MIN: 13.0, OPT_BEDROOM_LEAD: 6.0}
BEDROOM_OPTIONS = (OPT_BEDROOMS, *BEDROOM_DEFAULTS)

# options (v0.8): PV curtailment and appliances (own screen)
OPT_PV_POWER = "pv_power_entities"     # live power per inverter (W), one sensor each
OPT_APPLIANCES = "appliances"          # YAML, see appliances.parse_appliances
PV_APPLIANCE_OPTIONS = (OPT_PV_POWER, OPT_APPLIANCES)
APPLIANCES_EXAMPLE = """\
# Witgoed achter een slimme stekker; Home EMS adviseert de goedkoopste starttijd.
# - naam: Vaatwasser
#   kwh: 1.1
#   duur: 2.5
#   klaar_voor: "07:00"
"""
BEDROOMS_EXAMPLE = """\
# Radiatorthermostaten in de slaapkamers (alleen een klep; warm water stroomt als de woonkamer vraagt).
# - entiteit: climate.slaapkamer_1
#   naam: Slaapkamer 1
#   doel: 17
#   bedtijd: "20:00"
"""

OVERVIEW_REFRESH = timedelta(hours=1)

# overview status (sensor "Overview status")
OVERVIEW_OK = "ok"
OVERVIEW_NOT_CONFIGURED = "not_configured"
OVERVIEW_NO_DATA = "no_statistics"
OVERVIEW_BAD_SETTINGS = "invalid_cost_settings"
OVERVIEW_STATUSES = [OVERVIEW_OK, OVERVIEW_NOT_CONFIGURED, OVERVIEW_NO_DATA, OVERVIEW_BAD_SETTINGS]

WEATHER_REFRESH = timedelta(minutes=30)
REPLAN_INTERVAL = timedelta(minutes=5)

# planner status (sensor "Planner status")
PLAN_OK = "ok"
PLAN_NO_TEMPERATURES = "no_temperature_forecast"
PLAN_NO_PRICES = "no_price_data"
PLAN_STATUSES = [PLAN_OK, PLAN_NO_TEMPERATURES, PLAN_NO_PRICES]
