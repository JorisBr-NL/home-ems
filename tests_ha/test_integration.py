"""Home EMS inside a real Home Assistant test instance: entities, switches, writes, options."""
from __future__ import annotations

from datetime import timedelta

from homeassistant.core import HomeAssistant, State
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import (
    async_mock_service,
    mock_restore_cache,
)

from .conftest import LIVING, set_house

TARGET = "sensor.home_ems_target_setpoint_living_room"
CONTROL = "switch.home_ems_control_write_setpoint"
EMS = "switch.home_ems_ems_active"
STATUS = "sensor.home_ems_control_status"


async def _setup(hass: HomeAssistant, entry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _refresh(hass: HomeAssistant, entry) -> None:
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()


async def test_setup_creates_the_entities_and_writes_nothing(recorder_mock, hass, entry):
    set_house(hass)
    calls = async_mock_service(hass, "climate", "set_temperature")
    await _setup(hass, entry)

    assert hass.states.get("sensor.home_ems_advice").state == "run"   # floor below the room
    target = hass.states.get(TARGET)
    assert float(target.state) == 19.2 and target.attributes["source"] == "floor"
    assert target.attributes["would_write"] is True
    assert hass.states.get(CONTROL).state == "off"
    assert hass.states.get(EMS).state == "on"
    assert hass.states.get(STATUS).state == "off"
    assert calls == []


async def test_control_writes_the_target_and_falls_back_when_switched_off(recorder_mock, hass, entry):
    set_house(hass)
    calls = async_mock_service(hass, "climate", "set_temperature")
    await _setup(hass, entry)

    await hass.services.async_call("switch", "turn_on", {"entity_id": CONTROL}, blocking=True)
    await hass.async_block_till_done()
    assert [c.data["temperature"] for c in calls] == [19.2]
    assert calls[0].data["entity_id"] == LIVING
    assert hass.states.get("sensor.home_ems_setpoint_changes_today").state == "1"

    # the thermostat reports the new setpoint: nothing more to write
    set_house(hass, setpoint=19.2)
    await _refresh(hass, entry)
    assert len(calls) == 1 and hass.states.get(STATUS).state == "steady"

    # control off: back to the comfort setpoint once
    await hass.services.async_call("switch", "turn_off", {"entity_id": CONTROL}, blocking=True)
    await hass.async_block_till_done()
    assert [c.data["temperature"] for c in calls] == [19.2, 19.0]
    assert hass.states.get(STATUS).state == "off"


async def test_change_by_hand_pauses_control(recorder_mock, hass, entry):
    set_house(hass)
    calls = async_mock_service(hass, "climate", "set_temperature")
    await _setup(hass, entry)
    await hass.services.async_call("switch", "turn_on", {"entity_id": CONTROL}, blocking=True)
    await hass.async_block_till_done()
    assert len(calls) == 1

    coordinator = entry.runtime_data
    coordinator.executor.last_write -= timedelta(minutes=5)   # past the settle time
    set_house(hass, setpoint=21.0)                            # someone turned it up
    await _refresh(hass, entry)
    assert hass.states.get(STATUS).state == "override"
    assert len(calls) == 1


async def test_ems_off_gives_no_target(recorder_mock, hass, entry):
    set_house(hass)
    await _setup(hass, entry)
    await hass.services.async_call("switch", "turn_off", {"entity_id": EMS}, blocking=True)
    await hass.async_block_till_done()
    target = hass.states.get(TARGET)
    assert target.attributes["source"] == "off"
    assert target.state in ("unknown", "unavailable")


async def test_control_switch_is_restored_without_writing(recorder_mock, hass, entry):
    mock_restore_cache(hass, [State(CONTROL, "on")])
    set_house(hass)
    calls = async_mock_service(hass, "climate", "set_temperature")
    await _setup(hass, entry)
    assert hass.states.get(CONTROL).state == "on"
    assert entry.runtime_data.control is True
    # restoring writes nothing by itself; the next update does
    assert calls == []
    await _refresh(hass, entry)
    assert [c.data["temperature"] for c in calls] == [19.2]


async def test_options_menu_has_control_and_keeps_other_screens(recorder_mock, hass, entry):
    set_house(hass)
    await _setup(hass, entry)
    hass.config_entries.async_update_entry(entry, options={"comfort_setpoint": 19.5})
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["planner", "control", "overview"]
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "control"})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "control"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"override_hours": 3, "write_interval_minutes": 10})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["override_hours"] == 3
    assert entry.options["comfort_setpoint"] == 19.5   # planner screen value kept
