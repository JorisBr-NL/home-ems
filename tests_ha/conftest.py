"""Fixtures for the Home Assistant integration tests (pytest-homeassistant-custom-component)."""
from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.home_ems.const import DOMAIN

LIVING = "climate.living_room"
DATA = {
    "living_climate": LIVING,
    "heat_pump_power": ["sensor.heat_pump_power"],
    "flow_temperature": "sensor.flow_temperature",
    "grid_power": "sensor.grid_power",
}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations):
    # recorder_mock first: Home EMS depends on the recorder, which must be set up
    # before the hass fixture
    yield


def set_house(hass, *, room=19.0, setpoint=19.0, flow=18.0, hp_w=5.0, grid_w=300.0):
    """States of the entities Home EMS reads. Floor 18 C under a 19 C room = floor rule."""
    hass.states.async_set(LIVING, "heat", {"current_temperature": room, "temperature": setpoint})
    hass.states.async_set("sensor.heat_pump_power", str(hp_w))
    hass.states.async_set("sensor.flow_temperature", str(flow))
    hass.states.async_set("sensor.grid_power", str(grid_w))


# no night window: the tests run at any time of day (the night limits have their own tests)
NO_NIGHT = {"night_start": "00:00:00", "night_end": "00:00:00"}


@pytest.fixture
def entry() -> MockConfigEntry:
    return MockConfigEntry(domain=DOMAIN, title="Home EMS", data=DATA, unique_id=DOMAIN,
                           options=NO_NIGHT)
