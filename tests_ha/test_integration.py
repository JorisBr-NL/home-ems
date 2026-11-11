"""Home EMS inside a real Home Assistant test instance: entities, switches, writes, options."""
from __future__ import annotations

from datetime import timedelta

from homeassistant.core import HomeAssistant, State
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
    mock_restore_cache,
)

from custom_components.home_ems.const import DOMAIN

from .conftest import DATA, LIVING, NO_NIGHT, set_house

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
    hass.config_entries.async_update_entry(entry, options={**NO_NIGHT, "comfort_setpoint": 19.5})
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["planner", "control", "bedrooms", "pv_appliances", "overview"]
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "control"})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "control"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"override_hours": 3, "write_interval_minutes": 10})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["override_hours"] == 3
    assert entry.options["comfort_setpoint"] == 19.5   # planner screen value kept


async def test_bedroom_below_target_before_bedtime_asks_for_a_run(hass, freezer):
    freezer.move_to("2026-11-10T20:00:00+00:00")
    bedtime = (dt_util.now() + timedelta(minutes=30)).strftime("%H:%M")
    set_house(hass, room=19.4, flow=21.0)         # floor fine, room above its minimum: no run
    hass.states.async_set("climate.room_a", "heat", {"current_temperature": 15.0, "temperature": 13.0})
    entry = MockConfigEntry(domain=DOMAIN, title="Home EMS", data=DATA, unique_id=DOMAIN, options={**NO_NIGHT,
        "bedrooms": f'- entiteit: climate.room_a\n  naam: Room A\n  doel: 17\n  bedtijd: "{bedtime}"\n'})
    await _setup(hass, entry)

    rooms = hass.states.get("sensor.home_ems_bedrooms_open")
    assert rooms.state == "1" and rooms.attributes["needs_run"] is True
    assert rooms.attributes["rooms"][0]["setpoint"] == 17.0
    target = hass.states.get(TARGET)
    assert target.attributes["source"] == "bedroom" and float(target.state) == 19.6


async def test_negative_price_shows_which_inverter_would_go_off(hass, freezer):
    freezer.move_to("2027-05-01T11:05:00+00:00")
    set_house(hass, flow=21.0, grid_w=-2500.0)     # exporting 2.5 kW
    hass.states.async_set("sensor.inverter_a", "2000")
    hass.states.async_set("sensor.inverter_b", "3000")
    hass.states.async_set("sensor.price", "0.10", {
        "today": [{"start": "2027-05-01T11:00:00+00:00", "price": 0.10, "market": -0.02}],
        "prices": []})
    entry = MockConfigEntry(domain=DOMAIN, title="Home EMS", data=DATA, unique_id=DOMAIN, options={**NO_NIGHT,
        "price_entity": "sensor.price",
        "pv_power_entities": ["sensor.inverter_a", "sensor.inverter_b"]})
    await _setup(hass, entry)

    curtail = hass.states.get("sensor.home_ems_pv_curtailment_inverters_off")
    assert curtail.state == "1"
    assert curtail.attributes["off"] == ["sensor.inverter_a"]


async def test_a_write_that_did_not_arrive_is_retried(recorder_mock, hass, entry):
    set_house(hass)
    calls = async_mock_service(hass, "climate", "set_temperature")
    await _setup(hass, entry)
    await hass.services.async_call("switch", "turn_on", {"entity_id": CONTROL}, blocking=True)
    await hass.async_block_till_done()
    assert [c.data["temperature"] for c in calls] == [19.2]

    # the thermostat still shows 19.0 after the settle time: retry, not a change by hand
    entry.runtime_data.executor.last_write -= timedelta(minutes=5)
    await _refresh(hass, entry)
    assert [c.data["temperature"] for c in calls] == [19.2, 19.2]
    assert hass.states.get(STATUS).state == "writing"


async def test_reconfigure_changes_the_thermostat_and_keeps_the_options(recorder_mock, hass, entry):
    set_house(hass)
    await _setup(hass, entry)
    hass.config_entries.async_update_entry(entry, options={**NO_NIGHT, "comfort_setpoint": 19.5})
    await hass.async_block_till_done()

    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "reconfigure"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**DATA, "living_climate": "climate.other_thermostat"})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reconfigure_successful"
    assert entry.data["living_climate"] == "climate.other_thermostat"
    assert entry.options["comfort_setpoint"] == 19.5


async def test_solar_boost_survives_a_reload(recorder_mock, hass, entry):
    set_house(hass, hp_w=900.0, grid_w=-1500.0)   # running on solar, still exporting
    await _setup(hass, entry)
    coordinator = entry.runtime_data
    coordinator.pv_boost.extra = 0.3
    coordinator._prev_raised = 0.3
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data is not coordinator
    assert entry.runtime_data.pv_boost.extra == 0.3
    assert entry.runtime_data._prev_raised == 0.3


