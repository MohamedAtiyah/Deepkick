"""Event contract shared by the simulator (producer) and the stream consumers."""

import re
from typing import Any

EVENT_TYPES = frozenset(
    {"match_metadata", "pass", "shot", "tackle", "possession_change", "pressure_event", "player_run"}
)
EVENT_FIELDS = frozenset(
    {"event_id", "match_id", "timestamp", "match_clock", "half", "event_type", "team",
     "player", "coordinates", "payload", "context"}
)
PITCH_M = {"x": 105, "y": 68}  # pitch length and width in metres
SHOT_SPEED_KMH = (60, 130)  # realistic range for a struck shot
RUN_SPEED_KMH_MAX = 37  # a player's run; 5-minute tracking summaries report their (much lower) average
_CLOCK = re.compile(r"^\d{2,3}:[0-5]\d$")


def validate_event(event: dict[str, Any]) -> None:
    """Raise ValueError if the event does not match the event contract."""
    if not isinstance(event, dict):
        raise ValueError("event must be a JSON object")
    missing, extra = EVENT_FIELDS - event.keys(), event.keys() - EVENT_FIELDS
    if missing or extra:
        raise ValueError(f"bad fields: missing={sorted(missing)} extra={sorted(extra)}")
    if event["event_type"] not in EVENT_TYPES:
        raise ValueError(f"unknown event_type {event['event_type']!r}")
    if event["half"] not in (1, 2):
        raise ValueError(f"bad half {event['half']!r}")
    if not isinstance(event["match_clock"], str) or not _CLOCK.match(event["match_clock"]):
        raise ValueError(f"bad match_clock {event['match_clock']!r}")
    if not isinstance(event["payload"], dict):
        raise ValueError("payload must be an object")

    # Only match_metadata may omit the acting player and the ball position.
    player, coords = event["player"], event["coordinates"]
    if event["event_type"] != "match_metadata" or player is not None:
        if not isinstance(player, dict) or not {"id", "name", "position"} <= player.keys():
            raise ValueError("player must have id, name, position")
    if event["event_type"] != "match_metadata" or coords is not None:
        if not isinstance(coords, dict) or not all(
            isinstance(coords.get(k), (int, float)) and 0 <= coords[k] <= limit for k, limit in PITCH_M.items()
        ):
            raise ValueError(f"coordinates must be metres within x 0..105, y 0..68, got {coords!r}")

    ctx = event["context"]
    if not isinstance(ctx, dict) or not {"score", "possession_team", "pressure_index"} <= ctx.keys():
        raise ValueError("context must have score, possession_team, pressure_index")
    if not 0 <= ctx["pressure_index"] <= 1:
        raise ValueError(f"pressure_index out of range: {ctx['pressure_index']!r}")

    payload = event["payload"]
    if event["event_type"] == "pass" and not _in_range(payload.get("difficulty"), 0, 1):
        raise ValueError(f"pass difficulty must be 0..1, got {payload.get('difficulty')!r}")
    if event["event_type"] == "shot" and not _in_range(payload.get("speed_kmh"), *SHOT_SPEED_KMH):
        raise ValueError(f"shot speed_kmh must be {SHOT_SPEED_KMH[0]}..{SHOT_SPEED_KMH[1]}, "
                         f"got {payload.get('speed_kmh')!r}")
    if event["event_type"] == "player_run" and not _in_range(payload.get("speed_kmh"), 0, RUN_SPEED_KMH_MAX):
        raise ValueError(f"player_run speed_kmh must be 0..{RUN_SPEED_KMH_MAX}, got {payload.get('speed_kmh')!r}")


def _in_range(value: Any, low: float, high: float) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and low <= value <= high


OVERLAY_FIELDS = frozenset(
    {"overlay_id", "type", "match_clock", "show_at_ms", "duration_ms", "priority", "persona", "lang", "payload"}
)
PERSONAS = ("analyst", "casual", "player")
LANGS = ("en",)  # overlays are English only
PAYLOAD_FIELDS = frozenset({"player", "headline", "detail", "why", "evidence", "source"})
SOURCES = ("model", "template")  # who wrote the overlay text: the Foundry model or a stats template


def validate_overlay(overlay: dict[str, Any]) -> None:
    """Raise ValueError if the overlay does not match the overlay contract."""
    if not isinstance(overlay, dict):
        raise ValueError("overlay must be a JSON object")
    missing, extra = OVERLAY_FIELDS - overlay.keys(), overlay.keys() - OVERLAY_FIELDS
    if missing or extra:
        raise ValueError(f"bad overlay fields: missing={sorted(missing)} extra={sorted(extra)}")
    for key in ("overlay_id", "type"):
        if not isinstance(overlay[key], str) or not overlay[key]:
            raise ValueError(f"{key} must be a non-empty string")
    if not isinstance(overlay["match_clock"], str) or not _CLOCK.match(overlay["match_clock"]):
        raise ValueError(f"bad match_clock {overlay['match_clock']!r}")
    if not _is_int(overlay["show_at_ms"]) or overlay["show_at_ms"] < 0:
        raise ValueError(f"show_at_ms must be a non-negative integer, got {overlay['show_at_ms']!r}")
    if not _is_int(overlay["duration_ms"]) or overlay["duration_ms"] <= 0:
        raise ValueError(f"duration_ms must be a positive integer, got {overlay['duration_ms']!r}")
    if not _is_int(overlay["priority"]) or not 1 <= overlay["priority"] <= 5:
        raise ValueError(f"priority must be an integer 1..5, got {overlay['priority']!r}")
    if overlay["persona"] not in PERSONAS:
        raise ValueError(f"persona must be one of {PERSONAS}, got {overlay['persona']!r}")
    if overlay["lang"] not in LANGS:
        raise ValueError(f"lang must be one of {LANGS}, got {overlay['lang']!r}")
    payload = overlay["payload"]
    if not isinstance(payload, dict) or payload.keys() != PAYLOAD_FIELDS:
        raise ValueError("payload must have exactly player, headline, detail, why, evidence, source")
    if payload["player"] is not None and (not isinstance(payload["player"], str) or not payload["player"]):
        raise ValueError("payload.player must be a player name or null")
    for key in ("headline", "detail", "why"):
        if not isinstance(payload[key], str) or not payload[key].strip():
            raise ValueError(f"payload.{key} must be a non-empty string")
    if payload["source"] not in SOURCES:
        raise ValueError(f"payload.source must be one of {SOURCES}, got {payload['source']!r}")
    evidence = payload["evidence"]
    if not isinstance(evidence, list) or not all(
            isinstance(e, dict) and e.keys() == {"label", "value"} and isinstance(e["label"], str)
            and e["label"].strip() and _is_number(e["value"]) for e in evidence):
        raise ValueError("payload.evidence must be a list of {label, value} with a non-empty label and a number")


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)
