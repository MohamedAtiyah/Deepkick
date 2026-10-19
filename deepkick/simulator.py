"""Seeded football match simulator that writes replays or streams events to Azure Event Hubs.

Coordinates are 0-100 on both axes, oriented so the acting team always attacks towards x=100.
Each event's `context` is the match state after that event.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

LEAGUE_PATH = Path(__file__).resolve().parent.parent / "data" / "league.json"
KICKOFF = datetime(2026, 10, 27, 15, 0, tzinfo=timezone.utc)
HALF_SECONDS = 45 * 60
HALF_TIME_BREAK = 15 * 60
BATCH_SIZE = 200  # ~90 KB per batch, well under the Basic tier 256 KB limit

# Relative chance of each position doing the action.
DEFEND_W = {"GK": 0, "DF": 6, "MF": 4, "FW": 1}
RUN_W = {"GK": 0, "DF": 2, "MF": 4, "FW": 5}
RECEIVE_W = {  # by the third of the pitch the pass lands in
    0: {"GK": 1, "DF": 6, "MF": 3, "FW": 0},
    1: {"GK": 0, "DF": 2, "MF": 6, "FW": 2},
    2: {"GK": 0, "DF": 1, "MF": 4, "FW": 6},
}

log = logging.getLogger("deepkick.simulator")


def load_league(path: Path = LEAGUE_PATH) -> dict[str, Any]:
    """Load the fictional league data."""
    return json.loads(path.read_text())


def _clamp(v: float, lo: float = 1.0, hi: float = 99.0) -> float:
    return max(lo, min(hi, v))


def _metres(x0: float, y0: float, x1: float, y1: float) -> float:
    """Distance on a 105 x 68 m pitch between two 0-100 points."""
    return math.hypot((x1 - x0) * 1.05, (y1 - y0) * 0.68)


def _ref(player: dict[str, Any]) -> dict[str, Any]:
    return {"id": player["id"], "name": player["name"], "position": player["position"]}


class _Match:
    """Mutable state of one simulated match; `run()` returns its events."""

    def __init__(self, league: dict[str, Any], home_id: str, away_id: str, seed: int, kickoff: datetime):
        clubs = {c["id"]: c for c in league["clubs"]}
        if home_id == away_id or home_id not in clubs or away_id not in clubs:
            raise ValueError(f"need two different club ids from {sorted(clubs)}, got {home_id!r}, {away_id!r}")
        self.rng = random.Random(seed)
        self.seed, self.kickoff = seed, kickoff
        self.home, self.away = clubs[home_id], clubs[away_id]
        self.referee = self.rng.choice(league["referees"])
        self.match_id = f"{home_id}-{away_id}-{seed}"
        self.events: list[dict[str, Any]] = []
        self.score = {home_id: 0, away_id: 0}
        self.possession = home_id
        self.carrier = self.home["squad"][0]
        self.ball = (50.0, 50.0)
        self.pressure = 0.0
        self.clock = 0
        self.half = 1

    def squad(self, team: str) -> list[dict[str, Any]]:
        return self.home["squad"] if team == self.home["id"] else self.away["squad"]

    def other(self, team: str) -> str:
        return self.away["id"] if team == self.home["id"] else self.home["id"]

    def pick(self, team: str, weights: dict[str, int], exclude: dict[str, Any] | None = None) -> dict[str, Any]:
        pool = [p for p in self.squad(team) if p is not exclude]
        return self.rng.choices(pool, [weights[p["position"]] for p in pool])[0]

    def emit(self, event_type: str, team: str, player: dict[str, Any] | None,
             xy: tuple[float, float] | None, payload: dict[str, Any]) -> None:
        offset = self.clock + (HALF_TIME_BREAK if self.half == 2 else 0)
        self.events.append({
            "event_id": f"{self.match_id}-{len(self.events):05d}",
            "match_id": self.match_id,
            "timestamp": (self.kickoff + timedelta(seconds=offset)).isoformat().replace("+00:00", "Z"),
            "match_clock": f"{self.clock // 60:02d}:{self.clock % 60:02d}",
            "half": self.half,
            "event_type": event_type,
            "team": team,
            "player": _ref(player) if player else None,
            "coordinates": {"x": round(xy[0], 1), "y": round(xy[1], 1)} if xy else None,
            "payload": payload,
            "context": {"score": dict(self.score), "possession_team": self.possession,
                        "pressure_index": round(self.pressure, 2)},
        })

    def meta(self, status: str) -> None:
        payload: dict[str, Any] = {"status": status}
        if status == "kickoff":
            payload.update({
                "seed": self.seed,
                "home": {k: self.home[k] for k in ("id", "name", "short_name")},
                "away": {k: self.away[k] for k in ("id", "name", "short_name")},
                "stadium": self.home["stadium"],
                "referee": self.referee,
                "lineups": {c["id"]: [_ref(p) | {"number": p["number"]} for p in c["squad"]]
                            for c in (self.home, self.away)},
            })
        self.emit("match_metadata", self.home["id"], None, None, payload)

    def change_possession(self, team: str, player: dict[str, Any], xy: tuple[float, float], reason: str) -> None:
        old = self.possession
        self.possession, self.carrier, self.ball, self.pressure = team, player, xy, 0.0
        self.emit("possession_change", team, player, xy, {"from_team": old, "reason": reason})

    def kickoff_to(self, team: str, reason: str) -> None:
        self.change_possession(team, self.pick(team, {"GK": 0, "DF": 0, "MF": 1, "FW": 3}), (50.0, 50.0), reason)

    def run(self) -> list[dict[str, Any]]:
        self.meta("kickoff")
        for half in (1, 2):
            self.half, self.clock = half, (half - 1) * HALF_SECONDS
            end = half * HALF_SECONDS
            if half == 2:
                self.meta("second_half")
            self.kickoff_to(self.home["id"] if half == 1 else self.away["id"], "kickoff")
            while True:
                self.clock += self.rng.randint(3, 9)
                if self.clock >= end:
                    break
                self.step()
            self.clock = end
            self.meta("half_time" if half == 1 else "full_time")
        return self.events

    def step(self) -> None:
        """Play one on-ball action for the team in possession."""
        att, dfn = self.possession, self.other(self.possession)
        x, _ = self.ball
        self.pressure *= 0.9
        shot_chance = max(0.0, (x - 65) / 35 * 0.15)
        r = self.rng.random()
        if r < shot_chance:
            self.shot(att, dfn)
        elif r < shot_chance + 0.07:
            self.tackle(att, dfn)
        elif r < shot_chance + 0.17:
            self.press(att, dfn)
        elif r < shot_chance + 0.27:
            self.player_run(att)
        else:
            self.pass_ball(att, dfn)

    def pass_ball(self, att: str, dfn: str) -> None:
        x, y = self.ball
        nx, ny = _clamp(x + self.rng.gauss(6, 12)), _clamp(y + self.rng.gauss(0, 15))
        recipient = self.pick(att, RECEIVE_W[min(2, int(nx // 33.4))], exclude=self.carrier)
        length = _metres(x, y, nx, ny)
        p_complete = 0.93 - length / 150 - self.pressure * 0.15 - (0.08 if nx > 75 else 0)
        completed = self.rng.random() < p_complete
        self.emit("pass", att, self.carrier, (x, y), {
            "recipient": {"id": recipient["id"], "name": recipient["name"]},
            "end": {"x": round(nx, 1), "y": round(ny, 1)},
            "length_m": round(length, 1),
            "completed": completed,
        })
        if completed:
            self.carrier, self.ball = recipient, (nx, ny)
        else:
            self.change_possession(dfn, self.pick(dfn, DEFEND_W), (100 - nx, 100 - ny), "interception")

    def player_run(self, att: str) -> None:
        x, y = self.ball
        runner = self.carrier if self.carrier["position"] != "GK" else self.pick(att, RUN_W)
        nx, ny = _clamp(x + self.rng.uniform(3, 18)), _clamp(y + self.rng.gauss(0, 8))
        self.emit("player_run", att, runner, (x, y), {
            "end": {"x": round(nx, 1), "y": round(ny, 1)},
            "distance_m": round(_metres(x, y, nx, ny), 1),
            "with_ball": runner is self.carrier,
        })
        self.carrier, self.ball = runner, (nx, ny)

    def press(self, att: str, dfn: str) -> None:
        x, y = self.ball
        presser = self.pick(dfn, DEFEND_W)
        self.pressure = min(1.0, self.pressure + 0.25)
        won = self.rng.random() < 0.2 + self.pressure * 0.2
        self.emit("pressure_event", dfn, presser, (100 - x, 100 - y), {
            "target": {"id": self.carrier["id"], "name": self.carrier["name"]},
            "success": won,
        })
        if won:
            self.change_possession(dfn, presser, (100 - x, 100 - y), "pressure")

    def tackle(self, att: str, dfn: str) -> None:
        x, y = self.ball
        tackler = self.pick(dfn, DEFEND_W)
        self.pressure = min(1.0, self.pressure + 0.15)
        won = self.rng.random() < 0.6
        self.emit("tackle", dfn, tackler, (100 - x, 100 - y), {
            "opponent": {"id": self.carrier["id"], "name": self.carrier["name"]},
            "won": won,
        })
        if won:
            self.change_possession(dfn, tackler, (100 - x, 100 - y), "tackle")

    def shot(self, att: str, dfn: str) -> None:
        x, y = self.ball
        distance = _metres(x, y, 100, 50)
        xg = round(min(0.95, 0.7 * math.exp(-distance / 9)), 3)
        r = self.rng.random()
        outcome = ("goal" if r < xg else
                   self.rng.choices(["blocked", "saved", "off_target"], [25, 35, 40])[0])
        if outcome == "goal":
            self.score[att] += 1
        self.emit("shot", att, self.carrier, (x, y),
                  {"xg": xg, "distance_m": round(distance, 1), "outcome": outcome})
        keeper = self.squad(dfn)[0]
        if outcome == "goal":
            self.kickoff_to(dfn, "goal")
        elif outcome == "saved":
            self.change_possession(dfn, keeper, (6.0, 50.0), "save")
        elif outcome == "off_target":
            self.change_possession(dfn, keeper, (5.0, 50.0), "out_of_play")
        elif self.rng.random() < 0.5:  # blocked: rebound falls back to the attackers
            self.ball = (_clamp(x - 10), y)
        else:
            self.change_possession(dfn, self.pick(dfn, DEFEND_W), (100 - x, 100 - y), "block")


def simulate_match(home_id: str, away_id: str, seed: int,
                   league: dict[str, Any] | None = None, kickoff: datetime = KICKOFF) -> list[dict[str, Any]]:
    """Return every event of one match; the same arguments always give the same events."""
    return _Match(league or load_league(), home_id, away_id, seed, kickoff).run()


def clock_seconds(event: dict[str, Any]) -> int:
    """Seconds of match clock for an event's `match_clock`."""
    minutes, seconds = event["match_clock"].split(":")
    return int(minutes) * 60 + int(seconds)


