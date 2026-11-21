"""Explain what changed between two plans, and why."""

from __future__ import annotations

from dataclasses import dataclass

from .model import CalendarEvent, EventKind, Household, Plan


@dataclass
class Change:
    chore_id: str
    chore_name: str
    kind: str  # moved | added | dropped
    before: str | None
    after: str | None
    because: str | None

    def describe(self) -> str:
        if self.kind == "moved":
            text = f"{self.chore_name}: moved {self.before} -> {self.after}"
        elif self.kind == "added":
            text = f"{self.chore_name}: now scheduled {self.after}"
        else:
            text = f"{self.chore_name}: dropped (was {self.before})"
        return text + (f" because {self.because}" if self.because else "")


def _span(a) -> str:
    return f"{a.start:%H:%M}-{a.end:%H:%M}"


def _culprit(old_h: Household, new_h: Household, start, end) -> str | None:
    """Find a calendar event that is new in `new_h` and overlaps [start, end)."""

    def key(e: CalendarEvent):
        return (e.member, e.title, e.start, e.end, e.kind, e.room)

    old_keys = {key(e) for e in old_h.events}
    for e in new_h.events:
        if key(e) in old_keys or not (e.start < end and start < e.end):
            continue
        what = {EventKind.AWAY: "is out", EventKind.MEETING: "has a call", EventKind.FOCUS: "is busy"}[e.kind]
        return f"{e.member} {what} {e.start:%H:%M}-{e.end:%H:%M} ({e.title})"
    return None


def diff(old_h: Household, old: Plan, new_h: Household, new: Plan) -> list[Change]:
    before = {a.chore_id: a for a in old.assignments}
    after = {a.chore_id: a for a in new.assignments}
    changes: list[Change] = []
    for cid, a in before.items():
        b = after.get(cid)
        if b is None:
            changes.append(
                Change(cid, a.chore_name, "dropped", _span(a), None, _culprit(old_h, new_h, a.start, a.end))
            )
        elif (a.start, a.end) != (b.start, b.end):
            because = _culprit(old_h, new_h, a.start, a.end)
            if because is None:
                because = (
                    "an earlier slot opened up"
                    if b.start < a.start
                    else "another chore now needs its old slot"
                )
            changes.append(Change(cid, a.chore_name, "moved", _span(a), _span(b), because))
    for cid, b in after.items():
        if cid not in before:
            changes.append(Change(cid, b.chore_name, "added", None, _span(b), None))
    return sorted(changes, key=lambda c: c.chore_id)
