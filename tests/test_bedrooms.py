"""Tests for the bedrooms (radiator valves upstairs, shadow mode)."""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, time
from pathlib import Path

import pytest

_PATH = Path(__file__).parents[1] / "custom_components" / "home_ems" / "bedrooms.py"
_SPEC = importlib.util.spec_from_file_location("home_ems_bedrooms", _PATH)
bedrooms = importlib.util.module_from_spec(_SPEC)
sys.modules["home_ems_bedrooms"] = bedrooms
_SPEC.loader.exec_module(bedrooms)

ROOM = bedrooms.Bedroom("climate.room_a", "Room A", 17.0, time(20, 0))


def at(hour, minute=0):
    return datetime(2026, 11, 10, hour, minute)


def one(temp, now, flowing):
    return bedrooms.room_targets([ROOM], {"climate.room_a": temp}, now, flowing)[0]


def test_outside_the_evening_window_the_valve_stays_at_the_minimum():
    t = one(15.0, at(12), True)        # window is 14:00-20:00
    assert t.setpoint == 13.0 and not t.needs_run
    assert one(15.0, at(20, 30), True).setpoint == 13.0


def test_valve_opens_while_heat_flows_in_the_window():
    t = one(15.0, at(15), True)
    assert t.setpoint == 17.0 and not t.needs_run


def test_no_heat_flowing_keeps_the_valve_closed_until_the_catch_up():
    assert one(15.0, at(16), False).setpoint == 13.0
    t = one(15.0, at(18, 45), False)    # last 90 minutes, room 2 K short
    assert t.setpoint == 17.0 and t.needs_run
    assert not one(16.8, at(18, 45), False).needs_run   # close enough


def test_parse_rooms():
    rooms = bedrooms.parse_rooms([
        {"entiteit": "climate.room_a", "naam": "A", "doel": 16.5, "bedtijd": "19:45"},
        {"entiteit": "climate.room_b", "bedtijd": 1335},   # 22:15 read by YAML as minutes
    ])
    assert rooms[0].target == 16.5 and rooms[0].bedtime == time(19, 45)
    assert rooms[1].name == "climate.room_b" and rooms[1].bedtime == time(22, 15)
    assert bedrooms.parse_rooms(None) == []
    with pytest.raises(ValueError):
        bedrooms.parse_rooms([{"naam": "no entity"}])
    with pytest.raises(ValueError):
        bedrooms.parse_rooms({"entiteit": "climate.x"})
