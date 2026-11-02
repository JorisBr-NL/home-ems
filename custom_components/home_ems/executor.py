"""Executor: writes the arbiter's target setpoint to the living room thermostat.

Pure logic (no Home Assistant imports): `step` decides what to write, the coordinator
does the actual service call and then calls `ExecutorState.record_write`.

Safety rules:
- Control is off by default (own switch); off means nothing is written.
- Only on a change, and at most once per `min_interval` while the reason stays the same.
- A setpoint changed by hand (on the thermostat or in the app) pauses control for
  `override_hold`.
- When the EMS is switched off, or its inputs stay unavailable for `fallback_after`,
  the fallback setpoint (the normal comfort setpoint) is written once.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

STATUSES = ["off", "steady", "writing", "waiting", "override", "fallback", "error"]


@dataclass(frozen=True)
class ExecutorSettings:
    min_interval: timedelta = timedelta(minutes=5)   # between writes for the same reason
    settle: timedelta = timedelta(minutes=2)         # time for the thermostat to report a write
    override_hold: timedelta = timedelta(hours=2)
    fallback_after: timedelta = timedelta(minutes=10)
    fallback_setpoint: float = 19.0


@dataclass(frozen=True)
class Action:
    write: float | None   # setpoint to write now, None = nothing
    status: str           # one of STATUSES
    reason: str


@dataclass
class ExecutorState:
    last_written: float | None = None   # last setpoint written while steering (None = not steering)
    last_write: datetime | None = None
    last_source: str | None = None
    override_until: datetime | None = None
    unknown_since: datetime | None = None

    def record_write(self, setpoint: float, source: str, now: datetime,
                     steering: bool = True) -> None:
        """After a successful write. A fallback write ends steering (steering=False), so
        later changes by hand are not taken for an override."""
        self.last_written = setpoint if steering else None
        self.last_write = now
        self.last_source = source

    def to_dict(self) -> dict:
        def iso(value: datetime | None) -> str | None:
            return value.isoformat() if value else None

        return {"last_written": self.last_written, "last_write": iso(self.last_write),
                "last_source": self.last_source, "override_until": iso(self.override_until)}


def _same(a: float | None, b: float | None) -> bool:
    return a is not None and b is not None and abs(a - b) < 0.05


def step(state: ExecutorState, target: float | None, source: str, reason: str,
         current: float | None, now: datetime, control_on: bool,
         s: ExecutorSettings | None = None) -> Action:
    """Decide what to write now. Changes only the override and unknown timers in `state`."""
    s = s or ExecutorSettings()
    if not control_on:
        return Action(None, "off", "control switched off")

    if state.override_until is not None:
        if now < state.override_until:
            return Action(None, "override", "setpoint changed by hand: control paused")
        state.override_until = None

    # a setpoint that is not ours (after the thermostat had time to report our write)
    if (
        state.last_written is not None
        and state.last_write is not None
        and now - state.last_write >= s.settle
        and current is not None
        and not _same(current, state.last_written)
    ):
        state.override_until = now + s.override_hold
        state.last_written = None
        return Action(None, "override", "setpoint changed by hand: control paused")

    if target is None:
        steering = state.last_written is not None
        if source == "off":
            state.unknown_since = None
            if steering and not _same(current, s.fallback_setpoint):
                return Action(s.fallback_setpoint, "fallback", "EMS switched off: back to the comfort setpoint")
            return Action(None, "off", "EMS switched off")
        if state.unknown_since is None:
            state.unknown_since = now
        if steering and now - state.unknown_since >= s.fallback_after:
            return Action(s.fallback_setpoint, "fallback", "no data: back to the comfort setpoint")
        return Action(None, "waiting", "no data from the sensors")

    state.unknown_since = None
    if _same(target, current):
        return Action(None, "steady", f"{source}: setpoint already right")
    if (
        state.last_write is not None
        and source == state.last_source
        and now - state.last_write < s.min_interval
    ):
        return Action(None, "waiting", f"{source}: next change after the minimum interval")
    return Action(target, "writing", f"{source}: {reason}")
