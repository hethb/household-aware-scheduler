"""Small web view: the day's timeline, and live replanning when the calendar changes."""

from __future__ import annotations

from importlib.resources import files

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ValidationError

from .diff import diff
from .model import CalendarEvent, Household
from .occupancy import build_slots
from .scheduler import schedule


class EventIn(BaseModel):
    event: CalendarEvent


def _view(h: Household) -> dict:
    plan = schedule(h)
    slots = build_slots(h)
    return {
        "household": h.model_dump(mode="json"),
        "plan": plan.model_dump(mode="json"),
        "slots": [
            {
                "start": s.start.isoformat(),
                "home": sorted(s.home),
                "on_call": sorted(s.on_call),
                "quiet": s.quiet_hours,
                "rooms": {k: sorted(v) for k, v in s.rooms.items()},
            }
            for s in slots
        ],
    }


def create_app(base: Household) -> FastAPI:
    app = FastAPI(title="homesched")
    state: dict = {"h": base.model_copy(deep=True), "changes": None}

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (files("homesched") / "static" / "index.html").read_text()

    @app.get("/api/plan")
    def get_plan() -> dict:
        return {**_view(state["h"]), "changes": state["changes"]}

    @app.post("/api/events")
    def add_event(body: EventIn) -> dict:
        old_h = state["h"]
        try:
            new_h = Household.model_validate(
                {**old_h.model_dump(), "events": [*old_h.model_dump()["events"], body.event.model_dump()]}
            )
        except ValidationError as e:
            raise HTTPException(422, str(e)) from e
        changes = diff(old_h, schedule(old_h), new_h, schedule(new_h))
        state["h"] = new_h
        state["changes"] = [c.describe() for c in changes]
        return {**_view(new_h), "changes": state["changes"]}

    @app.post("/api/reset")
    def reset() -> dict:
        state["h"] = base.model_copy(deep=True)
        state["changes"] = None
        return {**_view(state["h"]), "changes": None}

    return app
