"""Tests for the orchestrator on the seed-42 replay with a fake, latency-controlled model client and no network."""

import asyncio
import json
import logging
import re
import threading
import time
from types import SimpleNamespace

import pytest

from deepkick.agents.narrative import NarrativeAgent
from deepkick.agents.persona import PersonaAgent
from deepkick.contracts import validate_overlay
from deepkick.agents.narrative import _leaves
from deepkick.moments import PRIORITY
from deepkick.orchestrator import MODEL_MIN_PRIORITY, PROTECTED, WORKERS, Orchestrator, cap_minute, replay_events
from tests.conftest import REPLAY_PATH, rate_limit_error, timeout_error


class SlowClient:
    """Fake chat client: waits `delay(clock)` seconds, then raises `fail(clock)` or replies with unusable text.

    Unusable replies send every moment to its template, so overlays are deterministic.
    """

    def __init__(self, delay=lambda clock: 0.0, fail=lambda clock: None):
        self.delay, self.fail, self.finished, self.lock = delay, fail, [], threading.Lock()
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        clock = re.search(r'"match_clock": "(\d+:\d+)"', kwargs["messages"][1]["content"]).group(1)
        time.sleep(self.delay(clock))
        with self.lock:
            self.finished.append((time.monotonic(), clock))
        error = self.fail(clock)
        if error:
            raise error
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="not json"))])


# The seed-42 replay: events, moments (all released: the cap drops none), moments of priority 2+ that call the
# model, low-priority moments that use templates, and goals.
EVENTS, MOMENTS, MODEL, LOW, GOALS = 1497, 44, 22, 22, 8


def _run(client=None, **kwargs):
    client = client or SlowClient()
    out = []
    orchestrator = Orchestrator(NarrativeAgent(client, "m"), PersonaAgent(client, "m"), emit=out.append, **kwargs)
    summary = asyncio.run(orchestrator.run(replay_events(REPLAY_PATH)))
    return orchestrator, out, summary


@pytest.fixture(scope="module")
def baseline():
    return _run()


def _minute(overlay):
    return overlay["show_at_ms"] // 60000


def test_every_moment_becomes_a_valid_overlay_in_clock_order(baseline):
    orchestrator, out, summary = baseline
    assert summary["events"] == EVENTS and summary["moments"] == MOMENTS == summary["emitted"] == len(out)
    model = sum(m["priority"] >= MODEL_MIN_PRIORITY for m in _seed42_moments())
    assert (summary["templated"], summary["model_explained"]) == (MOMENTS - model, 0)  # low priority; no usable reply
    assert summary["dropped"] == 0 and summary["fallbacks"] == model == MODEL and summary["rate_limits"] == 0
    for overlay in out:
        validate_overlay(overlay)
    assert [o["show_at_ms"] for o in out] == sorted(o["show_at_ms"] for o in out)
    assert orchestrator.history() == out
    assert orchestrator.snapshot()["status"] == "full_time"


def test_concurrent_work_finishing_out_of_order_is_released_in_order(baseline):
    # Moments in even minutes are slow, so later moments often finish first.
    client = SlowClient(delay=lambda clock: 0.03 if int(clock[:2]) % 2 == 0 else 0.0)
    _, out, _ = _run(client)
    finished = [c for _, c in client.finished]
    assert finished != sorted(finished, key=lambda c: tuple(map(int, c.split(":"))))  # really out of order
    assert out == baseline[1]


def test_one_moments_timeout_does_not_stall_the_others(baseline):
    slow = "13:52"  # the first goal
    client = SlowClient(delay=lambda clock: 0.4 if clock == slow else 0.0,
                        fail=lambda clock: timeout_error() if clock == slow else None)
    start = time.monotonic()
    _, out, summary = _run(client)
    elapsed = time.monotonic() - start
    slow_done = max(t for t, c in client.finished if c == slow)
    others_meanwhile = {c for t, c in client.finished if c != slow and t < slow_done}
    assert len(others_meanwhile) >= 12  # the other workers kept going while it timed out four times
    assert elapsed < 4 * 0.4 + 1.5  # about one slow moment, not a serial queue behind it
    assert out == baseline[1] and summary["emitted"] == MOMENTS  # the timed-out goal still ships, from templates


def test_cap_keeps_the_highest_priority_and_never_drops_protected_moments():
    def moment(i, kind, priority):
        return {"moment_id": f"m{i}", "type": kind, "priority": priority}
    minute = [moment(1, "distance_milestone", 1), moment(2, "goal", 3), moment(3, "big_chance", 2),
              moment(4, "high_press_win", 1), moment(5, "pressure_spell", 2), moment(6, "momentum_swing", 2)]
    kept, dropped = cap_minute(minute, cap=3)
    assert [m["moment_id"] for m in kept] == ["m2", "m3", "m5"]  # goal, then the two earliest priority-2 moments
    assert {m["moment_id"] for m in dropped} == {"m1", "m4", "m6"}
    protected = [moment(i, kind, 3) for i, kind in enumerate(["goal", "goal", "half_time", "full_time"])]
    assert cap_minute(protected + [moment(9, "big_chance", 2)], cap=3) == (protected, [moment(9, "big_chance", 2)])


@pytest.mark.parametrize("cap", [0, 1])
def test_cap_holds_on_the_replay_and_goals_are_never_dropped(cap, caplog):
    with caplog.at_level(logging.INFO, logger="deepkick.orchestrator"):
        _, out, summary = _run(cap=cap)
    per_minute = {}
    for overlay in out:
        if overlay["type"] not in PROTECTED:
            per_minute[_minute(overlay)] = per_minute.get(_minute(overlay), 0) + 1
    assert all(n <= cap for n in per_minute.values())
    assert sum(o["type"] == "goal" for o in out) == GOALS
    assert {"half_time", "full_time"} <= {o["type"] for o in out}
    assert summary["dropped"] == MOMENTS - summary["emitted"] > 0
    assert caplog.text.count("dropped by the cap") == summary["dropped"]
    assert re.search(r"moment_id=ASH-CAL-42-\d{5}-\w+ priority=\d dropped", caplog.text)


