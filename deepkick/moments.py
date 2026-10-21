"""Moment detector: flags key moments from each event and the Stats Agent snapshot. Pure Python, no model calls."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections import Counter, deque
from pathlib import Path
from typing import Any

from deepkick.simulator import clock_seconds
from deepkick.stats import MOMENTUM_WINDOW_S, StatsAgent

log = logging.getLogger("deepkick.moments")

MOMENT_FIELDS = frozenset(
    {"moment_id", "match_id", "event_id", "type", "match_clock", "half", "team", "player", "priority", "facts"}
)
BIG_CHANCE_XG = 0.2
HIGH_PRESS_X = 75  # metres up the pitch in the pressing team's attacking frame
SPELL_SHOTS, SPELL_WINDOW_S, SPELL_COOLDOWN_S = 3, 5 * 60, 10 * 60
SWING_SHARE = 70.0  # a team leads momentum once its share reaches this (a 70/30 split)
SPEED_MILESTONE_KMH = 120.0  # shot speed: the ball's speed
SPRINT_KMH = 30.0  # run speed: the player's speed
SPRINT_NEW_TOP_FROM_KMH = 25.0  # a team's new fastest run counts from here, so early slow "records" don't fire
DISTANCE_STEP_KM = 3  # only the match's leading distance coverer, at 3, 6, 9 km: three moments a match, not 48
PASS_MILESTONES = (50, 75)
PRIORITY = {"goal": 3, "full_time": 3, "big_chance": 2, "pressure_spell": 2, "half_time": 2, "momentum_swing": 2,
            "high_press_win": 1, "speed_milestone": 1, "sprint_milestone": 1, "distance_milestone": 1,
            "pass_milestone": 1}


def goal_kind(goals_for: int, goals_against: int) -> str:
    """Name a goal from the scoring team's score after it: opener, equaliser, go_ahead, extends_lead or pulls_back."""
    before = goals_for - 1
    if goals_for == 1 and goals_against == 0:
        return "opener"
    if goals_for == goals_against:
        return "equaliser"
    if before == goals_against:
        return "go_ahead"
    return "extends_lead" if before > goals_against else "pulls_back"


