"""Data coordinator for Home EMS: reads sensors and evaluates the model."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    CONF_FLOW_TEMPERATURE,
    CONF_GRID_POWER,
    CONF_HEAT_PUMP_POWER,
    CONF_LIVING_CLIMATE,
    DOMAIN,
    UPDATE_INTERVAL,
)
from .model import Evaluation, Inputs, RunTracker, evaluate

_LOGGER = logging.getLogger(__name__)

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
        evaluation = evaluate(self.read_inputs(), self.tracker, dt_util.now())
        self._store.async_delay_save(self.tracker.to_dict, SAVE_DELAY)
        return evaluation
