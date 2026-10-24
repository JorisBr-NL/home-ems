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
