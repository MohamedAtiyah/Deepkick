"""Tests for the event contract validator."""

import copy

import pytest

from deepkick.contracts import validate_event


def test_every_replay_event_is_valid(replay):
    for event in replay:
        validate_event(event)


@pytest.mark.parametrize("mutate, message", [
    (lambda e: e.pop("context"), "missing"),
    (lambda e: e.update(extra=1), "extra"),
    (lambda e: e.update(event_type="corner"), "event_type"),
    (lambda e: e.update(half=3), "half"),
    (lambda e: e.update(match_clock="5:00"), "match_clock"),
    (lambda e: e.update(player=None), "player"),
    (lambda e: e.update(coordinates={"x": 101, "y": 50}), "coordinates"),
    (lambda e: e["context"].update(pressure_index=1.5), "pressure_index"),
])
def test_rejects_broken_events(replay, mutate, message):
    event = copy.deepcopy(next(e for e in replay if e["event_type"] == "pass"))
    mutate(event)
    with pytest.raises(ValueError, match=message):
        validate_event(event)


def test_metadata_may_omit_player_and_coordinates(replay):
    validate_event(replay[0])
    assert replay[0]["player"] is None and replay[0]["coordinates"] is None