def write_replay(events: Iterable[dict[str, Any]], out) -> None:
    """Write events as JSON lines to an open text stream."""
    for event in events:
        out.write(json.dumps(event, separators=(",", ":")) + "\n")


def send_events(events: list[dict[str, Any]], producer: Any, speed: float = 0.0) -> None:
    """Send events in order to an Event Hubs producer, paced at `speed` x real time (0 = as fast as possible)."""
    from azure.eventhub import EventData
    from azure.eventhub.exceptions import EventHubError

    # Paced sends go one event at a time so the stream plays live; unpaced sends go in batches.
    chunks = ([[e] for e in events] if speed > 0 else
              [events[i:i + BATCH_SIZE] for i in range(0, len(events), BATCH_SIZE)])
    previous = None
    for chunk in chunks:
        if speed > 0 and previous is not None:
            time.sleep(max(0, clock_seconds(chunk[0]) - clock_seconds(previous)) / speed)
        start = time.monotonic()
        try:
            # One partition key per match keeps its events in order.
            producer.send_batch([EventData(json.dumps(e)) for e in chunk], partition_key=chunk[0]["match_id"])
        except EventHubError:
            log.exception("stage=ingest event_id=%s send failed after SDK retries", chunk[0]["event_id"])
            raise
        log.info("stage=ingest event_id=%s count=%d duration_ms=%.0f fallback=no",
                 chunk[-1]["event_id"], len(chunk), (time.monotonic() - start) * 1000)
        previous = chunk[-1]


