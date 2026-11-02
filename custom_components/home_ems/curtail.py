"""PV curtailment: which inverter to switch off at a negative price (pure logic).

With a dynamic contract, export at a negative market price costs money. Each inverter
sits behind its own switch, so the EMS can switch them off one by one:
- only when the market price is negative;
- an inverter only goes off when it delivers less than the export right now, otherwise
  the house would buy power instead (and buying still costs energy tax);
- the smallest one first; both when the export is larger than both together;
- once off, it stays off for at least `min_off` (an inverter needs a minute or more to
  come back), and comes back as soon as the price is no longer negative or after
  `max_off`.
Shadow mode: nothing is switched; the advice and what it would have avoided are shown.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta


@dataclass(frozen=True)
class CurtailSettings:
    min_off: timedelta = timedelta(minutes=15)
    max_off: timedelta = timedelta(hours=4)
    margin_w: float = 200.0      # keep this much export as a margin (clouds, load changes)
    max_step: timedelta = timedelta(minutes=5)


@dataclass
class CurtailState:
    off_since: dict[str, datetime] = field(default_factory=dict)   # entity -> switched off at
    last: datetime | None = None
    avoided_kwh: float = 0.0     # export that would not have happened (shadow)
    avoided_eur: float = 0.0     # what that export would have cost at the negative price
    day: object = None

    def roll(self, now: datetime) -> None:
        if self.day != now.date():
            self.day, self.avoided_kwh, self.avoided_eur = now.date(), 0.0, 0.0


@dataclass(frozen=True)
class CurtailAdvice:
    off: tuple[str, ...]         # inverters that would be off
    reason: str


def advise(state: CurtailState, inverters: dict[str, float | None], export_w: float | None,
           market: float | None, now: datetime, s: CurtailSettings | None = None) -> CurtailAdvice:
    s = s or CurtailSettings()
    state.roll(now)
    # what the inverters that were off would have exported in the past step (shadow)
    if state.last is not None and timedelta(0) < now - state.last <= s.max_step and market is not None:
        hours = (now - state.last).total_seconds() / 3600
        kwh = sum((inverters.get(e) or 0.0) for e in state.off_since) / 1000 * hours
        state.avoided_kwh += kwh
        state.avoided_eur += -market * kwh if market < 0 else 0.0
    state.last = now

    if market is None or market >= 0 or export_w is None:
        state.off_since.clear()
        return CurtailAdvice((), "price not negative: all inverters on")
    for e, since in list(state.off_since.items()):
        if now - since >= s.max_off:
            del state.off_since[e]

    # in shadow mode the inverters really produce, so their power is the export they add
    off = set(state.off_since)
    room = export_w - s.margin_w - sum((inverters.get(e) or 0.0) for e in off)
    for e, power in sorted(((e, p) for e, p in inverters.items() if e not in off and p),
                           key=lambda x: x[1]):
        if power <= room:
            state.off_since[e] = now
            off.add(e)
            room -= power
    # back on early only if the price is no longer negative (above) or the export is gone
    for e in list(off):
        if room < -(inverters.get(e) or 0.0) and now - state.off_since[e] >= s.min_off:
            del state.off_since[e]
            off.discard(e)
            room += inverters.get(e) or 0.0
    if not off:
        return CurtailAdvice((), "negative price, but each inverter delivers more than the export")
    return CurtailAdvice(tuple(sorted(off)), f"negative price ({market:.3f} EUR/kWh): {len(off)} off")
