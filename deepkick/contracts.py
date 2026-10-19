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
