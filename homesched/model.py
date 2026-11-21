"""Data model. Pydantic models so households and plans round-trip through JSON."""

from __future__ import annotations

from datetime import date, datetime, time
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator


class EventKind(StrEnum):
    AWAY = "away"  # member is out of the house
    MEETING = "meeting"  # member is home and on a call: keep it quiet
    FOCUS = "focus"  # member is home in a room and should not be disturbed in it


class CalendarEvent(BaseModel):
    member: str
    title: str
    start: datetime
    end: datetime
    kind: EventKind = EventKind.AWAY
    #: Room the member occupies during the event (meetings and focus blocks).
    room: str | None = None

    @model_validator(mode="after")
    def _ordered(self) -> CalendarEvent:
        if self.end <= self.start:
            raise ValueError(f"event '{self.title}' ends before it starts")
        return self


class RoomRule(BaseModel):
    """Where a member usually is when home: e.g. bedroom 22:00-07:00."""

    room: str
    start: time
    end: time  # may wrap past midnight


class Member(BaseModel):
    name: str
    #: Room a member is in when home and no rule or event says otherwise.
    default_room: str = "living room"
    rules: list[RoomRule] = Field(default_factory=list)


class Noise(StrEnum):
    QUIET = "quiet"
    LOUD = "loud"


class Chore(BaseModel):
    id: str
    name: str
    room: str
    duration_min: int = Field(gt=0)
    earliest: time = time(6, 0)
    deadline: time = time(23, 0)
    priority: int = 2  # 1 low .. 3 high
    noise: Noise = Noise.QUIET
    #: Safety: only run with nobody in the room (the robot also locks on presence).
    requires_empty_room: bool = True
    #: Chore ids that must finish first (unload dryer before folding).
    after: list[str] = Field(default_factory=list)


class Robot(BaseModel):
    name: str = "robot"
    dock_room: str = "living room"
    #: Minutes of work on a full battery.
    battery_work_min: int = 120
    #: Work-minutes of charge regained per minute docked.
    charge_rate: float = 0.5
    #: Minutes to move between two different rooms.
    travel_min: int = 3
    #: Work-minutes always kept in reserve so the robot can get back to its dock.
    reserve_min: int = 10


class Household(BaseModel):
    name: str
    timezone: str = "America/New_York"
    day: date
    day_start: time = time(6, 0)
    day_end: time = time(23, 0)
    quiet_start: time = time(21, 30)
    quiet_end: time = time(7, 30)
    rooms: list[str]
    members: list[Member]
    events: list[CalendarEvent] = Field(default_factory=list)
    chores: list[Chore]
    robot: Robot = Field(default_factory=Robot)

    @model_validator(mode="after")
    def _consistent(self) -> Household:
        rooms = set(self.rooms)
        names = {m.name for m in self.members}
        ids = {c.id for c in self.chores}
        for c in self.chores:
            if c.room not in rooms:
                raise ValueError(f"chore '{c.id}' is in unknown room '{c.room}'")
            for dep in c.after:
                if dep not in ids:
                    raise ValueError(f"chore '{c.id}' depends on unknown chore '{dep}'")
        for e in self.events:
            if e.member not in names:
                raise ValueError(f"event '{e.title}' belongs to unknown member '{e.member}'")
            if e.room is not None and e.room not in rooms:
                raise ValueError(f"event '{e.title}' is in unknown room '{e.room}'")
        return self


class Assignment(BaseModel):
    chore_id: str
    chore_name: str
    room: str
    start: datetime
    end: datetime
    travel_min: int
    battery_after_min: float
    reasons: list[str]


class Unscheduled(BaseModel):
    chore_id: str
    chore_name: str
    reason: str


class Plan(BaseModel):
    household: str
    day: date
    assignments: list[Assignment]
    unscheduled: list[Unscheduled]
    #: Per-slot presence summary for display: slot start -> members at home.
    presence: list[tuple[datetime, list[str]]]
