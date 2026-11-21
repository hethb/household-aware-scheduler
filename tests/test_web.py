from pathlib import Path

from fastapi.testclient import TestClient

from homesched import Household
from homesched.web import create_app

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "household.json"


def client() -> TestClient:
    return TestClient(create_app(Household.model_validate_json(EXAMPLE.read_text())))


def test_index_and_plan():
    c = client()
    assert "timeline" in c.get("/").text
    body = c.get("/api/plan").json()
    assert body["plan"]["assignments"]
    assert len(body["slots"]) == (23 - 6) * 12


def test_add_event_returns_changes_and_reset():
    c = client()
    ev = {
        "member": "Priya",
        "kind": "away",
        "title": "Errand",
        "start": "2026-11-23T07:00",
        "end": "2026-11-23T10:00",
    }
    body = c.post("/api/events", json={"event": ev}).json()
    assert "changes" in body
    assert c.post("/api/reset").status_code == 200


def test_bad_event_rejected():
    c = client()
    ev = {
        "member": "Nobody",
        "kind": "away",
        "title": "x",
        "start": "2026-11-23T07:00",
        "end": "2026-11-23T08:00",
    }
    assert c.post("/api/events", json={"event": ev}).status_code == 422
