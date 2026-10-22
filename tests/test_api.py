"""Tests for the overlay API with FastAPI's TestClient, the seed-42 replay and a fake model client (no network)."""

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from deepkick.agents.narrative import NarrativeAgent
from deepkick.agents.persona import PersonaAgent
from deepkick.api import create_app
from deepkick.contracts import validate_overlay
from deepkick.orchestrator import Orchestrator, replay_events
from deepkick.simulator import clock_seconds
from tests.conftest import REPLAY_PATH

TOTAL = 35  # overlays from the seed-42 replay; none are dropped by the cap


def _failing_client():
    """A model client whose every call fails, so every overlay comes from the templates and is deterministic."""
    def create(**kwargs):
        raise ConnectionError("no network in tests")
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def _orchestrator():
    client = _failing_client()
    return Orchestrator(NarrativeAgent(client, "m"), PersonaAgent(client, "m"))


async def _paused_at_half_time(gate: threading.Event):
    """The replay, holding before the first second-half event until the test opens the gate."""
    async for event in replay_events(REPLAY_PATH):
        while clock_seconds(event) >= 45 * 60 and not gate.is_set():
            await asyncio.sleep(0.01)
        yield event


def _wait_until(check, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.02)


def _settled_recap(client, persona="analyst"):
    """The recap once it has stopped growing (the stream is paused and the queue has drained)."""
    previous = None
    while True:
        current = client.get("/recap", params={"persona": persona}).json()
        if current and current == previous:
            return current
        previous = current
        time.sleep(0.2)


@pytest.fixture
def finished():
    """A client whose orchestrator has played the whole replay."""
    with TestClient(create_app(_orchestrator(), replay_events(REPLAY_PATH))) as client:
        _wait_until(lambda: client.get("/healthz").json()["status"] == "finished")
        yield client


def test_state_is_the_match_snapshot(finished):
    state = finished.get("/state").json()
    assert state["status"] == "full_time" and state["match_clock"] == "90:00"
    assert state["score"] == {"ASH": 2, "CAL": 3}
    assert {"goals", "shots", "xg", "possession_pct", "momentum"} <= state["teams"]["ASH"].keys()


def test_recap_is_newest_first_with_persona_and_limit(finished):
    recap = finished.get("/recap").json()
    assert len(recap) == TOTAL
    assert [o["show_at_ms"] for o in recap] == sorted((o["show_at_ms"] for o in recap), reverse=True)
    for overlay in recap:
        validate_overlay(overlay)
    casual = finished.get("/recap", params={"persona": "casual", "limit": 5}).json()
    assert len(casual) == 5 and {o["persona"] for o in casual} == {"casual"}
    assert [o["overlay_id"].rsplit("-", 2)[0] for o in casual] == [o["overlay_id"].rsplit("-", 2)[0]
                                                                  for o in recap[:5]]
    assert finished.get("/recap", params={"persona": "coach"}).status_code == 422
    assert finished.get("/recap", params={"limit": 0}).status_code == 422


def test_healthz_reports_stages_and_counters(finished):
    health = finished.get("/healthz").json()
    assert health["status"] == "finished"
    assert health["stages"] == {"ingest": "injected", "interpret": "in_process", "explain": "foundry",
                                "personalise": "foundry"}
    assert {"events": 1485, "moments": TOTAL, "emitted": TOTAL, "dropped": 0}.items() <= health["summary"].items()


