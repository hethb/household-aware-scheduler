"""Chore scheduler.

Hard constraints
    * each chore runs inside its time window and after the chores it depends on
    * one robot, one chore at a time, with travel time between rooms
    * safety: chores that need an empty room never overlap someone being in it
    * loud chores never run during quiet hours or while anyone home is on a call
    * the battery never drops below its reserve: work drains it, docked idle time recharges it

Soft preferences (scored among feasible starts)
    * run while nobody is home
    * finish sooner rather than later
    * avoid extra travel

Chores are placed greedily in order of priority, then deadline, with
dependencies first. For every chore the scheduler scores every feasible start
and keeps the best, and it records why a chore landed where it did, or why it
could not be placed at all.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta

from .model import Assignment, Chore, Household, Noise, Plan, Unscheduled
from .occupancy import SLOT_MIN, Slot, build_slots

W_EMPTY = 3.0
W_SOON = 1.0
W_TRAVEL = 0.2


@dataclass
class _Placed:
    chore: Chore
    start: int  # slot index of first work slot (after travel)
    end: int  # exclusive
    travel: int  # travel slots before start


def _slots(minutes: int) -> int:
    return -(-minutes // SLOT_MIN)


def _order(chores: list[Chore]) -> list[Chore]:
    """Priority then deadline, with every chore after its dependencies."""
    by_id = {c.id: c for c in chores}
    ranked = sorted(chores, key=lambda c: (-c.priority, c.deadline, c.id))
    out: list[Chore] = []
    seen: set[str] = set()

    def visit(c: Chore, stack: tuple[str, ...] = ()) -> None:
        if c.id in seen:
            return
        if c.id in stack:
            raise ValueError(f"dependency cycle: {' -> '.join((*stack, c.id))}")
        for d in sorted((by_id[x] for x in c.after), key=lambda c: (-c.priority, c.deadline)):
            visit(d, (*stack, c.id))
        seen.add(c.id)
        out.append(c)

    for c in ranked:
        visit(c)
    return out


class Scheduler:
    def __init__(self, h: Household):
        self.h = h
        self.slots: list[Slot] = build_slots(h)
        self.placed: list[_Placed] = []
        self.travel_slots = _slots(h.robot.travel_min) if h.robot.travel_min else 0

    # ---- constraints -------------------------------------------------
    def _index(self, t) -> int:
        start = datetime.combine(self.h.day, self.h.day_start)
        dt = datetime.combine(self.h.day, t) - start
        return max(0, min(len(self.slots), int(dt.total_seconds() // 60) // SLOT_MIN))

    def _slot_blocker(self, c: Chore, i: int) -> str | None:
        s = self.slots[i]
        if c.requires_empty_room and s.occupied(c.room):
            who = ", ".join(sorted(s.rooms[c.room]))
            return f"{c.room} occupied ({who})"
        if c.noise == Noise.LOUD and s.quiet_hours:
            return "quiet hours"
        if c.noise == Noise.LOUD and s.on_call:
            return f"{', '.join(sorted(s.on_call))} on a call"
        return None

    def _room_before(self, start: int) -> str:
        prev = [p for p in self.placed if p.end <= start]
        return max(prev, key=lambda p: p.end).chore.room if prev else self.h.robot.dock_room

    def _battery_ok(self, extra: _Placed | None) -> tuple[bool, dict[str, float]]:
        r = self.h.robot
        timeline = sorted(self.placed + ([extra] if extra else []), key=lambda p: p.start)
        level = float(r.battery_work_min)
        t = 0
        after: dict[str, float] = {}
        for p in timeline:
            busy_from = p.start - p.travel
            idle = max(0, busy_from - t) * SLOT_MIN
            level = min(r.battery_work_min, level + idle * r.charge_rate)
            level -= (p.end - busy_from) * SLOT_MIN
            if level < r.reserve_min:
                return False, {}
            after[p.chore.id] = level
            t = p.end
        return True, after

    def _feasible(self, c: Chore, s: int) -> tuple[_Placed | None, str | None]:
        n = _slots(c.duration_min)
        lo = self._index(c.earliest)
        hi = self._index(c.deadline)
        for dep in c.after:
            done = next((p for p in self.placed if p.chore.id == dep), None)
            if done is None:
                return None, f"waits on '{dep}', which could not be scheduled"
            lo = max(lo, done.end)
        travel = self.travel_slots if self._room_before(s) != c.room else 0
        if s - travel < 0 or s < lo or s + n > hi:
            return None, "outside its time window"
        for p in self.placed:
            if s - travel < p.end and p.start - p.travel < s + n:
                return None, "robot busy with another chore"
        nxt = min((p for p in self.placed if p.start >= s + n), key=lambda p: p.start, default=None)
        if nxt is not None:
            need = self.travel_slots if nxt.chore.room != c.room else 0
            if nxt.start - need < s + n:
                return None, "robot busy with another chore"
        for i in range(s, s + n):
            why = self._slot_blocker(c, i)
            if why:
                return None, why
        cand = _Placed(c, s, s + n, travel)
        ok, _ = self._battery_ok(cand)
        if not ok:
            return None, "battery would run out"
        return cand, None

    # ---- scoring and explanation ------------------------------------
    def _score(self, c: Chore, p: _Placed) -> float:
        lo = self._index(c.earliest)
        hi = self._index(c.deadline)
        empty = sum(self.slots[i].house_empty for i in range(p.start, p.end)) / (p.end - p.start)
        soon = 1 - (p.start - lo) / max(1, hi - lo)
        return W_EMPTY * empty + W_SOON * soon - W_TRAVEL * p.travel

    def _reasons(self, c: Chore, p: _Placed) -> list[str]:
        span = [self.slots[i] for i in range(p.start, p.end)]
        out: list[str] = []
        if all(s.house_empty for s in span):
            out.append("nobody home")
        else:
            home = sorted(set().union(*(s.home for s in span)))
            if c.requires_empty_room:
                out.append(f"{c.room} clear while {', '.join(home)} home")
            else:
                out.append(f"{', '.join(home)} home")
        if c.noise == Noise.LOUD:
            out.append("no calls or quiet hours")
        if c.after:
            out.append("after " + ", ".join(c.after))
        if p.travel:
            out.append(f"travels from {self._room_before(p.start - p.travel)}")
        return out

    # ---- main ---------------------------------------------------------
    def run(self) -> Plan:
        unscheduled: list[Unscheduled] = []
        for c in _order(self.h.chores):
            best: tuple[float, _Placed] | None = None
            blockers: Counter[str] = Counter()
            for s in range(len(self.slots)):
                cand, why = self._feasible(c, s)
                if cand is None:
                    if why:
                        blockers[why] += 1
                    continue
                score = self._score(c, cand)
                if best is None or score > best[0] + 1e-9:
                    best = (score, cand)
            if best is None:
                reason = self._unscheduled_reason(c, blockers)
                unscheduled.append(Unscheduled(chore_id=c.id, chore_name=c.name, reason=reason))
            else:
                self.placed.append(best[1])
                self._retravel()
        return self._plan(unscheduled)

    def _retravel(self) -> None:
        """Recompute travel for every chore from the room the robot is actually in."""
        room = self.h.robot.dock_room
        for p in sorted(self.placed, key=lambda p: p.start):
            p.travel = self.travel_slots if p.chore.room != room else 0
            room = p.chore.room

    def _unscheduled_reason(self, c: Chore, blockers: Counter[str]) -> str:
        dep = [b for b in blockers if b.startswith("waits on")]
        if dep:
            return dep[0]
        window = [b for b in blockers if b != "outside its time window"]
        if not window:
            return "no time between earliest start and deadline"
        top = ", ".join(f"{b}" for b, _ in Counter({b: blockers[b] for b in window}).most_common(2))
        return f"no feasible slot between {c.earliest:%H:%M} and {c.deadline:%H:%M}: {top}"

    def _plan(self, unscheduled: list[Unscheduled]) -> Plan:
        _, battery = self._battery_ok(None)
        assignments = []
        for p in sorted(self.placed, key=lambda p: p.start):
            assignments.append(
                Assignment(
                    chore_id=p.chore.id,
                    chore_name=p.chore.name,
                    room=p.chore.room,
                    start=self.slots[p.start].start,
                    end=self.slots[p.start].start + timedelta(minutes=(p.end - p.start) * SLOT_MIN),
                    travel_min=p.travel * SLOT_MIN,
                    battery_after_min=round(battery.get(p.chore.id, 0.0), 1),
                    reasons=self._reasons(p.chore, p),
                )
            )
        return Plan(
            household=self.h.name,
            day=self.h.day,
            assignments=assignments,
            unscheduled=unscheduled,
            presence=[(s.start, sorted(s.home)) for s in self.slots],
        )


def schedule(h: Household) -> Plan:
    return Scheduler(h).run()
