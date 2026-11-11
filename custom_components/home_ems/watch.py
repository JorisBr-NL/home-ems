"""Watching tado (v0.10.10, pure logic, no Home Assistant imports).

Two things Home EMS cannot fix but should notice:

- the living room thermostat drops out of Home Assistant. On 10/11 Nov 2026 it came and
  went every few minutes for eight hours, so a notice that disappears as soon as the
  thermostat is back for a moment came and went with it. An outage now ends only after the
  thermostat has been reachable for a while, and it is reported once it adds up to some
  minutes of being away;
- the thermostat asks for heat (setpoint above the room) but the heat pump does not run.
  On 11 Nov at 02:50 the setpoint 18.8 arrived with the room at 18.3, and the heat pump
  only started an hour later: tado did not pass the demand on.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

RAISE, CLEAR = "raise", "clear"
MAX_STEP = timedelta(minutes=5)   # a longer gap between updates (a restart) counts as this


@dataclass
class OutageWatch:
    """Outages of the thermostat: report once it was away `alert_after` in total, end it
    after it has been back `clear_after` without a break."""

    alert_after: timedelta = timedelta(minutes=10)
    clear_after: timedelta = timedelta(minutes=30)
    since: datetime | None = None          # start of the outage
    away: timedelta = timedelta(0)         # time away during the outage
    back_since: datetime | None = None
    last: datetime | None = None
    alert: bool = False

    def update(self, reachable: bool, now: datetime, armed: bool = True) -> str | None:
        """`armed`: a notice may be raised (control is on). Returns RAISE, CLEAR or None."""
        step = min(now - self.last, MAX_STEP) if self.last is not None else timedelta(0)
        self.last = now
        if not reachable:
            if self.since is None:
                self.since, self.away = now, timedelta(0)
            self.away += step
            self.back_since = None
            if armed and not self.alert and self.away >= self.alert_after:
                self.alert = True
                return RAISE
            return None
        if self.since is None:
            return None
        if self.back_since is None:
            self.back_since = now
        if now - self.back_since >= self.clear_after:
            self.since, self.away, self.back_since = None, timedelta(0), None
            if self.alert:
                self.alert = False
                return CLEAR
        return None


@dataclass
class DemandWatch:
    """The thermostat asks for heat but the heat pump does not run for `after`."""

    after: timedelta = timedelta(minutes=30)
    since: datetime | None = None
    alert: bool = False

    def update(self, asking: bool, running: bool | None, now: datetime) -> str | None:
        """`asking`: control on, thermostat reachable and its setpoint above the room."""
        if asking and running is False:
            if self.since is None:
                self.since = now
            if not self.alert and now - self.since >= self.after:
                self.alert = True
                return RAISE
            return None
        self.since = None
        if self.alert and (running or not asking):
            self.alert = False
            return CLEAR
        return None
