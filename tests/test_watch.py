"""Tests for watching tado (v0.10.10)."""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "home_ems_watch", Path(__file__).parents[1] / "custom_components" / "home_ems" / "watch.py")
watch = importlib.util.module_from_spec(_SPEC)
sys.modules["home_ems_watch"] = watch
_SPEC.loader.exec_module(watch)

T0 = datetime(2026, 11, 11, 0, 0, tzinfo=timezone.utc)


def minutes(n: int) -> datetime:
    return T0 + timedelta(minutes=n)


def test_a_flapping_thermostat_gives_one_notice():
    w = watch.OutageWatch()
    events = []
    # away 4 minutes, back 1 minute, again and again (like the night of 10/11 Nov)
    for m in range(0, 120):
        reachable = m % 5 == 4
        events.append(w.update(reachable, minutes(m)))
    assert events.count(watch.RAISE) == 1
    assert watch.CLEAR not in events
    assert w.alert


def test_the_notice_ends_after_thirty_minutes_back():
    w = watch.OutageWatch()
    for m in range(0, 15):
        w.update(False, minutes(m))
    assert w.alert
    assert w.update(True, minutes(15)) is None
    assert w.update(True, minutes(44)) is None
    assert w.update(True, minutes(45)) == watch.CLEAR
    assert not w.alert and w.since is None


def test_a_short_dropout_gives_no_notice_and_no_notice_with_control_off():
    w = watch.OutageWatch()
    for m in range(0, 5):
        assert w.update(False, minutes(m)) is None
    off = watch.OutageWatch()
    assert all(off.update(False, minutes(m), armed=False) is None for m in range(0, 30))


def test_no_heat_while_asking_for_thirty_minutes():
    w = watch.DemandWatch()
    assert w.update(True, False, minutes(0)) is None
    assert w.update(True, False, minutes(29)) is None
    assert w.update(True, False, minutes(30)) == watch.RAISE
    assert w.update(True, False, minutes(31)) is None           # only once
    assert w.update(True, True, minutes(40)) == watch.CLEAR     # the heat pump runs


def test_no_notice_when_not_asking_or_running():
    w = watch.DemandWatch()
    for m in range(0, 60):
        assert w.update(False, False, minutes(m)) is None
    for m in range(0, 60):
        assert w.update(True, True, minutes(m)) is None
