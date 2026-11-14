"""Battery reserve: keep a battery that feeds a backup output above its floor (pure logic).

Some plug-in batteries power their backup socket from the battery itself, also while the
grid is there. Whatever hangs on that socket (Home Assistant, the network) slowly drains
the battery once its discharge floor stops the normal output, until the battery shuts
down and takes the socket with it. The reserve guard charges that battery from the grid
before that happens:

- start when the state of charge is at or below `start_soc`, and the grid meter has
  reported values for `grid_ok_for` (no grid, nothing to charge from);
- stop at `stop_soc`, when the grid meter stops reporting, when the guard is switched
  off, or after `max_charge` (a charge that does not raise the state of charge is a
  fault: then no new start for `error_hold`);
- anti-flap: no new start within `min_pause` of a stop. The band between start and stop
  is the hysteresis.

`decide` only says what should happen; the coordinator does the service calls (switch the
battery to external control, set the charge power and the target state of charge, force
charge; afterwards standby and back to its own control) and then calls `record_start` or
`record_stop`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

STATUSES = ["off", "not_configured", "idle", "waiting", "charging", "error"]
START, STOP = "start", "stop"


@dataclass(frozen=True)
class ReserveSettings:
    start_soc: float = 15.0
    stop_soc: float = 24.0
    power_w: float = 1000.0
    grid_ok_for: timedelta = timedelta(minutes=1)
    min_pause: timedelta = timedelta(minutes=15)
    max_charge: timedelta = timedelta(hours=3)
    error_hold: timedelta = timedelta(hours=1)


@dataclass(frozen=True)
class Decision:
    action: str | None   # START, STOP or None
    status: str          # one of STATUSES
    reason: str


@dataclass
class ReserveState:
    charging: bool = False
    since: datetime | None = None          # start of the charge in progress
    stopped_at: datetime | None = None     # last stop
    error_until: datetime | None = None    # no new start before this (after a fault)
    grid_ok_since: datetime | None = None  # the grid meter reports values since
    restore: dict[str, float] = field(default_factory=dict)   # entity -> value before the charge

    def record_start(self, now: datetime, restore: dict[str, float] | None = None) -> None:
        self.charging, self.since = True, now
        self.restore = dict(restore or {})

    def record_stop(self, now: datetime, fault: bool = False,
                    s: ReserveSettings | None = None) -> None:
        s = s or ReserveSettings()
        self.charging, self.since, self.stopped_at = False, None, now
        self.restore = {}
        if fault:
            self.error_until = now + s.error_hold

    def to_dict(self) -> dict:
        def iso(value: datetime | None) -> str | None:
            return value.isoformat() if value else None

        return {"charging": self.charging, "since": iso(self.since),
                "stopped_at": iso(self.stopped_at), "error_until": iso(self.error_until),
                "restore": dict(self.restore)}

    @classmethod
    def from_dict(cls, data: dict | None) -> ReserveState:
        def parse(value: str | None) -> datetime | None:
            return datetime.fromisoformat(value) if value else None
        data = data or {}
        return cls(charging=bool(data.get("charging")), since=parse(data.get("since")),
                   stopped_at=parse(data.get("stopped_at")),
                   error_until=parse(data.get("error_until")),
                   restore={k: float(v) for k, v in (data.get("restore") or {}).items()})


def valid(s: ReserveSettings) -> bool:
    """The stop level must sit clearly above the start level (hysteresis)."""
    return 0 <= s.start_soc and s.stop_soc <= 100 and s.stop_soc - s.start_soc >= 2


def decide(state: ReserveState, enabled: bool, soc: float | None, grid_ok: bool,
           now: datetime, s: ReserveSettings | None = None) -> Decision:
    """What the guard should do now. Changes only the grid timer in `state`."""
    s = s or ReserveSettings()
    if grid_ok:
        if state.grid_ok_since is None:
            state.grid_ok_since = now
    else:
        state.grid_ok_since = None

    if state.charging:
        if not enabled:
            return Decision(STOP, "off", "reserve guard switched off: charge stopped")
        if not grid_ok:
            return Decision(STOP, "waiting", "the grid meter gives no values: charge stopped")
        if soc is not None and soc >= s.stop_soc:
            return Decision(STOP, "idle", f"charged to {soc:.0f}% (stop at {s.stop_soc:.0f}%)")
        if state.since is not None and now - state.since >= s.max_charge:
            return Decision(STOP, "error",
                            f"still {soc if soc is not None else '?'}% after "
                            f"{s.max_charge.total_seconds() / 3600:.1f} h of charging: stopped")
        return Decision(None, "charging",
                        f"charging to {s.stop_soc:.0f}% ({soc if soc is not None else '?'}% now)")

    if not enabled:
        return Decision(None, "off", "reserve guard switched off")
    if soc is None:
        return Decision(None, "waiting", "state of charge unknown")
    if state.error_until is not None and now < state.error_until:
        return Decision(None, "error", "the last charge did not work: waiting before a new try")
    if soc > s.start_soc:
        return Decision(None, "idle", f"{soc:.0f}% (start at {s.start_soc:.0f}%)")
    if state.grid_ok_since is None or now - state.grid_ok_since < s.grid_ok_for:
        return Decision(None, "waiting", f"{soc:.0f}%, but the grid meter gives no values")
    if state.stopped_at is not None and now - state.stopped_at < s.min_pause:
        return Decision(None, "waiting", f"{soc:.0f}%: next start after the minimum pause (anti-flap)")
    return Decision(START, "charging", f"{soc:.0f}%: at or below {s.start_soc:.0f}%, charging "
                                       f"to {s.stop_soc:.0f}% at {s.power_w:.0f} W")
