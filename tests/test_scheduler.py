from datetime import date, datetime, time
from itertools import pairwise
from pathlib import Path

import pytest

from homesched import Household, schedule
from homesched.calendars import classify
from homesched.diff import diff
from homesched.model import CalendarEvent, Chore, EventKind, Member, Noise, Robot
from homesched.occupancy import build_slots, in_window

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "household.json"
DAY = date(2026, 11, 23)


def at(hhmm: str) -> datetime:
    return datetime.combine(DAY, time.fromisoformat(hhmm))


def house(chores, events=(), members=None, robot=None, **kw) -> Household:
    return Household(
        name="t",
        day=DAY,
        rooms=["living room", "kitchen", "bedroom", "office"],
        members=members or [Member(name="A", default_room="living room")],
        events=list(events),
        chores=list(chores),
        robot=robot or Robot(battery_work_min=600, travel_min=0),
        **kw,
    )


def example() -> Household:
    return Household.model_validate_json(EXAMPLE.read_text())


def overlaps(a0, a1, b0, b1) -> bool:
    return a0 < b1 and b0 < a1


# --- occupancy ---------------------------------------------------------------


def test_window_wraps_midnight():
    assert in_window(time(23, 0), time(22, 0), time(7, 0))
    assert in_window(time(3, 0), time(22, 0), time(7, 0))
    assert not in_window(time(12, 0), time(22, 0), time(7, 0))


def test_away_event_removes_member_from_home():
    h = house([], events=[CalendarEvent(member="A", title="out", start=at("09:00"), end=at("10:00"))])
    slots = {s.start: s for s in build_slots(h)}
    assert "A" not in slots[at("09:30")].home
    assert "A" in slots[at("10:00")].home


# --- hard constraints -------------------------------------------------------


def test_requires_empty_room_is_never_violated_in_example():
    h = example()
    plan = schedule(h)
    slots = build_slots(h)
    for a in plan.assignments:
        chore = next(c for c in h.chores if c.id == a.chore_id)
        if not chore.requires_empty_room:
            continue
        for s in slots:
            if a.start <= s.start < a.end:
                assert not s.occupied(a.room), f"{a.chore_name} runs while {a.room} is occupied at {s.start}"


def test_loud_chore_avoids_calls_and_quiet_hours():
    loud = Chore(
        id="vac",
        name="Vacuum",
        room="kitchen",
        duration_min=30,
        earliest=time(7, 0),
        deadline=time(11, 0),
        noise=Noise.LOUD,
    )
    call = CalendarEvent(
        member="A", title="call", start=at("07:00"), end=at("09:00"), kind=EventKind.MEETING, room="office"
    )
    plan = schedule(house([loud], events=[call], quiet_start=time(21, 0), quiet_end=time(7, 30)))
    (a,) = plan.assignments
    assert a.start >= at("09:00")


def test_no_two_chores_overlap_and_travel_fits_in_example():
    plan = schedule(example())
    xs = sorted(plan.assignments, key=lambda a: a.start)
    for prev, nxt in pairwise(xs):
        assert prev.end <= nxt.start
        if prev.room != nxt.room:
            assert (nxt.start - prev.end).total_seconds() / 60 >= nxt.travel_min


def test_dependencies_respected():
    plan = schedule(example())
    by = {a.chore_id: a for a in plan.assignments}
    assert by["unload_dryer"].end <= by["fold_laundry"].start
    assert by["unload_dryer"].end <= by["fold_towels"].start


def test_battery_never_negative_and_forces_a_break():
    chores = [
        Chore(
            id=f"c{i}",
            name=f"c{i}",
            room="kitchen",
            duration_min=40,
            earliest=time(8, 0),
            deadline=time(23, 0),
        )
        for i in range(3)
    ]
    plan = schedule(
        house(chores, robot=Robot(battery_work_min=60, charge_rate=1.0, travel_min=0, reserve_min=0))
    )
    assert len(plan.assignments) == 3
    assert all(a.battery_after_min >= 0 for a in plan.assignments)
    xs = sorted(plan.assignments, key=lambda a: a.start)
    # 40 min drains to 20; the next 40 min chore needs at least 20 min of charging first.
    assert (xs[1].start - xs[0].end).total_seconds() >= 20 * 60


def test_battery_reserve_is_kept_in_example():
    h = example()
    assert all(a.battery_after_min >= h.robot.reserve_min for a in schedule(h).assignments)


