"""Base entity for Home EMS."""
from __future__ import annotations

from homeassistant.const import EntityCategory
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, NAME
from .coordinator import HomeEmsCoordinator

# v0.12.3: the entities are spread over sub-devices under the Home EMS device, so the
# device page shows them in groups. Everything not listed stays on the main device
# (living room and control). The entity ids do not change.
DEVICE_GROUPS: dict[str, frozenset[str]] = {
    "planner": frozenset({
        "plan_next_run", "plan_heat_need", "plan_expected_cost", "planner_status",
        "plan_run_now", "price_peak", "planner_model", "model_run_now",
        "model_predicted_living", "model_prediction_error", "model_night_run_hours",
        "model_learned_hours", "model_readiness",
    }),
    "solar": frozenset({
        "pv_surplus", "pv_curtail", "appliance_start", "negative_price_export_today",
    }),
    "battery": frozenset({"battery_reserve_status", "battery_reserve"}),
    "overview": frozenset({
        "overview_status", "year_cost_expected", "settlement_expected",
        "month_cost_expected", "own_use_year", "year_use_expected",
    }),
}

# day counters and technical values: in the Diagnostic section of their device
DIAGNOSTIC_KEYS = frozenset({
    "runs_today", "short_runs_today", "unadvised_starts_today", "setpoint_writes_today",
    "advised_run_minutes_today", "running_minutes_today", "pv_running_minutes_today",
    "run_advised", "model_learned_hours",
})


def device_group(key: str) -> str | None:
    """The sub-device of an entity key, None for the main device."""
    if key.startswith("appliance_wait_"):
        return "solar"
    return next((group for group, keys in DEVICE_GROUPS.items() if key in keys), None)


class HomeEmsEntity(CoordinatorEntity[HomeEmsCoordinator]):
    """Shared device and naming for all Home EMS entities."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: HomeEmsCoordinator, key: str) -> None:
        super().__init__(coordinator)
        entry_id = coordinator.config_entry.entry_id
        self._attr_unique_id = f"{entry_id}_{key}"
        self._attr_translation_key = key
        if key in DIAGNOSTIC_KEYS:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
        group = device_group(key)
        if group is None:
            self._attr_device_info = DeviceInfo(
                identifiers={(DOMAIN, entry_id)},
                name=NAME,
                entry_type=DeviceEntryType.SERVICE,
            )
        else:
            self._attr_device_info = DeviceInfo(
                identifiers={(DOMAIN, f"{entry_id}_{group}")},
                translation_key=group,
                entry_type=DeviceEntryType.SERVICE,
                via_device=(DOMAIN, entry_id),
            )
