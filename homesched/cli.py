"""Command line interface.

homesched plan examples/household.json
homesched plan examples/household.json --ics Priya=examples/priya.ics --work-room Priya=office
homesched whatif examples/household.json --add "Priya,meeting,08:00,08:45,Early client call,office"
homesched serve examples/household.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from .calendars import load_ics
from .diff import diff
from .model import CalendarEvent, EventKind, Household, Plan
from .scheduler import schedule


def load_household(path: str, ics: list[str], work_rooms: list[str]) -> Household:
    h = Household.model_validate_json(Path(path).read_text())
    rooms = dict(x.split("=", 1) for x in work_rooms)
    for spec in ics:
        member, file = spec.split("=", 1)
        h.events.extend(load_ics(file, member, h.day, h.timezone, rooms.get(member)))
    return Household.model_validate(h.model_dump())


def parse_event(h: Household, spec: str) -> CalendarEvent:
    """member,kind,HH:MM,HH:MM,title[,room]"""
    parts = [p.strip() for p in spec.split(",")]
    if len(parts) < 5:
        raise SystemExit("event must be member,kind,start,end,title[,room]")
    member, kind, start, end, title = parts[:5]
    room = parts[5] if len(parts) > 5 else None
    day = h.day.isoformat()
    return CalendarEvent(
        member=member,
        kind=EventKind(kind),
        start=datetime.fromisoformat(f"{day}T{start}"),
        end=datetime.fromisoformat(f"{day}T{end}"),
        title=title,
        room=room,
    )


def print_plan(p: Plan, out=sys.stdout) -> None:
    print(f"{p.household}, {p.day:%A %d %B %Y}\n", file=out)
    print(f"{'time':<13}{'chore':<22}{'room':<17}{'battery':>8}  why", file=out)
    for a in p.assignments:
        print(
            f"{a.start:%H:%M}-{a.end:%H:%M}  {a.chore_name:<22}{a.room:<17}"
            f"{a.battery_after_min:>6.0f}m  {'; '.join(a.reasons)}",
            file=out,
        )
    if p.unscheduled:
        print("\nNot scheduled:", file=out)
        for u in p.unscheduled:
            print(f"  {u.chore_name}: {u.reason}", file=out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="homesched", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "whatif", "serve"):
        sp = sub.add_parser(name)
        sp.add_argument("household")
        sp.add_argument(
            "--ics",
            action="append",
            default=[],
            metavar="MEMBER=FILE",
            help="merge an .ics calendar for a member",
        )
        sp.add_argument(
            "--work-room",
            action="append",
            default=[],
            metavar="MEMBER=ROOM",
            help="room used for calls at home",
        )
        if name == "plan":
            sp.add_argument("--json", action="store_true", help="print the plan as JSON")
        if name == "whatif":
            sp.add_argument(
                "--add",
                action="append",
                default=[],
                metavar="EVENT",
                help="member,kind,HH:MM,HH:MM,title[,room]",
            )
        if name == "serve":
            sp.add_argument("--port", type=int, default=8000)
    a = ap.parse_args(argv)
    h = load_household(a.household, a.ics, a.work_room)

    if a.cmd == "plan":
        p = schedule(h)
        if a.json:
            print(json.dumps(p.model_dump(mode="json"), indent=2))
        else:
            print_plan(p)
        return 0
    if a.cmd == "whatif":
        new_h = h.model_copy(deep=True)
        new_h.events.extend(parse_event(h, e) for e in a.add)
        new_h = Household.model_validate(new_h.model_dump())
        changes = diff(h, schedule(h), new_h, schedule(new_h))
        print("No changes to the plan." if not changes else "\n".join(c.describe() for c in changes))
        return 0
    if a.cmd == "serve":
        import uvicorn

        from .web import create_app

        uvicorn.run(create_app(h), host="127.0.0.1", port=a.port)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
