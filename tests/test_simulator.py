"""Tests for the seeded match simulator and its Event Hubs sender."""

import copy
import io
import json
import math

import pytest

import deepkick.simulator
from deepkick.simulator import (RUN_SPEED_KMH, _Match, clock_seconds, load_league, run_speed_kmh, send_events,
                                shape_point, simulate_match, write_replay)


def test_same_seed_reproduces_the_replay_file(replay):
    assert simulate_match(42) == replay


def test_league_fields_the_simulator_does_not_use_leave_the_match_unchanged(replay):
    league = copy.deepcopy(load_league())
    for club in league["clubs"]:
        club["kit_sponsor"] = "none"
        for player in club["starters"] + club["bench"]:
            player |= {"nickname": "x", "boot_size": 44}
    assert simulate_match(42, league) == replay


def test_different_seed_gives_a_different_match():
    assert simulate_match(1) != simulate_match(2)


@pytest.mark.parametrize("home, away", [("ASH", "ASH"), ("ASH", "NOPE")])
def test_fixture_with_unknown_or_same_club_is_rejected(home, away):
    league = copy.deepcopy(load_league())
    league["match"].update(home=home, away=away)
    with pytest.raises(ValueError):
        simulate_match(1, league)


def test_ids_unique_and_clock_never_goes_back(replay):
    assert len({e["event_id"] for e in replay}) == len(replay)
    clocks = [clock_seconds(e) for e in replay]
    assert clocks == sorted(clocks)
    assert clocks[-1] == 90 * 60


def test_match_status_sequence(replay):
    statuses = [e["payload"]["status"] for e in replay if e["event_type"] == "match_metadata"]
    assert statuses == ["kickoff", "half_time", "second_half", "full_time"]


def test_final_score_equals_goals_scored(replay):
    goals = {"ASH": 0, "CAL": 0}
    for e in replay:
        if e["event_type"] == "shot" and e["payload"]["outcome"] == "goal":
            goals[e["team"]] += 1
    assert replay[-1]["context"]["score"] == goals


def test_players_come_from_the_league_squads(replay):
    starters = {p["id"]: p["position"] for c in load_league()["clubs"] for p in c["starters"]}
    for e in replay:
        if e["player"]:
            assert starters[e["player"]["id"]] == e["player"]["position"]
            assert e["player"]["id"].startswith(e["team"])


def test_players_act_near_their_formation_slot(replay):
    # Strikers should, on average, touch the ball much further upfield than centre-backs.
    def mean_x(position):
        xs = [e["coordinates"]["x"] for e in replay
              if e["event_type"] == "pass" and e["team"] == "ASH" and e["player"]["position"] == position]
        return sum(xs) / len(xs)
    assert mean_x("RST") > mean_x("RCB") + 15


def test_harder_passes_are_longer_and_fail_more(replay):
    passes = [e["payload"] for e in replay if e["event_type"] == "pass"]
    hard = [p for p in passes if p["difficulty"] >= 0.7]
    easy = [p for p in passes if p["difficulty"] < 0.3]
    mean = lambda ps, f: sum(f(p) for p in ps) / len(ps)
    assert hard and easy
    assert mean(hard, lambda p: p["length_m"]) > mean(easy, lambda p: p["length_m"])
    assert mean(hard, lambda p: p["completed"]) < mean(easy, lambda p: p["completed"]) - 0.15


def test_longer_shots_are_struck_harder(replay):
    shots = sorted((e["payload"]["distance_m"], e["payload"]["speed_kmh"]) for e in replay if e["event_type"] == "shot")
    half = len(shots) // 2
    assert sum(s for _, s in shots[half:]) / (len(shots) - half) > sum(s for _, s in shots[:half]) / half


def test_starters_cover_realistic_distances(replay):
    covered = {}
    for e in replay:
        if e["event_type"] == "player_run":
            key = (e["player"]["id"], e["player"]["position"])
            covered[key] = covered.get(key, 0) + e["payload"]["distance_m"]
    assert len(covered) == 22
    for (_, position), metres in covered.items():
        assert (4_500 if position == "GK" else 9_000) < metres < (6_000 if position == "GK" else 11_500)


def test_write_replay_round_trips(replay):
    buf = io.StringIO()
    write_replay(replay[:5], buf)
    assert [json.loads(line) for line in buf.getvalue().splitlines()] == replay[:5]


class _FakeProducer:
    def __init__(self):
        self.batches = []

    def send_batch(self, batch, partition_key):
        self.batches.append((partition_key, [json.loads(e.body_as_str()) for e in batch]))


def test_send_events_batches_in_order_with_match_partition_key(replay):
    producer = _FakeProducer()
    send_events(replay, producer)
    assert all(key == "ASH-CAL-42" for key, _ in producer.batches)
    assert [e for _, batch in producer.batches for e in batch] == replay
    assert max(len(batch) for _, batch in producer.batches) <= 200


