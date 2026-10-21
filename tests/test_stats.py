"""Tests for the Stats Agent, checked against independent counts over the seed-42 replay."""

import copy
import json
import logging

import pytest

from deepkick.simulator import clock_seconds
from deepkick.stats import HARD_PASS, MOMENTUM_WEIGHTS, MOMENTUM_WINDOW_S, StatsAgent


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


@pytest.mark.parametrize("team", ["ASH", "CAL"])
def test_pass_length_difficulty_and_hard_passes_match_replay(replay, final, team):
    passes = [e["payload"] for e in replay if e["event_type"] == "pass" and e["team"] == team]
    hard = [p for p in passes if p["difficulty"] >= HARD_PASS]
    t = final["teams"][team]
    assert t["avg_pass_length_m"] == round(sum(p["length_m"] for p in passes) / len(passes), 1)
    assert t["avg_pass_difficulty"] == round(sum(p["difficulty"] for p in passes) / len(passes), 2)
    assert (t["hard_passes"], t["hard_passes_completed"]) == (len(hard), sum(p["completed"] for p in hard))
    assert t["hard_pass_completion_pct"] == round(100 * t["hard_passes_completed"] / t["hard_passes"], 1)
    assert t["hard_pass_completion_pct"] < t["pass_accuracy_pct"]
    assert not any(k.endswith("_total") or k.endswith("_total_m") for k in t)


def test_top_shot_speed_per_team_and_player(replay, final):
    for team_id, team in final["teams"].items():
        assert team["top_shot_speed_kmh"] == max(e["payload"]["speed_kmh"] for e in replay
                                                 if e["event_type"] == "shot" and e["team"] == team_id)
    for pid, player in final["players"].items():
        speeds = [e["payload"]["speed_kmh"] for e in replay
                  if e["event_type"] == "shot" and e["player"]["id"] == pid]
        assert player["top_shot_speed_kmh"] == max(speeds, default=0.0)


def test_pressure_index_and_possession_come_from_latest_event(replay):
    agent = StatsAgent()
    for event in replay[:400]:
        agent.update(event)
        snap = agent.snapshot()
        assert snap["pressure_index"] == event["context"]["pressure_index"]
        assert snap["possession_team"] == event["context"]["possession_team"]


def _window_momentum(events, now):
    """Independent momentum count over the events seen so far, for the window ending at `now` seconds."""
    window = {t: dict.fromkeys(MOMENTUM_WEIGHTS, 0.0) for t in ("ASH", "CAL")}
    for prev, e in zip(events, events[1:]):
        clock = clock_seconds(e)
        if clock < now - MOMENTUM_WINDOW_S:
            continue
        window[prev["context"]["possession_team"]]["possession"] += clock - clock_seconds(prev)
        p = e["payload"]
        if e["event_type"] == "shot":
            window[e["team"]]["shots"] += 1
            window[e["team"]]["xg"] += p["xg"]
        moved = (e["event_type"] == "pass" and p["completed"]) or (e["event_type"] == "player_run" and p["with_ball"])
        if moved and e["coordinates"]["x"] < 70 <= p["end"]["x"]:
            window[e["team"]]["final_third_entries"] += 1
    return window


@pytest.mark.parametrize("upto", [30, 200, 500, 900, 1200, -1])
def test_momentum_counts_the_last_five_minutes(replay, upto):
    events = replay[:upto] if upto > 0 else replay
    agent = StatsAgent()
    for event in events:
        agent.update(event)
    snap = agent.snapshot()
    window = _window_momentum(events, clock_seconds(events[-1]))
    held = sum(w["possession"] for w in window.values())
    for team_id, w in window.items():
        m = snap["teams"][team_id]["momentum"]
        assert m["shots"] == w["shots"] and m["final_third_entries"] == w["final_third_entries"]
        assert m["xg"] == round(w["xg"], 2)
        assert m["possession_pct"] == round(100 * w["possession"] / held, 1)
    values = [t["momentum"]["value"] for t in snap["teams"].values()]
    assert sum(values) == pytest.approx(100, abs=0.1)
    assert snap["momentum_window_minutes"] == MOMENTUM_WINDOW_S // 60


def test_momentum_favours_the_team_doing_more(replay):
    agent = StatsAgent()
    for event in replay:
        agent.update(event)
        snap = agent.snapshot()
        a, b = (snap["teams"][t]["momentum"] for t in ("ASH", "CAL"))
        if all(a[k] >= b[k] for k in ("shots", "xg", "possession_pct", "final_third_entries")) and a != b:
            assert a["value"] > b["value"]


def test_top_run_speed_per_team_and_player(replay, final):
    for team_id, team in final["teams"].items():
        assert team["top_run_speed_kmh"] == max(e["payload"]["speed_kmh"] for e in replay
                                                if e["event_type"] == "player_run" and e["team"] == team_id)
    for pid, player in final["players"].items():
        speeds = [e["payload"]["speed_kmh"] for e in replay
                  if e["event_type"] == "player_run" and e["player"]["id"] == pid]
        assert player["top_run_speed_kmh"] == max(speeds, default=0.0)
