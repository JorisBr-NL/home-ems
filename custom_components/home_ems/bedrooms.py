"""Bedrooms: when the radiator valves upstairs should open (pure logic, no HA imports).

The radiator thermostats in the bedrooms only open a valve; warm water only flows while
the living room asks for heat. So the cheapest moment to warm a bedroom is during a run
that happens anyway (a planned run on solar or cheap power), some hours before bedtime.

Per room, in the evening window (`lead` before bedtime until bedtime):
- heat flows (a run is going or starting): open the valve (setpoint = target);
- shortly before bedtime (`catch_up`) and the room clearly below target: open the valve
  and ask the arbiter for a short run, because otherwise no warm water comes;
- otherwise: the minimum, so the valve stays closed.
Outside the window: the minimum. Shadow mode: nothing is written.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any


@dataclass(frozen=True)
class Bedroom:
    entity_id: str
    name: str
    target: float = 17.0
    bedtime: time = time(21, 0)


@dataclass(frozen=True)
class BedroomSettings:
    min_temp: float = 13.0
    lead: timedelta = timedelta(hours=6)          # evening window before bedtime
    catch_up: timedelta = timedelta(minutes=90)   # last part: the room may ask for a run
    margin: float = 0.3                           # catch up when below target - margin


@dataclass(frozen=True)
class RoomTarget:
    entity_id: str
    name: str
    setpoint: float
    reason: str
    needs_run: bool = False
    temperature: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"entity_id": self.entity_id, "name": self.name, "setpoint": self.setpoint,
                "reason": self.reason, "needs_run": self.needs_run,
                "temperature": self.temperature}


def parse_rooms(data: Any) -> list[Bedroom]:
    """Rooms from the YAML option: a list of {entiteit, naam, doel, bedtijd}."""
    if not data:
        return []
    if not isinstance(data, list):
        raise ValueError("expected a list of rooms")
    rooms = []
    for item in data:
        if not isinstance(item, dict) or not str(item.get("entiteit", "")).startswith("climate."):
            raise ValueError("every room needs 'entiteit' (a climate entity)")
        bedtime = item.get("bedtijd", "21:00")
        if isinstance(bedtime, int):  # YAML reads 21:00 without quotes as minutes (sexagesimal)
            bedtime = f"{bedtime // 60:02d}:{bedtime % 60:02d}"
        rooms.append(Bedroom(entity_id=item["entiteit"], name=str(item.get("naam") or item["entiteit"]),
                             target=float(item.get("doel", 17.0)),
                             bedtime=time.fromisoformat(str(bedtime))))
    return rooms


def room_targets(rooms: list[Bedroom], temps: dict[str, float | None], now: datetime,
                 heat_flowing: bool, s: BedroomSettings | None = None) -> list[RoomTarget]:
    s = s or BedroomSettings()
    out = []
    for room in rooms:
        temp = temps.get(room.entity_id)
        bed = now.replace(hour=room.bedtime.hour, minute=room.bedtime.minute, second=0, microsecond=0)
        if not bed - s.lead <= now < bed:
            out.append(RoomTarget(room.entity_id, room.name, s.min_temp, "outside the evening window",
                                  temperature=temp))
        elif heat_flowing:
            out.append(RoomTarget(room.entity_id, room.name, room.target,
                                  "heat flows: warm up before bedtime", temperature=temp))
        elif now >= bed - s.catch_up and temp is not None and temp < room.target - s.margin:
            out.append(RoomTarget(room.entity_id, room.name, room.target,
                                  "below target shortly before bedtime: needs a run", True, temp))
        else:
            out.append(RoomTarget(room.entity_id, room.name, s.min_temp,
                                  "no heat flowing: valve closed", temperature=temp))
    return out