def main(argv: list[str] | None = None) -> None:
    """CLI: simulate a match, then print it, write a replay file, or stream it to Event Hubs."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--home", default="ASH", help="home club id from data/league.json")
    parser.add_argument("--away", default="CAL", help="away club id from data/league.json")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=Path, help="write a JSONL replay file here")
    parser.add_argument("--send", action="store_true", help="stream to Azure Event Hubs (reads .env)")
    parser.add_argument("--speed", type=float, default=0.0,
                        help="with --send: multiple of real time, e.g. 30 plays 90 minutes in 3 (0 = no pacing)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    try:
        events = simulate_match(args.home, args.away, args.seed)
    except ValueError as exc:
        parser.error(str(exc))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("w") as f:
            write_replay(events, f)
        log.info("wrote %d events to %s", len(events), args.out)
    if args.send:
        from azure.eventhub import EventHubProducerClient
        from dotenv import load_dotenv

        load_dotenv()
        conn, hub = os.environ.get("EVENTHUB_CONNECTION_STRING"), os.environ.get("EVENTHUB_NAME")
        if not conn or not hub:
            parser.error("--send needs EVENTHUB_CONNECTION_STRING and EVENTHUB_NAME in .env")
        with EventHubProducerClient.from_connection_string(conn, eventhub_name=hub) as producer:
            send_events(events, producer, args.speed)
    if not args.out and not args.send:
        write_replay(events, sys.stdout)


if __name__ == "__main__":
    main()
