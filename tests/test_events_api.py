"""Tests for GET /events, the event feed the pitch animates (seed-42 replay, no network)."""

from fastapi.testclient import TestClient

from deepkick.api import create_app
from deepkick.orchestrator import replay_events
from deepkick.simulator import clock_seconds
from tests.conftest import REPLAY_PATH
from tests.test_api import _orchestrator, _wait_until


async def _until(minute):
    async for event in replay_events(REPLAY_PATH):
        if clock_seconds(event) >= minute * 60:
            return
        yield event


def test_events_page_in_order_from_an_index_and_stop_at_the_clock():
    with TestClient(create_app(_orchestrator(), _until(20))) as client:
        _wait_until(lambda: client.get("/healthz").json()["status"] == "finished")
        late = client.get("/events").json()
        assert late["home"] == "ASH" and len(late["events"]) == 30 and late["next"] > 30  # a late joiner's tail
        first = client.get("/events", params={"after": 0, "limit": 5}).json()
        assert first["next"] == 5 and first["events"][0]["event_type"] == "match_metadata"
        assert "lineups" not in first["events"][0]["payload"] and "context" not in first["events"][1]
        rest = client.get("/events", params={"after": 5, "limit": 1000}).json()
        assert rest["next"] == late["next"] and all(clock_seconds(e) < 20 * 60 for e in rest["events"])
        ids = [e["event_id"] for e in first["events"] + rest["events"]]
        assert ids == sorted(ids) and len(ids) == len(set(ids))
        assert client.get("/events", params={"after": 10**6}).json()["events"] == []
        assert client.get("/events", params={"after": -1}).status_code == 422
