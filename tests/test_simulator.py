"""Tests for the seeded match simulator and its Event Hubs sender."""

import copy
import io
import json

import pytest

from deepkick.simulator import clock_seconds, load_league, send_events, simulate_match, write_replay


def test_same_seed_reproduces_the_replay_file(replay):
    assert simulate_match(42) == replay


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
