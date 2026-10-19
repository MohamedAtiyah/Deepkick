"""Tests for the seeded match simulator and its Event Hubs sender."""

import io
import json

import pytest

from deepkick.simulator import clock_seconds, load_league, send_events, simulate_match, write_replay


def test_same_seed_reproduces_the_replay_file(replay):
    assert simulate_match("ASH", "CAL", 42) == replay


def test_different_seed_gives_a_different_match():
    assert simulate_match("ASH", "CAL", 1) != simulate_match("ASH", "CAL", 2)


def test_unknown_or_same_club_is_rejected():
    with pytest.raises(ValueError):
        simulate_match("ASH", "ASH", 1)
    with pytest.raises(ValueError):
        simulate_match("ASH", "NOPE", 1)


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
    league_ids = {p["id"] for c in load_league()["clubs"] for p in c["squad"]}
    for e in replay:
        if e["player"]:
            assert e["player"]["id"] in league_ids
            assert e["player"]["id"].startswith(e["team"])


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
