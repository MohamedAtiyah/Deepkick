"""Moment detector: flags key moments from each event and the Stats Agent snapshot. Pure Python, no model calls."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any

from deepkick.simulator import clock_seconds
from deepkick.stats import StatsAgent

log = logging.getLogger("deepkick.moments")

MOMENT_FIELDS = frozenset(
    {"moment_id", "match_id", "event_id", "type", "match_clock", "half", "team", "player", "priority", "facts"}
)
BIG_CHANCE_XG = 0.2
HIGH_PRESS_X = 75  # metres up the pitch in the pressing team's attacking frame
SPELL_SHOTS, SPELL_WINDOW_S, SPELL_COOLDOWN_S = 3, 5 * 60, 10 * 60
PRIORITY = {"goal": 3, "full_time": 3, "big_chance": 2, "pressure_spell": 2, "half_time": 2, "high_press_win": 1}


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

    def detect(self, event: dict[str, Any], snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        """Return the moments this event creates, highest priority first; facts only quote the event and snapshot."""
        start = time.monotonic()
        moments = [self._moment(event, kind, facts) for kind, facts in self._find(event, snapshot)]
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

    stats, detector = StatsAgent(), MomentDetector()
    with args.replay.open() as f:
        for line in f:
            event = json.loads(line)
            if stats.update(event):
                for moment in detector.detect(event, stats.snapshot()):
                    print(json.dumps(moment))


if __name__ == "__main__":
    main()