def test_window_is_respected():
    c = Chore(id="x", name="x", room="kitchen", duration_min=30, earliest=time(13, 0), deadline=time(14, 0))
    (a,) = schedule(house([c])).assignments
    assert at("13:00") <= a.start and a.end <= at("14:00")


# --- soft preferences and explanations --------------------------------------


def test_prefers_empty_house():
    c = Chore(id="x", name="x", room="kitchen", duration_min=30, earliest=time(8, 0), deadline=time(18, 0))
    out = CalendarEvent(member="A", title="work", start=at("12:00"), end=at("15:00"))
    (a,) = schedule(house([c], events=[out])).assignments
    assert at("12:00") <= a.start and a.end <= at("15:00")
    assert "nobody home" in a.reasons


def test_impossible_chore_is_reported_with_reason():
    c = Chore(
        id="x", name="Make bed", room="bedroom", duration_min=30, earliest=time(8, 0), deadline=time(9, 0)
    )
    m = Member(name="A", default_room="bedroom")
    plan = schedule(house([c], members=[m]))
    assert not plan.assignments
    (u,) = plan.unscheduled
    assert "bedroom occupied" in u.reason


def test_dependent_of_unscheduled_chore_is_reported():
    first = Chore(id="a", name="a", room="bedroom", duration_min=30, earliest=time(8, 0), deadline=time(9, 0))
    second = Chore(id="b", name="b", room="kitchen", duration_min=10, after=["a"])
    plan = schedule(house([first, second], members=[Member(name="A", default_room="bedroom")]))
    reasons = {u.chore_id: u.reason for u in plan.unscheduled}
    assert "waits on 'a'" in reasons["b"]


# --- replanning ---------------------------------------------------------------


def test_new_call_moves_loud_chore_and_explains_it():
    h = example()
    before = schedule(h)
    dish = next(a for a in before.assignments if a.chore_id == "dishwasher")
    new_h = h.model_copy(deep=True)
    new_h.events.append(
        CalendarEvent(
            member="Priya",
            title="Early client call",
            start=dish.start,
            end=dish.end,
            kind=EventKind.MEETING,
            room="office",
        )
    )
    new_h = Household.model_validate(new_h.model_dump())
    changes = diff(h, before, new_h, schedule(new_h))
    moved = next(c for c in changes if c.chore_id == "dishwasher")
    assert moved.kind == "moved"
    assert "Priya has a call" in moved.describe()


# --- validation and calendars ----------------------------------------------


def test_unknown_room_rejected():
    with pytest.raises(ValueError):
        house([Chore(id="x", name="x", room="garage", duration_min=10)])


def test_cycle_rejected():
    a = Chore(id="a", name="a", room="kitchen", duration_min=10, after=["b"])
    b = Chore(id="b", name="b", room="kitchen", duration_min=10, after=["a"])
    with pytest.raises(ValueError, match="cycle"):
        schedule(house([a, b]))


@pytest.mark.parametrize(
    "title,location,kind",
    [
        ("Team standup", "https://meet.google.com/abc", EventKind.MEETING),
        ("1:1 with Alex", None, EventKind.MEETING),
        ("Dentist", "120 Main St", EventKind.AWAY),
        ("Lunch with Jo", "Cafe Lola", EventKind.AWAY),
        ("Deep work", "Home office", EventKind.FOCUS),
        ("Client meeting", "Their office, 5th Ave", EventKind.AWAY),
    ],
)
def test_classify(title, location, kind):
    assert classify(title, location, None) == kind


def test_load_ics(tmp_path):
    from homesched.calendars import load_ics

    ics = tmp_path / "a.ics"
    ics.write_text(
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n"
        "BEGIN:VEVENT\r\nUID:1\r\nSUMMARY:Standup\r\nLOCATION:https://zoom.us/j/1\r\n"
        "DTSTART:20261123T140000Z\r\nDTEND:20261123T143000Z\r\nEND:VEVENT\r\n"
        "BEGIN:VEVENT\r\nUID:2\r\nSUMMARY:Holiday\r\nDTSTART;VALUE=DATE:20261123\r\nDTEND;VALUE=DATE:20261124\r\nEND:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    (e,) = load_ics(ics, "A", DAY, "America/New_York", work_room="office")
    assert e.kind == EventKind.MEETING
    assert e.start == at("09:00") and e.room == "office"
