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
from homeassistant.const import UnitOfPower, UnitOfTemperature
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
        key="runs_today",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda c: c.tracker.runs_today,
    ),
    HomeEmsSensorDescription(
        key="short_runs_today",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda c: c.tracker.short_runs_today,
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
