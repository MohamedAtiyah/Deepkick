"""Tests for the per-minute momentum series and GET /timeline on the seed-42 replay (no model calls)."""

import threading
import time

from fastapi.testclient import TestClient

from deepkick.api import create_app
from deepkick.simulator import clock_seconds
from deepkick.stats import StatsAgent
from tests.test_api import _orchestrator, _paused_at_half_time, _wait_until


def _series(replay):
    agent = StatsAgent()
    for event in replay:
        agent.update(event)
    return agent.momentum_series


def test_series_length_equals_completed_minutes_after_every_event(replay):
    agent = StatsAgent()
    for event in replay:
        agent.update(event)
        assert len(agent.momentum_series) == clock_seconds(event) // 60, event["match_clock"]
    assert [e["minute"] for e in agent.momentum_series] == list(range(1, 91))
    assert all(-100 <= e["momentum"] <= 100 for e in agent.momentum_series)


def test_each_minute_is_home_minus_away_as_it_stood_at_the_end_of_that_minute(replay):
    agent, expected = StatsAgent(), {}
    for event in replay:
        minute = clock_seconds(event) // 60
        if len(agent.score) == 2 and minute > len(expected):  # the state before this event ends those minutes
            values = agent.momentum()
            home, away = agent.score
            for m in range(len(expected) + 1, minute + 1):
                expected[m] = round(values[home]["value"] - values[away]["value"], 1)
        agent.update(event)
    assert {e["minute"]: e["momentum"] for e in agent.momentum_series} == expected


def test_the_seed_42_fixture_gives_an_identical_series_on_two_runs(replay):
    first, second = _series(replay), _series(replay)
    assert first == second and len(first) == 90


def _settled(client, **params):
    """/timeline once two reads a moment apart agree (the stream is paused and the queue has drained)."""
    previous, deadline = None, time.monotonic() + 10
    while True:
        assert time.monotonic() < deadline, "timed out"
        current = client.get("/timeline", params=params).json()
        if current == previous and current["moments"]:
            return current
        previous = current
        time.sleep(0.2)


def test_timeline_has_no_entries_past_the_clock():
    gate = threading.Event()  # the stream holds before the first event at 45:00 until the test opens it
    with TestClient(create_app(_orchestrator(), _paused_at_half_time(gate))) as client:
        timeline = _settled(client, persona="analyst")
        now = clock_seconds({"match_clock": timeline["clock"]})
        assert 44 * 60 <= now < 45 * 60
        assert [m["minute"] for m in timeline["minutes"]] == list(range(1, now // 60 + 1))  # completed minutes only
        assert all(clock_seconds(m) <= now for m in timeline["moments"])
        assert all(m["overlay_id"].endswith("-analyst-en") for m in timeline["moments"])
        goal = next(m for m in timeline["moments"] if m["type"] == "goal")
        assert goal["priority"] == 5 and goal["coordinates"] and goal["team"] in ("ASH", "CAL")
        assert next(m for m in timeline["moments"] if m["type"] == "momentum_swing")["coordinates"] is None
        assert client.get("/timeline", params={"persona": "coach"}).status_code == 422
        gate.set()
        _wait_until(lambda: client.get("/healthz").json()["status"] == "finished")
        full = client.get("/timeline").json()
        assert len(full["minutes"]) == 90 and full["moments"][-1]["type"] == "full_time"
        assert full["minutes"][:44] == timeline["minutes"]  # the first half never changes afterwards