def test_send_events_logs_and_reraises_on_failure(replay, caplog):
    from azure.eventhub.exceptions import EventHubError

    class Broken:
        def send_batch(self, batch, partition_key):
            raise EventHubError("down")

    with pytest.raises(EventHubError):
        send_events(replay[:3], Broken())
    assert "send failed" in caplog.text


def _without_run_speed(events):
    out = copy.deepcopy(events)
    for e in out:
        if e["event_type"] == "player_run":
            del e["payload"]["speed_kmh"]
    return out


def test_run_speed_does_not_touch_the_main_rng(replay, monkeypatch):
    # With run speeds replaced by a constant, every other field must be unchanged: the speeds use their own RNG.
    monkeypatch.setattr(deepkick.simulator, "run_speed_kmh", lambda *args: 20.0)
    assert _without_run_speed(simulate_match(42)) == _without_run_speed(replay)


def test_every_run_has_a_plausible_speed(replay):
    runs = [e["payload"] for e in replay if e["event_type"] == "player_run"]
    assert runs and all(0 < r["speed_kmh"] <= 37 for r in runs)
    real = [r for r in runs if r["distance_m"] < 100]  # tracking summaries cover hundreds of metres
    summaries = [r for r in runs if r["distance_m"] >= 100]
    assert all(RUN_SPEED_KMH[0] <= r["speed_kmh"] <= RUN_SPEED_KMH[1] for r in real)
    assert all(r["speed_kmh"] < 10 for r in summaries)  # a 5-minute average, not a sprint


def test_run_speed_rises_with_distance_dips_with_the_ball_and_is_capped():
    speeds = {(d, b): [run_speed_kmh(42, f"ASH-CAL-42-{i:05d}", d, b) for i in range(400)]
              for d in (4.0, 18.0) for b in (False, True)}
    mean = {k: sum(v) / len(v) for k, v in speeds.items()}
    assert mean[(18.0, False)] > mean[(4.0, False)] + 10
    assert mean[(18.0, True)] < mean[(18.0, False)]
    assert all(RUN_SPEED_KMH[0] <= s <= 37 for v in speeds.values() for s in v)
    assert run_speed_kmh(42, "ASH-CAL-42-00005", 14.9, True) == run_speed_kmh(42, "ASH-CAL-42-00005", 14.9, True)


def _shape_distances(replay):
    """For every on-ball action, how far the actor is from his place in the team shape around the ball."""
    league = load_league()
    slots = {p["id"]: (p["position"], c["formation"]["slots"][p["position"]]) for c in league["clubs"]
             for p in c["starters"]}
    out = []
    for e in replay:
        if not e["player"] or e["event_type"] in ("possession_change", "match_metadata"):
            continue
        if e["event_type"] == "player_run" and e["payload"]["end"] == e["coordinates"]:
            continue  # a tracking summary, not an action
        xy = (e["coordinates"]["x"], e["coordinates"]["y"])
        out.append(math.dist(shape_point(*slots[e["player"]["id"]], xy), xy))
    return sorted(out)


def test_actors_are_chosen_near_the_ball(replay):
    # Each action goes to a team-mate whose place in the shape is near the ball, so the pitch never has to send a
    # player across the field to reach it. (Before this rule: median 19.7 m and 49% over 20 m on seed 42.)
    distances = _shape_distances(replay)
    assert distances[len(distances) // 2] < 14
    assert sum(d > 20 for d in distances) / len(distances) < 0.15


def test_pick_near_favours_the_nearest_team_mate_in_the_shape():
    match = _Match(load_league(), seed=7, kickoff=deepkick.simulator.KICKOFF)
    club = match.club("ASH")
    striker = next(p for p in club["starters"] if p["position"] == "RST")
    ball = shape_point("RST", club["formation"]["slots"]["RST"], (80.0, 40.0))  # right where the striker stands
    picks = [match.pick_near("ASH", ball)["id"] for _ in range(300)]
    assert picks.count(striker["id"]) / len(picks) > 0.5
    keeper = next(p for p in club["starters"] if p["position"] == "GK")
    assert keeper["id"] not in picks  # a keeper 75 m away is never chosen for a ball at the other end


def test_shape_point_shifts_the_block_toward_the_ball_and_stays_on_it():
    slot = {"x": 75, "y": 42}
    assert shape_point("RST", slot, (52.5, 34)) == (75, 42)  # ball on the centre spot: no shift
    assert shape_point("RST", slot, (105, 68)) == (93, 52)  # the cap: 18 m along, 10 m across
    assert shape_point("RCB", {"x": 22, "y": 44}, (105, 68)) == (31, 49)  # defenders move half as far
    assert shape_point("GK", {"x": 5, "y": 34}, (105, 68)) == (9, 38)  # keepers at most 4 m
    x, y = shape_point("RW", {"x": 100, "y": 66}, (105, 68))
    assert x <= 105 and y <= 68  # never off the pitch


def test_the_same_seed_still_gives_the_same_match_with_the_new_actor_rule():
    assert simulate_match(42) == simulate_match(42)
