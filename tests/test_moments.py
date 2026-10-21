"""Tests for the moment detector, checked against independent scans of the seed-42 replay."""

import json
import logging

import pytest

from deepkick.moments import (BIG_CHANCE_XG, DISTANCE_STEP_KM, HIGH_PRESS_X, MOMENT_FIELDS, PASS_MILESTONES,
                              SPEED_MILESTONE_KMH, SPELL_COOLDOWN_S, SPELL_SHOTS, SPELL_WINDOW_S, SWING_SHARE,
                              MomentDetector, goal_kind)
from deepkick.moments import SPRINT_KMH, SPRINT_NEW_TOP_FROM_KMH
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
    assert [m["facts"]["kind"] for m in _of(moments, "goal")] == [
        "opener", "extends_lead", "extends_lead", "pulls_back", "pulls_back"]


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


def test_speed_milestones_are_exactly_the_fast_shots(replay, moments):
    expected = [e["event_id"] for e in replay
                if e["event_type"] == "shot" and e["payload"]["speed_kmh"] >= SPEED_MILESTONE_KMH]
    assert expected
    assert [m["event_id"] for m in _of(moments, "speed_milestone")] == expected


def test_distance_milestones_flag_only_the_match_leader_every_step(replay, moments):
    covered, marks, expected = {}, 0, []
    for e in replay:
        if e["event_type"] == "player_run":
            pid = e["player"]["id"]
            covered[pid] = covered.get(pid, 0.0) + e["payload"]["distance_m"]
            mark = int(covered[pid] // (DISTANCE_STEP_KM * 1000))
            if mark > marks and covered[pid] >= max(covered.values()) - 0.05:  # snapshot distances are rounded to 0.1
                marks = mark
                expected.append((e["event_id"], mark * DISTANCE_STEP_KM))
    assert [km for _, km in expected] == [3, 6, 9]
    assert [(m["event_id"], m["facts"]["milestone_km"]) for m in _of(moments, "distance_milestone")] == expected


def test_distance_milestones_no_longer_dominate(moments):
    distance = _of(moments, "distance_milestone")
    assert 1 <= len(distance) <= 4 and len(distance) < len(moments) / 5
    assert all(m["priority"] == 1 for m in distance)
    assert all(m["priority"] > 1 for m in moments if m["type"] in ("goal", "big_chance"))


def test_pass_milestones_match_each_players_pass_count(replay, moments):
    counts, expected = {}, []
    for e in replay:
        if e["event_type"] == "pass":
            counts[e["player"]["id"]] = counts.get(e["player"]["id"], 0) + 1
            if counts[e["player"]["id"]] in PASS_MILESTONES:
                expected.append(e["event_id"])
    assert [m["event_id"] for m in _of(moments, "pass_milestone")] == expected


@pytest.mark.parametrize("passes, fires", [(49, False), (50, True), (51, False), (75, True)])
def test_pass_milestone_fires_on_the_50th_and_75th_pass(replay, passes, fires):
    # No seed-42 player reaches 50 passes, so feed a real pass with the player's count set in the snapshot.
    stats = StatsAgent()
    event = next(e for e in replay if e["event_type"] == "pass")
    for e in replay[:replay.index(event) + 1]:
        stats.update(e)
    snap = stats.snapshot()
    snap["players"][event["player"]["id"]].update(passes=passes, passes_completed=passes - 5,
                                                  pass_accuracy_pct=round(100 * (passes - 5) / passes, 1))
    found = _of(MomentDetector().detect(event, snap), "pass_milestone")
    assert bool(found) is fires
    if fires:
        assert found[0]["facts"] == {k: snap["players"][event["player"]["id"]][k]
                                     for k in ("passes", "passes_completed", "pass_accuracy_pct")}


def test_momentum_swings_alternate_between_clear_leaders(detected):
    swings = [(m, snap) for m, snap in detected if m["type"] == "momentum_swing"]
    assert swings
    for (m, snap), nxt in zip(swings, swings[1:] + [None]):
        facts = m["facts"]
        assert m["team"] == facts["to_team"] != facts["from_team"] and m["player"] is None
        assert facts["momentum"][m["team"]]["value"] >= SWING_SHARE
        assert facts["momentum"] == {tid: t["momentum"] for tid, t in snap["teams"].items()}
        assert facts["window_minutes"] == snap["momentum_window_minutes"]
        if nxt:
            assert nxt[0]["facts"]["from_team"] == m["team"]


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
        if m["type"] == "speed_milestone":
            player, team = snap["players"][m["player"]["id"]], snap["teams"][m["team"]]
            assert facts == {"shot_speed_kmh": event["payload"]["speed_kmh"],
                             "shot_distance_m": event["payload"]["distance_m"], "outcome": event["payload"]["outcome"],
                             "player_top_shot_speed_kmh": player["top_shot_speed_kmh"],
                             "team_top_shot_speed_kmh": team["top_shot_speed_kmh"]}
        if m["type"] == "sprint_milestone":
            assert event["event_type"] == "player_run"
            assert facts == {"run_speed_kmh": event["payload"]["speed_kmh"],
                             "run_distance_m": event["payload"]["distance_m"],
                             "team_top_run_speed_kmh": snap["teams"][m["team"]]["top_run_speed_kmh"]}
        if m["type"] == "distance_milestone":
            assert facts["distance_m"] == snap["players"][m["player"]["id"]]["distance_m"]
            assert facts["distance_m"] >= facts["milestone_km"] * 1000
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
    out, err = capsys.readouterr()
    assert [json.loads(line) for line in out.splitlines()] == moments
    assert err.startswith(f"{len(moments)} moments: ")
    for kind in {m["type"] for m in moments}:
        assert f"{kind}={len(_of(moments, kind))}" in err


def test_sprints_are_fast_runs_or_a_teams_new_fastest(replay, moments):
    top, expected = {}, []
    for e in replay:
        if e["event_type"] == "player_run":
            speed, before = e["payload"]["speed_kmh"], top.get(e["team"], 0.0)
            top[e["team"]] = max(before, speed)
            if speed >= SPRINT_KMH or (speed > before and speed >= SPRINT_NEW_TOP_FROM_KMH):
                expected.append(e["event_id"])
    assert expected
    assert [m["event_id"] for m in _of(moments, "sprint_milestone")] == expected


def test_shot_and_run_speeds_stay_in_separate_moments(moments):
    for m in _of(moments, "speed_milestone"):
        assert set(m["facts"]) >= {"shot_speed_kmh", "shot_distance_m", "outcome"} and "run_speed_kmh" not in m["facts"]
    for m in _of(moments, "sprint_milestone"):
        assert set(m["facts"]) == {"run_speed_kmh", "run_distance_m", "team_top_run_speed_kmh"}
