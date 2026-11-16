"""Appliances that wait for a good moment (v0.13, pure logic, no Home Assistant imports).

The machine is loaded and started, then its "wait for a good moment" switch is turned on:
the EMS turns the smart plug off, and the machine resumes its program when the plug comes
back on. The EMS turns the plug on, once, at the first of:

- solar: the 5-minute average of the power going out (export plus battery charging) covers
  the machine; a running heat pump keeps what it uses (heat first, the machine waits);
- planned: the best start of the advice has come (the cheapest window before the deadline,
  with a fixed tariff the window with the most solar), or there is no better moment;
- deadline: the latest start that still finishes before `klaar_voor`;
- timeout: it has waited `max_wachten` hours;
- manual: the switch is turned off (or the EMS is switched off).

A start is final: the EMS never turns the plug off again by itself (anti-flap), only a new
"wait" by hand does.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .appliances import Appliance, StartAdvice

SOLAR = "solar"
PLANNED = "planned"
DEADLINE = "deadline"
TIMEOUT = "timeout"
MANUAL = "manual"


@dataclass(frozen=True)
class Decision:
    start: bool
    reason: str | None
    detail: str


def decide(appliance: Appliance, since: datetime, now: datetime, advice: StartAdvice | None,
           available_w: float | None) -> Decision:
    """Start the waiting appliance now? `since`: when it began to wait; `advice`: the best
    start (None without price or solar data); `available_w`: 5-minute average of the power
    going out of the house (export plus battery charging), None without a grid meter."""
    duration = timedelta(hours=appliance.hours)
    if now - since >= timedelta(hours=appliance.max_wait):
        return Decision(True, TIMEOUT, f"waited {appliance.max_wait:g} h")
    if advice is not None and now >= advice.deadline - duration:
        return Decision(True, DEADLINE, "latest start to be ready in time")
    if available_w is not None and appliance.power_w > 0 and available_w >= appliance.power_w:
        return Decision(True, SOLAR, f"{available_w:.0f} W going out covers {appliance.power_w:.0f} W")
    if advice is None:
        return Decision(False, None, "waiting for solar (no plan data)")
    if advice.start is None:
        return Decision(False, None, "waiting for solar (no window before the deadline)")
    if now >= advice.start:
        return Decision(True, PLANNED, "best window has come")
    return Decision(False, None, f"waiting for {advice.start.isoformat(timespec='minutes')}")


def available_power(grid_w: float | None, battery_charge_w: float) -> float | None:
    """Power going out of the house now: export plus what the batteries charge (W)."""
    if grid_w is None:
        return None
    return max(0.0, battery_charge_w - grid_w)
