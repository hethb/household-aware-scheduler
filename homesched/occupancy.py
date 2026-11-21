"""Turn calendars and routines into a per-slot picture of who is where.

The planning day is split into fixed slots (5 minutes by default). For each
slot we derive which members are home, which rooms they occupy, and whether
anyone at home is on a call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from .model import EventKind, Household, Member

SLOT_MIN = 5


@dataclass
class Slot:
    start: datetime
    home: set[str] = field(default_factory=set)
    #: room -> members in it
    rooms: dict[str, set[str]] = field(default_factory=dict)
    on_call: set[str] = field(default_factory=set)
    quiet_hours: bool = False

    @property
    def house_empty(self) -> bool:
        return not self.home

    def occupied(self, room: str) -> bool:
        return bool(self.rooms.get(room))


def in_window(t: time, start: time, end: time) -> bool:
    """True if t is in [start, end), handling windows that wrap past midnight."""
    if start <= end:
        return start <= t < end
    return t >= start or t < end


def _room_for(member: Member, t: time) -> str:
    for rule in member.rules:
        if in_window(t, rule.start, rule.end):
            return rule.room
    return member.default_room


def build_slots(h: Household) -> list[Slot]:
    start = datetime.combine(h.day, h.day_start)
    end = datetime.combine(h.day, h.day_end)
    slots: list[Slot] = []
    t = start
    while t < end:
        slots.append(Slot(start=t, quiet_hours=in_window(t.time(), h.quiet_start, h.quiet_end)))
        t += timedelta(minutes=SLOT_MIN)

    for s in slots:
        mid = s.start + timedelta(minutes=SLOT_MIN / 2)
        for m in h.members:
            events = [e for e in h.events if e.member == m.name and e.start <= mid < e.end]
            if any(e.kind == EventKind.AWAY for e in events):
                continue
            s.home.add(m.name)
            room = _room_for(m, mid.time())
            for e in events:
                if e.room:
                    room = e.room
                if e.kind == EventKind.MEETING:
                    s.on_call.add(m.name)
            s.rooms.setdefault(room, set()).add(m.name)
    return slots