class MomentDetector:
    """Stateful detector; call `detect` with each accepted event and the snapshot taken after it."""

    def __init__(self) -> None:
        self._shots: dict[str, deque[tuple[int, float]]] = {}
        self._last_spell: dict[str, int] = {}
        self._momentum_leader: str | None = None
        self._distance_marks = 0  # how many DISTANCE_STEP_KM marks the match's distance leader has passed
        self._top_run: dict[str, float] = {}  # team -> fastest run speed seen before this event

    def detect(self, event: dict[str, Any], snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        """Return the moments this event creates, highest priority first; facts only quote the event and snapshot."""
        start = time.monotonic()
        moments = [self._moment(event, kind, facts) for kind, facts in self._find(event, snapshot)]
        swing = self._swing(event, snapshot)
        if swing:
            # The moment belongs to the team that took momentum, not to whoever made this event.
            moments.append(self._moment(event, "momentum_swing", swing) | {"team": swing["to_team"], "player": None})
        moments.sort(key=lambda m: -m["priority"])
        log.info("stage=moments event_id=%s moments=%d duration_ms=%.2f fallback=no",
                 event["event_id"], len(moments), (time.monotonic() - start) * 1000)
        return moments

    def _find(self, event: dict[str, Any], snap: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        kind, payload, team = event["event_type"], event["payload"], event["team"]
        found: list[tuple[str, dict[str, Any]]] = []

        if kind == "match_metadata" and payload["status"] in ("half_time", "full_time"):
            found.append((payload["status"], {"score": snap["score"], "teams": {
                tid: {k: t[k] for k in ("shots", "shots_on_target", "xg", "possession_pct", "pass_accuracy_pct")}
                for tid, t in snap["teams"].items()}}))

        elif kind == "shot":
            team_stats = snap["teams"][team]
            shot = {"xg": payload["xg"], "distance_m": payload["distance_m"], "outcome": payload["outcome"],
                    "team_shots": team_stats["shots"], "team_xg": team_stats["xg"]}
            if payload["outcome"] == "goal":
                opponent = next(t for t in snap["score"] if t != team)
                found.append(("goal", shot | {
                    "kind": goal_kind(snap["score"][team], snap["score"][opponent]),
                    "score": snap["score"],
                    "player_goals": snap["players"][event["player"]["id"]]["goals"],
                }))
            elif payload["xg"] >= BIG_CHANCE_XG:
                found.append(("big_chance", shot))

            spell = self._spell(team, clock_seconds(event), payload["xg"])
            if spell:
                found.append(("pressure_spell", spell))

            if payload["speed_kmh"] >= SPEED_MILESTONE_KMH:
                player = snap["players"][event["player"]["id"]]
                found.append(("speed_milestone", {
                    "shot_speed_kmh": payload["speed_kmh"], "shot_distance_m": payload["distance_m"],
                    "outcome": payload["outcome"], "player_top_shot_speed_kmh": player["top_shot_speed_kmh"],
                    "team_top_shot_speed_kmh": team_stats["top_shot_speed_kmh"],
                }))

        elif kind == "pass":
            player = snap["players"][event["player"]["id"]]
            if player["passes"] in PASS_MILESTONES:
                found.append(("pass_milestone", {k: player[k] for k in
                                                 ("passes", "passes_completed", "pass_accuracy_pct")}))

        elif kind == "player_run":
            speed, previous_top = payload["speed_kmh"], self._top_run.get(team, 0.0)
            self._top_run[team] = max(previous_top, speed)
            if speed >= SPRINT_KMH or (speed > previous_top and speed >= SPRINT_NEW_TOP_FROM_KMH):
                found.append(("sprint_milestone", {
                    "run_speed_kmh": speed, "run_distance_m": payload["distance_m"],
                    "team_top_run_speed_kmh": snap["teams"][team]["top_run_speed_kmh"],
                }))
            pid = event["player"]["id"]
            distance = snap["players"][pid]["distance_m"]
            marks = int(distance // (DISTANCE_STEP_KM * 1000))
            leads = distance >= max(p["distance_m"] for p in snap["players"].values())
            if marks > self._distance_marks and leads:
                self._distance_marks = marks
                found.append(("distance_milestone", {"milestone_km": marks * DISTANCE_STEP_KM, "distance_m": distance}))

        elif kind == "pressure_event" and payload["success"] and event["coordinates"]["x"] >= HIGH_PRESS_X:
            found.append(("high_press_win", {
                "x_m": event["coordinates"]["x"],
                "target": payload["target"]["name"],
                "team_pressures_won": snap["teams"][team]["pressures_won"],
            }))
        return found

    def _spell(self, team: str, clock: int, xg: float) -> dict[str, Any] | None:
        """Flag a team's SPELL_SHOTS-th shot within SPELL_WINDOW_S, at most once per SPELL_COOLDOWN_S."""
        shots = self._shots.setdefault(team, deque())
        shots.append((clock, xg))
        while clock - shots[0][0] > SPELL_WINDOW_S:
            shots.popleft()
        if len(shots) < SPELL_SHOTS or clock - self._last_spell.get(team, -SPELL_COOLDOWN_S) < SPELL_COOLDOWN_S:
            return None
        self._last_spell[team] = clock
        return {"shots": len(shots), "window_minutes": SPELL_WINDOW_S // 60,
                "xg": round(sum(x for _, x in shots), 2)}

    def _swing(self, event: dict[str, Any], snap: dict[str, Any]) -> dict[str, Any] | None:
        """Flag momentum passing from one team to the other, each side having reached SWING_SHARE in turn."""
        if event["event_type"] == "match_metadata" or clock_seconds(event) < MOMENTUM_WINDOW_S:
            return None  # the first window is too short to judge
        leader = next((tid for tid, t in snap["teams"].items() if t["momentum"]["value"] >= SWING_SHARE), None)
        if leader is None or leader == self._momentum_leader:
            return None
        previous, self._momentum_leader = self._momentum_leader, leader
        if previous is None:
            return None
        return {"to_team": leader, "from_team": previous, "window_minutes": snap["momentum_window_minutes"],
                "momentum": {tid: t["momentum"] for tid, t in snap["teams"].items()}}

    @staticmethod
    def _moment(event: dict[str, Any], kind: str, facts: dict[str, Any]) -> dict[str, Any]:
        return {
            "moment_id": f"{event['event_id']}-{kind}",
            "match_id": event["match_id"],
            "event_id": event["event_id"],
            "type": kind,
            "match_clock": event["match_clock"],
            "half": event["half"],
            "team": None if event["event_type"] == "match_metadata" else event["team"],
            "player": event["player"],
            "priority": PRIORITY[kind],
            "facts": facts,
        }


def main(argv: list[str] | None = None) -> None:
    """CLI: replay a JSONL match file through the Stats Agent and moment detector and print moments as JSON lines."""
    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("replay", type=Path, help="JSONL file written by `python -m deepkick.simulator --out`")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    stats, detector, counts = StatsAgent(), MomentDetector(), Counter()
    with args.replay.open() as f:
        for line in f:
            event = json.loads(line)
            if stats.update(event):
                for moment in detector.detect(event, stats.snapshot()):
                    print(json.dumps(moment))
                    counts[moment["type"]] += 1
    # The summary goes to stderr so stdout stays one moment per line.
    summary = ", ".join(f"{kind}={n}" for kind, n in counts.most_common())
    print(f"{sum(counts.values())} moments: {summary}", file=sys.stderr)


if __name__ == "__main__":
    main()
