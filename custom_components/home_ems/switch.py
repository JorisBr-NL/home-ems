"""Switch for Home EMS: turn the EMS on or off (shadow mode: only the target changes)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import STATE_OFF
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .coordinator import HomeEmsConfigEntry
from .entity import HomeEmsEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HomeEmsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([EmsEnabledSwitch(entry.runtime_data, "ems_enabled")])


class EmsEnabledSwitch(HomeEmsEntity, SwitchEntity, RestoreEntity):
    """Off = the EMS has no target and the thermostat schedule leads. Survives a restart."""

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            self.coordinator.set_enabled(last.state != STATE_OFF)

    @property
    def is_on(self) -> bool:
        return self.coordinator.enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        self.coordinator.set_enabled(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.coordinator.set_enabled(False)
