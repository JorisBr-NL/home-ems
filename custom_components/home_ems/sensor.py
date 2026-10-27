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
from homeassistant.const import EntityCategory, UnitOfEnergy, UnitOfPower, UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import PLAN_STATUSES
from .coordinator import HomeEmsConfigEntry, HomeEmsCoordinator
from .entity import HomeEmsEntity
from .model import Advice


@dataclass(frozen=True, kw_only=True)
class HomeEmsSensorDescription(SensorEntityDescription):
    value_fn: Callable[[HomeEmsCoordinator], Any]
    attrs_fn: Callable[[HomeEmsCoordinator], dict[str, Any]] | None = None


def _next_run(c: HomeEmsCoordinator):
    return c.plan.next_run(dt_util.now()) if c.plan else None


def _local(moment) -> str:
    """ISO time in the local time zone of Home Assistant (easier to read than UTC)."""
    return dt_util.as_local(moment).isoformat()


def _plan_attrs(c: HomeEmsCoordinator) -> dict[str, Any]:
    p = c.plan
    if p is None:
        return {"status": c.plan_status, "mode": "observation"}
    nxt = _next_run(c)
    return {
        "status": c.plan_status,
        "mode": "observation",
        "end": _local(nxt.end) if nxt else None,
        "reason": nxt.reason if nxt else None,
        "pv_share": round(nxt.pv_share, 2) if nxt else None,
        "horizon_end": _local(p.horizon_end),
        "run_hours": round(p.run_hours, 2),
        "heat_need_kwh": p.heat_need_kwh,
        "expected_cost": p.expected_cost,
        "baseline_cost": p.baseline_cost,
        "made": _local(p.made),
        "runs": [r.to_dict() | {"start": _local(r.start), "end": _local(r.end)} for r in p.runs],
    }


SENSORS: tuple[HomeEmsSensorDescription, ...] = (
    HomeEmsSensorDescription(
        key="advice",
        device_class=SensorDeviceClass.ENUM,
        options=[a.value for a in Advice],
        value_fn=lambda c: c.data.advice.value,
        attrs_fn=lambda c: {"reason": c.data.reason, "mode": "observation",
                            "plan_says_run": bool(c.plan and c.plan.running_now(dt_util.now()))},
    ),
    # --- day planner (v0.3)
    HomeEmsSensorDescription(
        key="planner_status",
        device_class=SensorDeviceClass.ENUM,
        options=PLAN_STATUSES,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: c.plan_status,
    ),
    HomeEmsSensorDescription(
        key="plan_next_run",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda c: r.start if (r := _next_run(c)) else None,
        attrs_fn=_plan_attrs,
    ),
    HomeEmsSensorDescription(
        key="plan_heat_need",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=1,
        value_fn=lambda c: c.plan.heat_need_kwh if c.plan else None,
    ),
    HomeEmsSensorDescription(
        key="plan_expected_cost",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="EUR",
        suggested_display_precision=2,
        value_fn=lambda c: c.plan.expected_cost if c.plan else None,
        attrs_fn=lambda c: {"baseline_cost": c.plan.baseline_cost,
                            "savings": round(c.plan.savings, 3)} if c.plan else {},
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
    _unrecorded_attributes = frozenset({"runs"})
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
        fn = self.entity_description.attrs_fn
        return fn(self.coordinator) if fn else None
