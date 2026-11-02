"""Switches for Home EMS: the EMS itself, and control (writing the target setpoint)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import STATE_OFF, STATE_ON
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
    async_add_entities([EmsEnabledSwitch(entry.runtime_data, "ems_enabled"),
                        ControlSwitch(entry.runtime_data, "control")])


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
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        # with control on, the next update writes the comfort setpoint once
        self.coordinator.set_enabled(False)
        await self.coordinator.async_request_refresh()


class ControlSwitch(HomeEmsEntity, SwitchEntity, RestoreEntity):
    """On = Home EMS writes its target setpoint to the living room thermostat.
    Off by default; restored after a restart (without writing anything)."""

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            await self.coordinator.async_set_control(last.state == STATE_ON, restore=True)

    @property
    def is_on(self) -> bool:
        return self.coordinator.control

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_control(True)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_control(False)
