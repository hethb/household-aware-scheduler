"""Calendar import: iCalendar (.ics) files and, optionally, Google Calendar.

Events are classified from their content:

* a video-conference link or words like "call", "meeting", "sync", "standup"
  while the event has no physical location away from home -> MEETING
* a location at home (configurable keywords) -> FOCUS / MEETING at home
* anything else with a time -> AWAY (the person is out)

All-day events are ignored: they rarely mean someone is out all day.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from icalendar import Calendar

from .model import CalendarEvent, EventKind

CALL_WORDS = re.compile(
    r"\b(call|meeting|sync|standup|stand-up|1:1|interview|zoom|meet|webinar|review)\b", re.IGNORECASE
)
CONF_LINK = re.compile(r"(zoom\.us|meet\.google\.com|teams\.microsoft\.com|webex\.com)", re.IGNORECASE)
HOME_WORDS = re.compile(r"\b(home|wfh|remote)\b", re.IGNORECASE)


def classify(title: str, location: str | None, description: str | None) -> EventKind:
    loc = location or ""
    text = " ".join(x for x in (title, loc, description or "") if x)
    is_call = bool(CONF_LINK.search(text) or CALL_WORDS.search(title))
    physically_elsewhere = bool(loc.strip()) and not CONF_LINK.search(loc) and not HOME_WORDS.search(loc)
    if is_call and not physically_elsewhere:
        return EventKind.MEETING
    if HOME_WORDS.search(loc) or HOME_WORDS.search(title):
        return EventKind.FOCUS
    return EventKind.AWAY


def _local(v, tz: ZoneInfo) -> datetime | None:
    if isinstance(v, datetime):
        return (v.astimezone(tz) if v.tzinfo else v.replace(tzinfo=tz)).replace(tzinfo=None)
    return None  # all-day (date) events are skipped


def load_ics(
    path: str | Path, member: str, day: date, tz: str, work_room: str | None = None
) -> list[CalendarEvent]:
    """Events from an .ics file that overlap `day`, in household local time."""
    zone = ZoneInfo(tz)
    cal = Calendar.from_ical(Path(path).read_bytes())
    lo = datetime.combine(day, time.min)
    hi = datetime.combine(day, time.max)
    out: list[CalendarEvent] = []
    for ev in cal.walk("VEVENT"):
        start = _local(ev.decoded("DTSTART"), zone)
        end = _local(ev.decoded("DTEND"), zone) if ev.get("DTEND") else None
        if start is None or end is None or end <= lo or start >= hi:
            continue
        title = str(ev.get("SUMMARY", "Busy"))
        kind = classify(title, ev.get("LOCATION"), ev.get("DESCRIPTION"))
        room = work_room if kind in (EventKind.MEETING, EventKind.FOCUS) else None
        out.append(
            CalendarEvent(
                member=member, title=title, start=max(start, lo), end=min(end, hi), kind=kind, room=room
            )
        )
    return sorted(out, key=lambda e: e.start)


def load_google(
    calendar_id: str,
    member: str,
    day: date,
    tz: str,
    credentials_file: str = "credentials.json",
    work_room: str | None = None,
) -> list[CalendarEvent]:  # pragma: no cover - needs network and OAuth
    """Fetch one day of a Google Calendar via the Calendar API (read-only scope).

    Requires `pip install homesched[google]` and an OAuth client file from the
    Google Cloud console. A token is cached in token.json after the first run.
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    scopes = ["https://www.googleapis.com/auth/calendar.readonly"]
    token = Path("token.json")
    creds = Credentials.from_authorized_user_file(str(token), scopes) if token.exists() else None
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            creds = InstalledAppFlow.from_client_secrets_file(credentials_file, scopes).run_local_server(
                port=0
            )
        token.write_text(creds.to_json())

    zone = ZoneInfo(tz)
    lo = datetime.combine(day, time.min, tzinfo=zone)
    hi = datetime.combine(day, time.max, tzinfo=zone)
    service = build("calendar", "v3", credentials=creds)
    items = (
        service.events()
        .list(
            calendarId=calendar_id,
            timeMin=lo.isoformat(),
            timeMax=hi.isoformat(),
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
        .get("items", [])
    )
    out: list[CalendarEvent] = []
    for it in items:
        s, e = it["start"].get("dateTime"), it["end"].get("dateTime")
        if not s or not e:
            continue
        start = datetime.fromisoformat(s).astimezone(zone).replace(tzinfo=None)
        end = datetime.fromisoformat(e).astimezone(zone).replace(tzinfo=None)
        kind = classify(
            it.get("summary", "Busy"), it.get("location"), it.get("description") or it.get("hangoutLink")
        )
        room = work_room if kind in (EventKind.MEETING, EventKind.FOCUS) else None
        out.append(
            CalendarEvent(
                member=member, title=it.get("summary", "Busy"), start=start, end=end, kind=kind, room=room
            )
        )
    return out
