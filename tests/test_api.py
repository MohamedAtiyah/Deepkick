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

TOTAL = 44  # overlays from the seed-42 replay; none are dropped by the cap


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


def _settled_backlog(client, persona="analyst"):
    """The released overlays, oldest first, once they stop growing (the stream is paused and the queue drained)."""
    orchestrator, previous = client.app.state.orchestrator, None
    while True:
        current = orchestrator.history(orchestrator.profile | {"persona": persona})
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
    assert state["score"] == {"ASH": 3, "CAL": 5}
    assert {"goals", "shots", "xg", "possession_pct", "momentum"} <= state["teams"]["ASH"].keys()


def test_recap_returns_the_half_and_full_time_recaps_newest_first_per_persona(finished):
    recap = finished.get("/recap").json()  # the run's persona, analyst
    assert [r["type"] for r in recap] == ["full_time", "half_time"]
    assert [r["match_clock"] for r in recap] == ["90:00", "45:00"] and recap[0]["score"] == {"ASH": 3, "CAL": 5}
    assert {r["persona"] for r in recap} == {"analyst"} and {r["lang"] for r in recap} == {"en"}
    assert all(r["source"] == "template" and r["text"] for r in recap)  # no Foundry settings in tests
    casual = finished.get("/recap", params={"persona": "casual"}).json()
    assert {r["persona"] for r in casual} == {"casual"} and casual[0]["text"] != recap[0]["text"]
    assert finished.get("/recap", params={"persona": "coach"}).status_code == 422


def test_healthz_reports_stages_and_counters(finished):
    health = finished.get("/healthz").json()
    assert health["status"] == "finished"
    assert health["stages"] == {"ingest": "injected", "interpret": "in_process", "explain": "foundry",
                                "personalise": "foundry"}
    assert {"events": 1497, "moments": TOTAL, "emitted": TOTAL, "dropped": 0}.items() <= health["summary"].items()


def test_websocket_sends_the_backlog_then_live_overlays_per_viewer():
    gate = threading.Event()
    with TestClient(create_app(_orchestrator(), _paused_at_half_time(gate))) as client:
        backlog = _settled_backlog(client)  # oldest first, as the socket sends it
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
    from tests.test_translate import ARABIC, as_arabic, fake_translator

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
        assert a["lang"] == "ar" and a["payload"]["headline"] == as_arabic(e["payload"]["headline"], translator)
        assert a["payload"]["player"] == e["payload"]["player"]  # the data field keeps the league's spelling
        # Evidence labels are translated with the text; values stay exactly the English (fact) values.
        assert [x["label"] for x in a["payload"]["evidence"]] == [as_arabic(x["label"], translator)
                                                                  for x in e["payload"]["evidence"]]
        assert [x["value"] for x in a["payload"]["evidence"]] == [x["value"] for x in e["payload"]["evidence"]]
        assert a["payload"]["source"] == e["payload"]["source"] == "template"  # the model client always fails here
    assert [r["lang"] for r in recap] == ["ar", "ar"] and all(r["text"].startswith(ARABIC) for r in recap)
    assert len(requests) <= TOTAL + 2  # one batched call per moment and per recap, fewer once strings repeat
