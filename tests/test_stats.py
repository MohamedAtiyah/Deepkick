"""Tests for the Stats Agent, checked against independent counts over the seed-42 replay."""

import copy
import json
import logging

import pytest

from deepkick.stats import StatsAgent


@pytest.fixture(scope="module")
def final(replay):
    agent = StatsAgent()
    for event in replay:
        assert agent.update(event)
    return agent.snapshot()


def _count(replay, event_type, team, test=lambda p: True):
    return sum(1 for e in replay if e["event_type"] == event_type and e["team"] == team and test(e["payload"]))


def test_final_state_matches_last_event(replay, final):
    assert final["score"] == replay[-1]["context"]["score"]
    assert final["status"] == "full_time"
    assert final["match_clock"] == "90:00"
    assert final["last_event_id"] == replay[-1]["event_id"]
    assert final["events_processed"] == len(replay)
    assert final["teams"]["ASH"]["name"] == "Ashvale Wanderers"


@pytest.mark.parametrize("team", ["ASH", "CAL"])
def test_team_totals_match_replay(replay, final, team):
    t = final["teams"][team]
    assert t["goals"] == final["score"][team]
    assert t["shots"] == _count(replay, "shot", team)
    assert t["shots_on_target"] == _count(replay, "shot", team, lambda p: p["outcome"] in ("goal", "saved"))
    assert t["xg"] == round(sum(e["payload"]["xg"] for e in replay
                                if e["event_type"] == "shot" and e["team"] == team), 2)
    assert t["passes"] == _count(replay, "pass", team)
    assert t["passes_completed"] == _count(replay, "pass", team, lambda p: p["completed"])
    assert t["pass_accuracy_pct"] == round(100 * t["passes_completed"] / t["passes"], 1)
    assert t["tackles_won"] == _count(replay, "tackle", team, lambda p: p["won"])
    assert t["pressures_won"] == _count(replay, "pressure_event", team, lambda p: p["success"])


def test_possession_covers_the_whole_match(final):
    teams = final["teams"].values()
    assert sum(t["possession_seconds"] for t in teams) == 90 * 60
    assert sum(t["possession_pct"] for t in teams) == pytest.approx(100, abs=0.1)


def test_player_totals_add_up_to_team_totals(final):
    for team_id, team in final["teams"].items():
        players = [p for p in final["players"].values() if p["team"] == team_id]
        assert len(players) <= 11
        for field in ("passes", "passes_completed", "shots", "goals", "tackles", "pressures"):
            assert sum(p[field] for p in players) == team[field], field


def test_snapshot_is_json_serialisable(final):
    assert json.loads(json.dumps(final)) == final


def test_snapshot_before_any_event_is_empty():
    snap = StatsAgent().snapshot()
    assert snap["status"] == "not_started" and snap["teams"] == {} and snap["events_processed"] == 0


def test_duplicate_event_is_counted_once(replay):
    agent = StatsAgent()
    for event in replay[:200]:
        agent.update(event)
    before = agent.snapshot()
    assert agent.update(replay[150]) is False
    assert agent.snapshot() == before


def test_malformed_event_is_skipped_and_logged(replay, caplog):
    agent = StatsAgent()
    for event in replay[:50]:
        agent.update(event)
    before = agent.snapshot()
    bad = copy.deepcopy(replay[50])
    del bad["context"]
    with caplog.at_level(logging.INFO, logger="deepkick.stats"):
        assert agent.update(bad) is False
        assert agent.update("not an event") is False
    assert agent.snapshot() == before
    assert f"stage=stats event_id={bad['event_id']}" in caplog.text
    assert "fallback=yes" in caplog.text


def test_each_handoff_is_logged(replay, caplog):
    with caplog.at_level(logging.INFO, logger="deepkick.stats"):
        StatsAgent().update(replay[0])
    assert f"stage=stats event_id={replay[0]['event_id']} duration_ms=" in caplog.text
    assert "fallback=no" in caplog.text


def test_cli_prints_final_snapshot(final, capsys):
    from deepkick.stats import main
    from tests.conftest import REPLAY_PATH

    main([str(REPLAY_PATH)])
    assert json.loads(capsys.readouterr().out) == final
