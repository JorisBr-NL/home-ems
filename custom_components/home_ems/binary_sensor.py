"""Binary sensors for Home EMS."""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .coordinator import HomeEmsConfigEntry
from .entity import HomeEmsEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HomeEmsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        [
            HeatPumpRunningSensor(coordinator, "heat_pump_running"),
            PeakWindowSensor(coordinator, "peak_window"),
            PlanRunNowSensor(coordinator, "plan_run_now"),
        ]
    )


class HeatPumpRunningSensor(HomeEmsEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.RUNNING

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.data.heat_pump_running


class PeakWindowSensor(HomeEmsEntity, BinarySensorEntity):
    @property
    def is_on(self) -> bool:
        return self.coordinator.data.in_peak


class PlanRunNowSensor(HomeEmsEntity, BinarySensorEntity):
    """On while the day planner has a run planned for this moment (observation only)."""

    @property
    def is_on(self) -> bool | None:
        plan = self.coordinator.plan
        return plan.running_now(dt_util.now()) if plan else None
