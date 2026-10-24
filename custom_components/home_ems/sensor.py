"""Sensors for Home EMS."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import UnitOfPower, UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import HomeEmsConfigEntry, HomeEmsCoordinator
from .entity import HomeEmsEntity
from .model import Advice


@dataclass(frozen=True, kw_only=True)
class HomeEmsSensorDescription(SensorEntityDescription):
    value_fn: Callable[[HomeEmsCoordinator], Any]


SENSORS: tuple[HomeEmsSensorDescription, ...] = (
    HomeEmsSensorDescription(
        key="advice",
        device_class=SensorDeviceClass.ENUM,
        options=[a.value for a in Advice],
        value_fn=lambda c: c.data.advice.value,
    ),
    HomeEmsSensorDescription(
        key="floor_margin",
        # temperature difference, so no temperature device class (HA would convert K to C)
        native_unit_of_measurement=UnitOfTemperature.KELVIN,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda c: c.data.floor_margin,
    ),
    HomeEmsSensorDescription(
        key="pv_surplus",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c: c.data.pv_surplus_w,
    ),
    HomeEmsSensorDescription(
        # 1 while the EMS advises starting a run, else 0; its 5-minute mean is the
        # share of time a run was advised, which the energy export can pick up.
        key="run_advised",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c: int(c.data.advice is Advice.RUN),
    ),
    HomeEmsSensorDescription(
        key="runs_today",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda c: c.tracker.stats.runs,
    ),
    HomeEmsSensorDescription(
        key="short_runs_today",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda c: c.tracker.stats.short_runs,
    ),
    HomeEmsSensorDescription(
        key="unadvised_starts_today",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda c: c.tracker.stats.unadvised_starts,
    ),
    *(
        HomeEmsSensorDescription(
            key=key,
            device_class=SensorDeviceClass.DURATION,
            native_unit_of_measurement=UnitOfTime.MINUTES,
            state_class=SensorStateClass.TOTAL_INCREASING,
            suggested_display_precision=0,
            value_fn=lambda c, attr=attr: round(getattr(c.tracker.stats, attr), 1),
        )
        for key, attr in (
            ("advised_run_minutes_today", "advised_run_minutes"),
            ("running_minutes_today", "running_minutes"),
            ("pv_running_minutes_today", "pv_running_minutes"),
        )
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HomeEmsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(HomeEmsSensor(coordinator, d) for d in SENSORS)


class HomeEmsSensor(HomeEmsEntity, SensorEntity):
    entity_description: HomeEmsSensorDescription

    def __init__(
        self, coordinator: HomeEmsCoordinator, description: HomeEmsSensorDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.key != "advice":
            return None
        return {"reason": self.coordinator.data.reason, "mode": "observation"}
