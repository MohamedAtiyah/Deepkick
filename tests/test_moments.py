"""Tests for the moment detector, checked against independent scans of the seed-42 replay."""

import json
import logging

import pytest

from deepkick.moments import (BIG_CHANCE_XG, HIGH_PRESS_X, MOMENT_FIELDS, SPELL_COOLDOWN_S, SPELL_SHOTS,
                              SPELL_WINDOW_S, MomentDetector, goal_kind)
from deepkick.simulator import clock_seconds
from deepkick.stats import StatsAgent


def _run(events):
    """Feed events through Stats Agent and detector; return (moments, snapshot after each moment's event)."""
    stats, detector, out = StatsAgent(), MomentDetector(), []
    for event in events:
        if stats.update(event):
            snap = stats.snapshot()
            out += [(m, snap) for m in detector.detect(event, snap)]
    return out


@pytest.fixture(scope="module")
def detected(replay):
    return _run(replay)


@pytest.fixture(scope="module")
def moments(detected):
    return [m for m, _ in detected]


def _of(moments, kind):
    return [m for m in moments if m["type"] == kind]


@pytest.mark.parametrize("goals_for, goals_against, kind", [
    (1, 0, "opener"), (1, 1, "equaliser"), (2, 2, "equaliser"), (2, 1, "go_ahead"),
    (3, 1, "extends_lead"), (2, 0, "extends_lead"), (1, 3, "pulls_back"),
])
def test_goal_kind(goals_for, goals_against, kind):
    assert goal_kind(goals_for, goals_against) == kind


def test_every_moment_has_the_moment_shape(moments):
    assert moments
    for m in moments:
        assert m.keys() == MOMENT_FIELDS
        assert m["moment_id"] == f"{m['event_id']}-{m['type']}"
    assert len({m["moment_id"] for m in moments}) == len(moments)


def test_one_goal_moment_per_goal_in_order(replay, moments):
    goals = [e for e in replay if e["event_type"] == "shot" and e["payload"]["outcome"] == "goal"]
    assert [m["event_id"] for m in _of(moments, "goal")] == [e["event_id"] for e in goals]
    assert [m["facts"]["kind"] for m in _of(moments, "goal")] == ["opener", "equaliser", "go_ahead"]


def test_big_chances_are_exactly_the_missed_high_xg_shots(replay, moments):
    expected = [e["event_id"] for e in replay if e["event_type"] == "shot"
                and e["payload"]["outcome"] != "goal" and e["payload"]["xg"] >= BIG_CHANCE_XG]
    assert [m["event_id"] for m in _of(moments, "big_chance")] == expected


def test_high_press_wins_are_successful_presses_high_up(replay, moments):
    expected = [e["event_id"] for e in replay if e["event_type"] == "pressure_event"
                and e["payload"]["success"] and e["coordinates"]["x"] >= HIGH_PRESS_X]
    assert [m["event_id"] for m in _of(moments, "high_press_win")] == expected


def test_pressure_spells_have_enough_shots_and_respect_cooldown(replay, moments):
    spells = _of(moments, "pressure_spell")
    assert spells
    shot_clocks = {}
    for e in replay:
        if e["event_type"] == "shot":
            shot_clocks.setdefault(e["team"], []).append(clock_seconds(e))
    last = {}
    for m in spells:
        clock = clock_seconds(m)
        recent = [c for c in shot_clocks[m["team"]] if clock - SPELL_WINDOW_S <= c <= clock]
        assert m["facts"]["shots"] == len(recent) >= SPELL_SHOTS
        assert clock - last.get(m["team"], -SPELL_COOLDOWN_S) >= SPELL_COOLDOWN_S
        last[m["team"]] = clock


def test_half_and_full_time_fire_once(moments):
    assert [m["match_clock"] for m in _of(moments, "half_time")] == ["45:00"]
    assert [m["match_clock"] for m in _of(moments, "full_time")] == ["90:00"]
    assert _of(moments, "full_time")[0]["team"] is None


def test_facts_only_quote_numbers_from_the_event_or_snapshot(replay, detected):
    events = {e["event_id"]: e for e in replay}
    for m, snap in detected:
        facts, event = m["facts"], events[m["event_id"]]
        if m["type"] in ("goal", "big_chance"):
            team = snap["teams"][m["team"]]
            assert (facts["xg"], facts["distance_m"]) == (event["payload"]["xg"], event["payload"]["distance_m"])
            assert (facts["team_shots"], facts["team_xg"]) == (team["shots"], team["xg"])
        if m["type"] == "goal":
            assert facts["score"] == snap["score"] == event["context"]["score"]
            assert facts["player_goals"] == snap["players"][m["player"]["id"]]["goals"]
        if m["type"] in ("half_time", "full_time"):
            assert facts["score"] == snap["score"]
            for tid, t in facts["teams"].items():
                assert t.items() <= snap["teams"][tid].items()


def test_priority_order_within_one_event(moments):
    by_event = {}
    for m in moments:
        by_event.setdefault(m["event_id"], []).append(m["priority"])
    assert all(p == sorted(p, reverse=True) for p in by_event.values())


def test_detection_is_deterministic(replay, moments):
    assert [m for m, _ in _run(replay)] == moments


def test_each_handoff_is_logged(replay, caplog):
    stats = StatsAgent()
    stats.update(replay[0])
    with caplog.at_level(logging.INFO, logger="deepkick.moments"):
        MomentDetector().detect(replay[0], stats.snapshot())
    assert f"stage=moments event_id={replay[0]['event_id']} moments=0 duration_ms=" in caplog.text
    assert "fallback=no" in caplog.text


def test_cli_prints_moments(moments, capsys):
    from deepkick.moments import main
    from tests.conftest import REPLAY_PATH

    main([str(REPLAY_PATH)])
    assert [json.loads(line) for line in capsys.readouterr().out.splitlines()] == moments
