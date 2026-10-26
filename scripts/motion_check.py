"""Measure how far players jump between the events that involve them, in m/s of match time, for a replay file."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from deepkick.simulator import LENGTH, WIDTH, clock_seconds

LIMIT_MS = 9.0  # faster than a sprinter: the pitch would have to teleport the player


def samples(events: list[dict]) -> dict[str, list[tuple[float, float, float]]]:
    """(match seconds, x, y) per player id in the home side's frame, from every event that places a player."""
    home = next(e["payload"]["home"]["id"] for e in events if e["event_type"] == "match_metadata")
    out: dict[str, list[tuple[float, float, float]]] = {}

    def put(pid: str, t: float, team: str, xy: dict) -> None:
        x, y = (xy["x"], xy["y"]) if team == home else (LENGTH - xy["x"], WIDTH - xy["y"])
        out.setdefault(pid, []).append((t, x, y))

    for e in events:
        if not e["player"] or not e["coordinates"]:
            continue
        t, p, kind = clock_seconds(e), e["payload"], e["event_type"]
        if kind == "player_run" and p["end"] == e["coordinates"]:
            continue  # a 5-minute tracking summary, not a place on the pitch
        put(e["player"]["id"], t, e["team"], e["coordinates"])
        if kind == "pass" and p["completed"]:
            put(p["recipient"]["id"], t, e["team"], p["end"])
        elif kind == "player_run":
            put(e["player"]["id"], t + p["distance_m"] / max(p["speed_kmh"] / 3.6, 0.1), e["team"], p["end"])
    return out


def jumps(events: list[dict]) -> tuple[int, int, int, float]:
    """(moves, moves faster than LIMIT_MS, instant moves over 2 m, median m/s) over consecutive samples per player."""
    total, fast, instant, speeds = 0, 0, 0, []
    for track in samples(events).values():
        track.sort()
        for (t0, x0, y0), (t1, x1, y1) in zip(track, track[1:]):
            d = math.hypot(x1 - x0, y1 - y0)
            total += 1
            if t1 - t0 <= 0:
                instant += d > 2
                fast += d > 2
                continue
            speed = d / (t1 - t0)
            speeds.append(speed)
            fast += speed > LIMIT_MS
    speeds.sort()
    return total, fast, instant, speeds[len(speeds) // 2] if speeds else 0.0


def main(argv: list[str] | None = None) -> None:
    """CLI: print the jump report for a replay file."""
    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("replay", type=Path)
    args = parser.parse_args(argv)
    events = [json.loads(line) for line in args.replay.read_text().splitlines()]
    total, fast, instant, median = jumps(events)
    print(f"{total} moves between a player's consecutive events; {fast} ({fast / total:.0%}) faster than "
          f"{LIMIT_MS:.0f} m/s, {instant} of them instant (same second, over 2 m); median {median:.1f} m/s")


if __name__ == "__main__":
    main()