async def test_planner_options_in_sections_are_saved_flat(recorder_mock, hass, entry):
    set_house(hass)
    await _setup(hass, entry)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "planner"})
    assert result["type"] is FlowResultType.FORM
    # every section is required; the fields in it have defaults
    user_input = {str(key): {} for key in result["data_schema"].schema}
    user_input["temperatures"]["comfort_setpoint"] = 19.2
    user_input["solar_price"]["dynamic_from"] = "2027-01-16"
    result = await hass.config_entries.options.async_configure(result["flow_id"], user_input)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["comfort_setpoint"] == 19.2
    assert entry.options["dynamic_from"] == "2027-01-16"
    assert "temperatures" not in entry.options


async def test_fixed_tariff_switches_the_price_rules_off(recorder_mock, hass):
    set_house(hass)
    entry = MockConfigEntry(domain=DOMAIN, title="Home EMS", data=DATA, unique_id=DOMAIN,
                            options={**NO_NIGHT, "dynamic_from": "2099-01-01"})
    await _setup(hass, entry)
    coordinator = entry.runtime_data
    assert coordinator.fixed_tariff() is True
    assert coordinator.settings().expensive_share == 0
    assert coordinator.arbiter_settings().low_price == 0


async def test_unreachable_thermostat_gives_a_notification_and_no_writes(recorder_mock, hass, entry, freezer):
    set_house(hass)
    calls = async_mock_service(hass, "climate", "set_temperature")
    await _setup(hass, entry)
    await hass.services.async_call("switch", "turn_on", {"entity_id": CONTROL}, blocking=True)
    await hass.async_block_till_done()
    written = len(calls)
    hass.states.async_set(LIVING, "unavailable")
    await _refresh(hass, entry)
    for _ in range(3):                                        # Home EMS updates every minute;
        freezer.tick(timedelta(minutes=5))                    # longer gaps count as 5 min
        await _refresh(hass, entry)
    assert len(calls) == written                              # nothing written into it
    assert hass.states.get(STATUS).state == "error"
    assert entry.runtime_data.outage_watch.alert is True
    set_house(hass, setpoint=21.0)                            # back, with another setpoint
    await _refresh(hass, entry)
    assert entry.runtime_data.outage_watch.alert is True      # not over after a moment
    freezer.tick(timedelta(minutes=31))
    await _refresh(hass, entry)
    assert entry.runtime_data.outage_watch.alert is False


async def test_no_heat_while_the_thermostat_asks_gives_a_notification(recorder_mock, hass, entry, freezer):
    set_house(hass, room=18.3, setpoint=18.8, hp_w=5.0, flow=25.0)
    async_mock_service(hass, "climate", "set_temperature")
    await _setup(hass, entry)
    await hass.services.async_call("switch", "turn_on", {"entity_id": CONTROL}, blocking=True)
    await hass.async_block_till_done()
    for _ in range(7):
        freezer.tick(timedelta(minutes=5))
        set_house(hass, room=18.3, setpoint=18.8, hp_w=5.0, flow=25.0)
        await _refresh(hass, entry)
    assert entry.runtime_data.demand_watch.alert is True
    set_house(hass, room=18.3, setpoint=18.8, hp_w=900.0, flow=25.0)   # the heat pump starts
    await _refresh(hass, entry)
    assert entry.runtime_data.demand_watch.alert is False


async def test_pause_after_a_change_by_hand_survives_a_reload(recorder_mock, hass, entry):
    set_house(hass)
    async_mock_service(hass, "climate", "set_temperature")
    await _setup(hass, entry)
    # the pause only exists while control is on (switching control off clears it)
    await hass.services.async_call("switch", "turn_on", {"entity_id": CONTROL}, blocking=True)
    await hass.async_block_till_done()
    until = dt_util.now() + timedelta(hours=1)
    entry.runtime_data.executor.override_until = until
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.executor.override_until == until


async def test_planner_model_switch_is_off_by_default_and_switches_without_reload(recorder_mock, hass, entry):
    set_house(hass)
    await _setup(hass, entry)
    switch = "switch.home_ems_planner_with_house_model"
    assert hass.states.get(switch).state == "off"
    coordinator = entry.runtime_data
    await hass.services.async_call("switch", "turn_on", {"entity_id": switch}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get(switch).state == "on"
    assert coordinator.planner_model is True
    assert entry.runtime_data is coordinator                  # no reload
