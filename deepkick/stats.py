"""Stats Agent: keeps running team and player stats from the event stream. Pure Python, no model calls."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from deepkick.contracts import validate_event
from deepkick.simulator import clock_seconds

log = logging.getLogger("deepkick.stats")

HARD_PASS = 0.7  # pass difficulty from which a pass counts as hard
FINAL_THIRD_X = 70.0  # metres up the pitch in the attacking team's frame
MOMENTUM_WINDOW_S = 5 * 60
MOMENTUM_WEIGHTS = {"shots": 2.0, "xg": 10.0, "final_third_entries": 1.0, "possession": 5.0}


@dataclass
class TeamStats:
    """Running totals for one club."""

    name: str = ""
    goals: int = 0
    shots: int = 0
    shots_on_target: int = 0
    xg: float = 0.0
    passes: int = 0
    passes_completed: int = 0
    pass_length_total_m: float = 0.0
    pass_difficulty_total: float = 0.0
    hard_passes: int = 0
    hard_passes_completed: int = 0
    top_shot_speed_kmh: float = 0.0
    top_run_speed_kmh: float = 0.0
    tackles: int = 0
    tackles_won: int = 0
    pressures: int = 0
    pressures_won: int = 0
    possession_seconds: int = 0


@dataclass
class PlayerStats:
    """Running totals for one player."""

    name: str
    position: str
    team: str
    actions: int = 0
    passes: int = 0
    passes_completed: int = 0
    shots: int = 0
    goals: int = 0
    xg: float = 0.0
    top_shot_speed_kmh: float = 0.0
    top_run_speed_kmh: float = 0.0
    tackles: int = 0
    tackles_won: int = 0
    pressures: int = 0
    pressures_won: int = 0
    distance_m: float = 0.0


def _pct(part: float, whole: float) -> float:
    return round(100 * part / whole, 1) if whole else 0.0


def _avg(total: float, count: int, digits: int) -> float:
    return round(total / count, digits) if count else 0.0


def _enters_final_third(event: dict[str, Any]) -> bool:
    """True for a completed pass or a carry that starts before the final third and ends inside it."""
    kind, payload = event["event_type"], event["payload"]
    moved = (kind == "pass" and payload["completed"]) or (kind == "player_run" and payload["with_ball"])
    return moved and event["coordinates"]["x"] < FINAL_THIRD_X <= payload["end"]["x"]


class StatsAgent:
    """Folds events into running match stats; `snapshot()` is the match state later agents cite."""

    def __init__(self) -> None:
        self.match_id: str | None = None
        self.match_clock = "00:00"
        self.half = 1
        self.status = "not_started"
        self.score: dict[str, int] = {}
        self.teams: dict[str, TeamStats] = {}
        self.players: dict[str, PlayerStats] = {}
        self.last_event_id: str | None = None
        self._seen: set[str] = set()
        self._clock = 0
        self._possession: str | None = None
        self._pressure = 0.0
        # (clock, team, "shots" | "xg" | "final_third_entries" | "possession", amount) inside the momentum window
        self._recent: deque[tuple[int, str, str, float]] = deque()

    def update(self, event: dict[str, Any]) -> bool:
        """Apply one event; return False if it was skipped as malformed or a duplicate."""
        start = time.monotonic()
        event_id = event.get("event_id") if isinstance(event, dict) else None
        try:
            validate_event(event)
        except ValueError as exc:
            log.warning("stage=stats event_id=%s skipped malformed event: %s", event_id, exc)
            self._log_handoff(event_id, start, fallback=True)
            return False
        # Event Hubs delivers at least once, so a redelivered event must not count twice.
        if event_id in self._seen:
            log.info("stage=stats event_id=%s skipped duplicate", event_id)
            self._log_handoff(event_id, start, fallback=True)
            return False
        self._seen.add(event_id)
        self._apply(event)
        self._log_handoff(event_id, start, fallback=False)
        return True

    def _log_handoff(self, event_id: str | None, start: float, fallback: bool) -> None:
        log.info("stage=stats event_id=%s duration_ms=%.2f fallback=%s",
                 event_id, (time.monotonic() - start) * 1000, "yes" if fallback else "no")

    def _team(self, team_id: str) -> TeamStats:
        return self.teams.setdefault(team_id, TeamStats())

    def _apply(self, event: dict[str, Any]) -> None:
        clock = clock_seconds(event)
        # The team that held the ball since the previous event gets the elapsed time.
        if self._possession is not None and clock > self._clock:
            self._team(self._possession).possession_seconds += clock - self._clock
            # shortcut: a spell is dated at its end, so one straddling the window edge counts whole (spells are ~3-9 s)
            self._recent.append((clock, self._possession, "possession", clock - self._clock))
        self._clock, self._possession = clock, event["context"]["possession_team"]
        self._pressure = event["context"]["pressure_index"]
        while self._recent and self._recent[0][0] < clock - MOMENTUM_WINDOW_S:
            self._recent.popleft()
        if _enters_final_third(event):
            self._recent.append((clock, event["team"], "final_third_entries", 1))

        self.match_id = event["match_id"]
        self.match_clock, self.half = event["match_clock"], event["half"]
        self.score = dict(event["context"]["score"])
        self.last_event_id = event["event_id"]
        for team_id in self.score:
            self._team(team_id)

        kind, payload, team = event["event_type"], event["payload"], self._team(event["team"])
        if kind == "match_metadata":
            self.status = payload["status"]
            for side in ("home", "away"):
                if side in payload:
                    self._team(payload[side]["id"]).name = payload[side]["name"]
            return

        ref = event["player"]
        player = self.players.setdefault(ref["id"], PlayerStats(ref["name"], ref["position"], event["team"]))
        player.actions += 1
        if kind == "pass":
            team.passes += 1
            player.passes += 1
            team.pass_length_total_m += payload["length_m"]
            team.pass_difficulty_total += payload["difficulty"]
            hard = payload["difficulty"] >= HARD_PASS
            team.hard_passes += hard
            if payload["completed"]:
                team.passes_completed += 1
                player.passes_completed += 1
                team.hard_passes_completed += hard
        elif kind == "shot":
            team.shots += 1
            player.shots += 1
            team.xg += payload["xg"]
            player.xg += payload["xg"]
            team.top_shot_speed_kmh = max(team.top_shot_speed_kmh, payload["speed_kmh"])
            player.top_shot_speed_kmh = max(player.top_shot_speed_kmh, payload["speed_kmh"])
            self._recent += [(clock, event["team"], "shots", 1), (clock, event["team"], "xg", payload["xg"])]
            if payload["outcome"] in ("goal", "saved"):
                team.shots_on_target += 1
            if payload["outcome"] == "goal":
                team.goals += 1
                player.goals += 1
        elif kind == "tackle":
            team.tackles += 1
            player.tackles += 1
            team.tackles_won += payload["won"]
            player.tackles_won += payload["won"]
        elif kind == "pressure_event":
            team.pressures += 1
            player.pressures += 1
            team.pressures_won += payload["success"]
            player.pressures_won += payload["success"]
        elif kind == "player_run":
            player.distance_m += payload["distance_m"]
            team.top_run_speed_kmh = max(team.top_run_speed_kmh, payload["speed_kmh"])
            player.top_run_speed_kmh = max(player.top_run_speed_kmh, payload["speed_kmh"])

    def momentum(self) -> dict[str, dict[str, Any]]:
        """Each team's shots, xG, possession and final-third entries in the last 5 minutes, and its momentum share.

        `value` is the team's share (0-100) of the weighted sum of those four; the two teams' values add up to 100.
        """
        window = {tid: dict.fromkeys(MOMENTUM_WEIGHTS, 0.0) for tid in self.teams}
        for _, team_id, kind, amount in self._recent:
            window[team_id][kind] += amount
        held = sum(w["possession"] for w in window.values())
        raw = {tid: sum(MOMENTUM_WEIGHTS[k] * v for k, v in w.items() if k != "possession")
               + MOMENTUM_WEIGHTS["possession"] * (w["possession"] / held if held else 0.0)
               for tid, w in window.items()}
        total = sum(raw.values())
        return {tid: {
            "shots": int(w["shots"]),
            "xg": round(w["xg"], 2),
            "possession_pct": _pct(w["possession"], held),
            "final_third_entries": int(w["final_third_entries"]),
            "value": _pct(raw[tid], total) if total else round(100 / len(window), 1),
        } for tid, w in window.items()}

    def snapshot(self) -> dict[str, Any]:
        """Return the current match state as a JSON-serialisable dict with rounded, quotable numbers."""
        total_possession = sum(t.possession_seconds for t in self.teams.values())
        momentum = self.momentum()
        teams = {}
        for team_id, t in self.teams.items():
            teams[team_id] = {k: v for k, v in asdict(t).items() if not k.endswith(("_total_m", "_total"))} | {
                "xg": round(t.xg, 2),
                "pass_accuracy_pct": _pct(t.passes_completed, t.passes),
                "avg_pass_length_m": _avg(t.pass_length_total_m, t.passes, 1),
                "avg_pass_difficulty": _avg(t.pass_difficulty_total, t.passes, 2),
                "hard_pass_completion_pct": _pct(t.hard_passes_completed, t.hard_passes),
                "possession_pct": _pct(t.possession_seconds, total_possession),
                "momentum": momentum[team_id],
            }
        players = {
            pid: asdict(p) | {"xg": round(p.xg, 2), "distance_m": round(p.distance_m, 1),
                              "pass_accuracy_pct": _pct(p.passes_completed, p.passes)}
            for pid, p in self.players.items()
        }
        return {
            "match_id": self.match_id,
            "match_clock": self.match_clock,
            "half": self.half,
            "status": self.status,
            "score": dict(self.score),
            "possession_team": self._possession,
            "pressure_index": self._pressure,
            "momentum_window_minutes": MOMENTUM_WINDOW_S // 60,
            "last_event_id": self.last_event_id,
            "events_processed": len(self._seen),
            "teams": teams,
            "players": players,
        }


def main(argv: list[str] | None = None) -> None:
    """CLI: replay a JSONL match file through the Stats Agent and print the final snapshot."""
    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("replay", type=Path, help="JSONL file written by `python -m deepkick.simulator --out`")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    agent = StatsAgent()
    with args.replay.open() as f:
        for line in f:
            agent.update(json.loads(line))
    json.dump(agent.snapshot(), sys.stdout, indent=2)
    print()


if __name__ == "__main__":
    main()