def test_the_same_seed_gives_the_same_overlays_with_any_worker_count(baseline):
    assert _run()[1] == baseline[1]
    assert _run(workers=1)[1] == baseline[1]


def test_each_handoff_and_the_run_summary_are_logged(caplog):
    with caplog.at_level(logging.INFO, logger="deepkick.orchestrator"):
        _run()
    assert re.search(r"stage=orchestrator moment_id=ASH-CAL-42-\d{5}-goal duration_ms=\d+ fallback=yes model=yes",
                     caplog.text)
    assert re.search(r"moment_id=ASH-CAL-42-\d{5}-\w+ duration_ms=\d+ fallback=no model=no", caplog.text)
    assert "stage=orchestrator event_id=" not in caplog.text  # one event can produce two moments
    assert re.search(rf"summary events={EVENTS} moments={MOMENTS} overlays_emitted={MOMENTS} overlays_dropped=0 "
                     rf"model_explained=0 templated={LOW} fallbacks={MODEL} rate_limits=0 wall_s=[\d.]+", caplog.text)


def test_cli_replay_needs_no_azure(monkeypatch, capsys):
    from deepkick.orchestrator import main

    main(["--replay", str(REPLAY_PATH), "--persona", "casual"])
    out, err = capsys.readouterr()
    overlays = [json.loads(line) for line in out.splitlines()]
    assert len(overlays) == MOMENTS and all(o["persona"] == "casual" for o in overlays)
    assert json.loads(err.splitlines()[-1])["summary"]["emitted"] == MOMENTS


def _seed42_moments():
    from deepkick.moments import MomentDetector
    from deepkick.stats import StatsAgent

    stats, detector, out = StatsAgent(), MomentDetector(), []
    for line in REPLAY_PATH.read_text().splitlines():
        event = json.loads(line)
        if stats.update(event):
            out += detector.detect(event, stats.snapshot())
    return out


class RateLimitedClient:
    """Fake chat client: the first call for each prompt gets a 429, the retry gets a valid model reply."""

    def __init__(self):
        self.seen, self.types, self.lock = set(), [], threading.Lock()
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        content = kwargs["messages"][1]["content"]
        inputs = json.loads(content)
        with self.lock:
            self.types.append(inputs["moment"]["type"])
            first = content not in self.seen
            self.seen.add(content)
        if first:
            raise rate_limit_error()
        moment = inputs["moment"]
        if "match_state" in inputs:  # the narrative call
            stat, value = next(_leaves(moment["facts"]))
            reply = {"headline": "Model headline", "why_it_matters": "Model reason.",
                     "evidence": [{"stat": stat, "value": value}]}
        else:  # the persona call
            who = moment["player"]["name"] if moment["player"] else moment["team"] or "The match"
            reply = {p: {"headline": f"Model: {who}", "detail": "The model wrote this detail.",
                         "why": "The model wrote this line."}
                     for p in ("analyst", "casual", "player")}
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(reply)))])


@pytest.fixture
def sleeps(monkeypatch):
    """Record backoff waits instead of sleeping."""
    import deepkick.agents.narrative as narrative

    waits = []
    monkeypatch.setattr(narrative, "_sleep", waits.append)
    return waits


def test_429s_back_off_and_the_overlays_still_ship_model_written(sleeps):
    client = RateLimitedClient()
    _, out, summary = _run(client)
    model = [o for o in out if PRIORITY[o["type"]] >= MODEL_MIN_PRIORITY]
    assert model and all(o["payload"]["headline"].startswith("Model:") for o in model)
    assert summary["model_explained"] == len(model) == MODEL and summary["fallbacks"] == 0
    assert summary["rate_limits"] == len(sleeps) == 2 * len(model)  # one 429 per narrative and per persona call
    assert set(sleeps) == {2.0}


def test_priority_one_moments_never_call_the_model(sleeps):
    client = RateLimitedClient()
    _, out, summary = _run(client)
    assert client.types and all(PRIORITY[kind] >= MODEL_MIN_PRIORITY for kind in client.types)
    low = [o for o in out if PRIORITY[o["type"]] < MODEL_MIN_PRIORITY]
    assert {o["type"] for o in low} == {"sprint_milestone", "speed_milestone", "high_press_win", "distance_milestone",
                                        "pass_milestone"}
    assert summary["templated"] == len(low) == LOW
    assert not any(o["payload"]["headline"].startswith("Model") for o in low)


def test_default_worker_count_is_three():
    client = SlowClient()
    assert Orchestrator(NarrativeAgent(client, "m"), PersonaAgent(client, "m")).workers == WORKERS == 3


def test_replay_is_paced_against_the_match_clock(tmp_path):
    lines = REPLAY_PATH.read_text().splitlines()
    head = [line for line in lines if json.loads(line)["match_clock"] <= "02:00"]
    short = tmp_path / "first-two-minutes.jsonl"
    short.write_text("\n".join(head) + "\n")

    async def drain(speed):
        start = time.monotonic()
        events = [e async for e in replay_events(short, speed)]
        return len(events), time.monotonic() - start

    count, paced = asyncio.run(drain(600))  # 120 match seconds at 600x is 0.2 s
    assert count == len(head) and 0.15 < paced < 1.0
    assert asyncio.run(drain(0))[1] < 0.15  # speed 0 stays unpaced for fast tests
