"""Executor: writes the arbiter's target setpoint to the living room thermostat.

Pure logic (no Home Assistant imports): `step` decides what to write, the coordinator
does the actual service call and then calls `ExecutorState.record_write`.

Safety rules:
- Control is off by default (own switch); off means nothing is written.
- Only on a change, and at most once per `min_interval` while the reason stays the same.
- Small changes (below `min_step`) are not written, unless they start or stop the heat
  demand (setpoint above or below the room). Fewer writes, fewer calls to the thermostat.
- A setpoint changed by hand (on the thermostat or in the app) pauses control for
  `override_hold`.
- A write that did not arrive (the thermostat still shows the setpoint from before the
  write) is not a change by hand: it is retried up to `max_retries` times, then the
  executor reports an error and pauses for `override_hold`.
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
    min_step: float = 0.1                            # smallest change worth writing
    max_retries: int = 2                             # retries of a write that did not arrive


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
    pause_status: str = "override"      # "override" or "error" while paused
    unknown_since: datetime | None = None
    before: float | None = None         # thermostat setpoint just before the last write
    retries: int = 0                    # retries of a write that did not arrive

    def record_write(self, setpoint: float, source: str, now: datetime,
                     steering: bool = True, before: float | None = None) -> None:
        """After a successful service call. A fallback write ends steering (steering=False),
        so later changes by hand are not taken for an override."""
        if not _same(setpoint, self.last_written):
            self.retries = 0
        self.last_written = setpoint if steering else None
        self.last_write = now
        self.last_source = source
        self.before = before

    def to_dict(self) -> dict:
        def iso(value: datetime | None) -> str | None:
            return value.isoformat() if value else None

        return {"last_written": self.last_written, "last_write": iso(self.last_write),
                "last_source": self.last_source, "override_until": iso(self.override_until),
                "pause_status": self.pause_status, "before": self.before, "retries": self.retries}

    @classmethod
    def from_dict(cls, data: dict | None) -> ExecutorState:
        def parse(value: str | None) -> datetime | None:
            return datetime.fromisoformat(value) if value else None
        data = data or {}
        return cls(last_written=data.get("last_written"), last_write=parse(data.get("last_write")),
                   last_source=data.get("last_source"),
                   override_until=parse(data.get("override_until")),
                   pause_status=data.get("pause_status") or "override",
                   before=data.get("before"), retries=int(data.get("retries") or 0))


def _same(a: float | None, b: float | None) -> bool:
    return a is not None and b is not None and abs(a - b) < 0.05


def action_is_retry(state: ExecutorState, current: float | None) -> bool:
    """True when the thermostat still shows the setpoint from before our last write, so a
    write now is a retry and the value from before stays the reference."""
    return (state.last_written is not None and state.before is not None
            and not _same(current, state.last_written) and _same(current, state.before))


def step(state: ExecutorState, target: float | None, source: str, reason: str,
         current: float | None, now: datetime, control_on: bool,
         s: ExecutorSettings | None = None, living: float | None = None) -> Action:
    """Decide what to write now. Changes only the override and unknown timers in `state`."""
    s = s or ExecutorSettings()
    if not control_on:
        return Action(None, "off", "control switched off")

    if state.override_until is not None:
        if now < state.override_until:
            if state.pause_status == "error":
                return Action(None, "error", "the thermostat did not take the setpoint: control paused")
            return Action(None, "override", "setpoint changed by hand: control paused")
        state.override_until = None
        state.pause_status = "override"

    # a setpoint that is not ours (after the thermostat had time to report our write)
    if (
        state.last_written is not None
        and state.last_write is not None
        and now - state.last_write >= s.settle
        and current is not None
        and not _same(current, state.last_written)
    ):
        if _same(current, state.before):
            # still the value from before our write: the write did not arrive
            if state.retries < s.max_retries:
                state.retries += 1
                return Action(state.last_written, "writing",
                              f"write did not arrive: retry {state.retries} of {s.max_retries}")
            state.override_until = now + s.override_hold
            state.pause_status = "error"
            state.last_written = None
            state.retries = 0
            return Action(None, "error", "the thermostat did not take the setpoint: control paused")
        state.override_until = now + s.override_hold
        state.pause_status = "override"
        state.last_written = None
        return Action(None, "override", "setpoint changed by hand: control paused")
    if state.last_written is not None and _same(current, state.last_written):
        state.retries = 0

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
    if current is not None and abs(target - current) < s.min_step - 1e-9:
        demand_changes = living is not None and (target > living) != (current > living)
        if not demand_changes:
            return Action(None, "steady", f"{source}: setpoint close enough")
    caught_up = (current is not None and living is not None
                 and current <= living + 0.05 and target > living)
    if (
        state.last_write is not None
        and source == state.last_source
        and now - state.last_write < s.min_interval
        and not caught_up   # the room reached the setpoint: write at once, or tado decides
    ):
        return Action(None, "waiting", f"{source}: next change after the minimum interval")
    return Action(target, "writing", f"{source}: {reason}")