def test_websocket_sends_the_backlog_then_live_overlays_per_viewer():
    gate = threading.Event()
    with TestClient(create_app(_orchestrator(), _paused_at_half_time(gate))) as client:
        backlog = _settled_recap(client)[::-1]  # oldest first, as the socket sends it
        assert 0 < len(backlog) < TOTAL
        with client.websocket_connect("/ws?persona=analyst") as analyst, \
                client.websocket_connect("/ws?persona=casual&club=ASH&player=ASH09") as casual:
            first = [analyst.receive_json() for _ in backlog]
            assert first == backlog  # a late viewer is not blank
            gate.set()
            analyst_all = first + [analyst.receive_json() for _ in range(TOTAL - len(backlog))]
            casual_all = [casual.receive_json() for _ in range(TOTAL)]
    for overlay in analyst_all + casual_all:
        validate_overlay(overlay)
    assert [o["show_at_ms"] for o in analyst_all] == sorted(o["show_at_ms"] for o in analyst_all)
    assert {o["persona"] for o in analyst_all} == {"analyst"} and {o["persona"] for o in casual_all} == {"casual"}
    # The same moments, each in the viewer's own words and with their favourite-club boost.
    assert [o["type"] for o in analyst_all] == [o["type"] for o in casual_all]
    differing = [a for a, c in zip(analyst_all, casual_all) if a["payload"] != c["payload"]]
    assert len(differing) > TOTAL / 2
    boosted = [c for a, c in zip(analyst_all, casual_all) if c["priority"] > a["priority"]]
    assert boosted and all(c["type"] != "goal" or c["priority"] == 5 for c in casual_all)


def test_websocket_rejects_an_unknown_persona_or_language(finished):
    for query in ("persona=coach", "lang=fr"):
        with pytest.raises(WebSocketDisconnect):
            with finished.websocket_connect(f"/ws?{query}") as ws:
                ws.receive_json()


def test_default_app_replays_REPLAY_FILE_with_no_azure(monkeypatch):
    import dotenv

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)  # never read a developer's real .env here
    for key in ("FOUNDRY_ENDPOINT", "FOUNDRY_MODEL_DEPLOYMENT", "FOUNDRY_API_KEY", "EVENTHUB_CONNECTION_STRING"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("REPLAY_FILE", str(REPLAY_PATH))
    monkeypatch.setenv("REPLAY_SPEED", "0")  # unpaced, so the test is fast
    with TestClient(create_app()) as client:
        _wait_until(lambda: client.get("/healthz").json()["status"] == "finished")
        health = client.get("/healthz").json()
    assert health["stages"] == {"ingest": "replay", "interpret": "in_process", "explain": "templates",
                                "personalise": "templates"}
    assert health["summary"]["emitted"] == TOTAL


def test_default_app_paces_the_replay_so_state_tracks_the_stream(monkeypatch):
    import dotenv

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)
    for key in ("FOUNDRY_ENDPOINT", "FOUNDRY_MODEL_DEPLOYMENT", "FOUNDRY_API_KEY", "REPLAY_SPEED"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("REPLAY_FILE", str(REPLAY_PATH))
    with TestClient(create_app()) as client:
        time.sleep(1.0)  # 30 match seconds at the default 30x
        state, health = client.get("/state").json(), client.get("/healthz").json()
    assert health["status"] == "running"
    assert state["status"] == "kickoff" and "00:20" <= state["match_clock"] <= "01:00"  # not 90:00 and full time


def test_arabic_overlays_over_ws_and_recap_with_a_mocked_translator():
    from tests.test_translate import ARABIC, fake_translator

    translator, requests = fake_translator()
    client = _failing_client()
    orchestrator = Orchestrator(NarrativeAgent(client, "m"), PersonaAgent(client, "m"), translator=translator)
    with TestClient(create_app(orchestrator, replay_events(REPLAY_PATH))) as api:
        _wait_until(lambda: api.get("/healthz").json()["status"] == "finished")
        with api.websocket_connect("/ws?persona=casual&lang=ar") as arabic, \
                api.websocket_connect("/ws?persona=casual&lang=en") as english:
            ar = [arabic.receive_json() for _ in range(TOTAL)]
            en = [english.receive_json() for _ in range(TOTAL)]
        recap = api.get("/recap", params={"persona": "casual", "lang": "ar"}).json()
        assert api.get("/recap", params={"lang": "es"}).status_code == 422
        with pytest.raises(WebSocketDisconnect):
            with api.websocket_connect("/ws?lang=es") as ws:
                ws.receive_json()
    for a, e in zip(ar, en):
        validate_overlay(a)
        assert a["lang"] == "ar" and a["payload"]["headline"] == ARABIC + e["payload"]["headline"]
        assert a["payload"]["player"] == e["payload"]["player"]  # names stay as they are
    assert recap[0] == ar[-1]
    assert len(requests) <= TOTAL  # at most one batched call per moment, fewer once strings repeat
