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
            PricePeakSensor(coordinator, "price_peak"),
            PlanRunNowSensor(coordinator, "plan_run_now"),
            ModelRunNowSensor(coordinator, "model_run_now"),
        ]
    )


class HeatPumpRunningSensor(HomeEmsEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.RUNNING

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.data.heat_pump_running


class PricePeakSensor(HomeEmsEntity, BinarySensorEntity):
    """On during a quarter among the most expensive of the day (morning or evening peak)."""

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.in_peak


class PlanRunNowSensor(HomeEmsEntity, BinarySensorEntity):
    """On while the day planner has a run planned for this moment (observation only)."""

    @property
    def is_on(self) -> bool | None:
        plan = self.coordinator.plan
        return plan.running_now(dt_util.now()) if plan else None


class ModelRunNowSensor(HomeEmsEntity, BinarySensorEntity):
    """On while the planner with the house model has a run planned for this moment, also
    while its switch is off (v0.12.2): to compare its plan with the control before it steers."""

    @property
    def is_on(self) -> bool | None:
        plan = self.coordinator.model_plan
        return plan.running_now(dt_util.now()) if plan else None

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        plan = self.coordinator.model_plan
        now = dt_util.now()
        run = next((r for r in plan.runs if r.start <= now < r.end), None) if plan else None
        return {"reason": run.reason if run else None}
