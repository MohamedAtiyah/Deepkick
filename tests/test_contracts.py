"""Tests for the event contract validator."""

import copy

import pytest

from deepkick.contracts import validate_event, validate_overlay


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
    (lambda e: e.update(coordinates={"x": 106, "y": 30}), "coordinates"),
    (lambda e: e.update(coordinates={"x": 50, "y": 69}), "coordinates"),
    (lambda e: e["context"].update(pressure_index=1.5), "pressure_index"),
    (lambda e: e["payload"].update(difficulty=1.2), "difficulty"),
    (lambda e: e["payload"].update(difficulty=True), "difficulty"),
    (lambda e: e["payload"].pop("difficulty"), "difficulty"),
])
def test_rejects_broken_events(replay, mutate, message):
    event = copy.deepcopy(next(e for e in replay if e["event_type"] == "pass"))
    mutate(event)
    with pytest.raises(ValueError, match=message):
        validate_event(event)


@pytest.mark.parametrize("speed", [None, 59.9, 130.1, "fast"])
def test_rejects_shot_without_realistic_speed(replay, speed):
    event = copy.deepcopy(next(e for e in replay if e["event_type"] == "shot"))
    event["payload"]["speed_kmh"] = speed
    with pytest.raises(ValueError, match="speed_kmh"):
        validate_event(event)


@pytest.mark.parametrize("speed", [None, -1, 37.1, "quick"])
def test_rejects_run_without_a_plausible_speed(replay, speed):
    event = copy.deepcopy(next(e for e in replay if e["event_type"] == "player_run"))
    event["payload"]["speed_kmh"] = speed
    with pytest.raises(ValueError, match="player_run speed_kmh"):
        validate_event(event)


def test_metadata_may_omit_player_and_coordinates(replay):
    validate_event(replay[0])
    assert replay[0]["player"] is None and replay[0]["coordinates"] is None


def _overlay():
    return {"overlay_id": "ASH-CAL-42-00208-goal-analyst-en", "type": "goal", "match_clock": "13:52",
            "show_at_ms": 832000, "duration_ms": 8000, "priority": 5, "persona": "analyst", "lang": "en",
            "payload": {"player": "Rafe Ironsby", "headline": "Rafe Ironsby opens the scoring",
                        "detail": "A 0.252 xG chance.", "why": "It makes the score ASH 0-1 CAL."}}


@pytest.mark.parametrize("mutate", [
    lambda o: None,
    lambda o: o["payload"].update(player=None),  # match-wide moments have no player
    lambda o: o.update(persona="casual", lang="ar", priority=1),
    lambda o: o.update(lang="en"),
])
def test_valid_overlays_pass(mutate):
    overlay = _overlay()
    mutate(overlay)
    validate_overlay(overlay)


@pytest.mark.parametrize("mutate, message", [
    (lambda o: o.pop("payload"), "missing"),
    (lambda o: o.update(fallback=True), "extra"),
    (lambda o: o.update(overlay_id=""), "overlay_id"),
    (lambda o: o.update(match_clock="13:5"), "match_clock"),
    (lambda o: o.update(show_at_ms=-1), "show_at_ms"),
    (lambda o: o.update(show_at_ms=832.0), "show_at_ms"),
    (lambda o: o.update(duration_ms=0), "duration_ms"),
    (lambda o: o.update(priority=6), "priority"),
    (lambda o: o.update(priority=True), "priority"),
    (lambda o: o.update(persona="coach"), "persona"),
    (lambda o: o.update(lang="fr"), "lang"),
    (lambda o: o.update(lang="es"), "lang"),
    (lambda o: o.update(lang="EN"), "lang"),
    (lambda o: o["payload"].pop("why"), "payload"),
    (lambda o: o["payload"].update(player=""), "payload.player"),
    (lambda o: o["payload"].update(headline=" "), "payload.headline"),
])
def test_rejects_broken_overlays(mutate, message):
    overlay = _overlay()
    mutate(overlay)
    with pytest.raises(ValueError, match=message):
        validate_overlay(overlay)


def test_rejects_non_object_overlay():
    with pytest.raises(ValueError, match="JSON object"):
        validate_overlay(["not", "an", "overlay"])
