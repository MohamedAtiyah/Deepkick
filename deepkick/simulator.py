"""Seeded football match simulator that writes replays or streams events to Azure Event Hubs.

Plays the fixture in data/league.json. Coordinates are metres on a 105 x 68 pitch, oriented so
the acting team always attacks towards x=105 (the same frame as the formation slots).
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
LENGTH, WIDTH = 105.0, 68.0
CENTRE = (LENGTH / 2, WIDTH / 2)
BLOCK_SHIFT = 0.4  # how far outfield players follow the ball up or down the pitch from their slot

log = logging.getLogger("deepkick.simulator")


def load_league(path: Path = LEAGUE_PATH) -> dict[str, Any]:
    """Load the fictional league data."""
    return json.loads(path.read_text())


def _clamp(x: float, y: float) -> tuple[float, float]:
    return max(1.0, min(LENGTH - 1, x)), max(1.0, min(WIDTH - 1, y))


def _flip(xy: tuple[float, float]) -> tuple[float, float]:
    """Same point seen from the other team's attacking direction."""
    return LENGTH - xy[0], WIDTH - xy[1]


def _ref(player: dict[str, Any]) -> dict[str, Any]:
    return {"id": player["id"], "name": player["name"], "position": player["position"]}


def _xy(xy: tuple[float, float]) -> dict[str, float]:
    return {"x": round(xy[0], 1), "y": round(xy[1], 1)}


class _Match:
    """Mutable state of one simulated match; `run()` returns its events."""

    def __init__(self, league: dict[str, Any], seed: int, kickoff: datetime):
        clubs = {c["id"]: c for c in league["clubs"]}
        match = league["match"]
        home_id, away_id = match["home"], match["away"]
        if home_id == away_id or home_id not in clubs or away_id not in clubs:
            raise ValueError(f"match needs two different club ids from {sorted(clubs)}, got {home_id!r}, {away_id!r}")
        self.rng = random.Random(seed)
        self.seed, self.kickoff = seed, kickoff
        self.home, self.away = clubs[home_id], clubs[away_id]
        self.stadium, self.referee = match["stadium"], match["referee"]
        self.match_id = f"{home_id}-{away_id}-{seed}"
        self.events: list[dict[str, Any]] = []
        self.score = {home_id: 0, away_id: 0}
        self.possession = home_id
        self.carrier = self.home["starters"][0]
        self.ball = CENTRE
        self.pressure = 0.0
        self.clock = 0
        self.half = 1

    def club(self, team: str) -> dict[str, Any]:
        return self.home if team == self.home["id"] else self.away

    def other(self, team: str) -> str:
        return self.away["id"] if team == self.home["id"] else self.home["id"]

    def keeper(self, team: str) -> dict[str, Any]:
        return next(p for p in self.club(team)["starters"] if p["position"] == "GK")

    def pick_near(self, team: str, xy: tuple[float, float], exclude: dict[str, Any] | None = None,
                  outfield: bool = False) -> dict[str, Any]:
        """Pick a starter of `team`, favouring those whose slot is near `xy` (in that team's frame)."""
        club = self.club(team)
        pool = [p for p in club["starters"]
                if p is not exclude and not (outfield and p["position"] == "GK")]
        weights = []
        for p in pool:
            slot = club["formation"]["slots"][p["position"]]
            sx = slot["x"] if p["position"] == "GK" else slot["x"] + (xy[0] - CENTRE[0]) * BLOCK_SHIFT
            weights.append(1 / (1 + (math.hypot(sx - xy[0], slot["y"] - xy[1]) / 12) ** 2))
        return self.rng.choices(pool, weights)[0]

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
            "coordinates": _xy(xy) if xy else None,
            "payload": payload,
            "context": {"score": dict(self.score), "possession_team": self.possession,
                        "pressure_index": round(self.pressure, 2)},
        })

    def meta(self, status: str) -> None:
        payload: dict[str, Any] = {"status": status}
        if status == "kickoff":
            payload.update({
                "seed": self.seed,
                "home": {k: self.home[k] for k in ("id", "name", "short_name", "colours", "manager")},
                "away": {k: self.away[k] for k in ("id", "name", "short_name", "colours", "manager")},
                "stadium": self.stadium,
                "referee": self.referee,
                "lineups": {c["id"]: {
                    "formation": c["formation"]["name"],
                    "starters": [_ref(p) | {"number": p["number"],
                                            "slot": c["formation"]["slots"][p["position"]]} for p in c["starters"]],
                    "bench": [_ref(p) | {"number": p["number"]} for p in c["bench"]],
                } for c in (self.home, self.away)},
            })
        self.emit("match_metadata", self.home["id"], None, None, payload)

    def change_possession(self, team: str, player: dict[str, Any], xy: tuple[float, float], reason: str) -> None:
        old = self.possession
        self.possession, self.carrier, self.ball, self.pressure = team, player, xy, 0.0
        self.emit("possession_change", team, player, xy, {"from_team": old, "reason": reason})

    def kickoff_to(self, team: str, reason: str) -> None:
        self.change_possession(team, self.pick_near(team, CENTRE, outfield=True), CENTRE, reason)

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
        self.pressure *= 0.9
        shot_chance = max(0.0, (self.ball[0] - 68) / 37 * 0.15)
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
        end = _clamp(x + self.rng.gauss(6.3, 12.6), y + self.rng.gauss(0, 10.2))
        recipient = self.pick_near(att, end, exclude=self.carrier, outfield=end[0] > 35)
        length = math.dist(self.ball, end)
        p_complete = 0.93 - length / 150 - self.pressure * 0.15 - (0.08 if end[0] > 79 else 0)
        completed = self.rng.random() < p_complete
        self.emit("pass", att, self.carrier, self.ball, {
            "recipient": {"id": recipient["id"], "name": recipient["name"]},
            "end": _xy(end),
            "length_m": round(length, 1),
            "completed": completed,
        })
        if completed:
            self.carrier, self.ball = recipient, end
        else:
            won_at = _flip(end)
            self.change_possession(dfn, self.pick_near(dfn, won_at), won_at, "interception")

    def player_run(self, att: str) -> None:
        x, y = self.ball
        runner = self.carrier if self.carrier["position"] != "GK" else self.pick_near(att, self.ball, outfield=True)
        end = _clamp(x + self.rng.uniform(3, 19), y + self.rng.gauss(0, 5.4))
        self.emit("player_run", att, runner, self.ball, {
            "end": _xy(end),
            "distance_m": round(math.dist(self.ball, end), 1),
            "with_ball": runner is self.carrier,
        })
        self.carrier, self.ball = runner, end

    def press(self, att: str, dfn: str) -> None:
        at = _flip(self.ball)
        presser = self.pick_near(dfn, at)
        self.pressure = min(1.0, self.pressure + 0.25)
        won = self.rng.random() < 0.2 + self.pressure * 0.2
        self.emit("pressure_event", dfn, presser, at, {
            "target": {"id": self.carrier["id"], "name": self.carrier["name"]},
            "success": won,
        })
        if won:
            self.change_possession(dfn, presser, at, "pressure")

    def tackle(self, att: str, dfn: str) -> None:
        at = _flip(self.ball)
        tackler = self.pick_near(dfn, at)
        self.pressure = min(1.0, self.pressure + 0.15)
        won = self.rng.random() < 0.6
        self.emit("tackle", dfn, tackler, at, {
            "opponent": {"id": self.carrier["id"], "name": self.carrier["name"]},
            "won": won,
        })
        if won:
            self.change_possession(dfn, tackler, at, "tackle")

    def shot(self, att: str, dfn: str) -> None:
        x, y = self.ball
        distance = math.dist(self.ball, (LENGTH, CENTRE[1]))
        xg = round(min(0.95, 0.7 * math.exp(-distance / 9)), 3)
        r = self.rng.random()
        outcome = ("goal" if r < xg else
                   self.rng.choices(["blocked", "saved", "off_target"], [25, 35, 40])[0])
        if outcome == "goal":
            self.score[att] += 1
        self.emit("shot", att, self.carrier, self.ball,
                  {"xg": xg, "distance_m": round(distance, 1), "outcome": outcome})
        if outcome == "goal":
            self.kickoff_to(dfn, "goal")
        elif outcome == "saved":
            self.change_possession(dfn, self.keeper(dfn), (6.0, CENTRE[1]), "save")
        elif outcome == "off_target":
            self.change_possession(dfn, self.keeper(dfn), (5.5, CENTRE[1]), "out_of_play")
        elif self.rng.random() < 0.5:  # blocked: rebound falls back to the attackers
            self.ball = _clamp(x - 10.5, y)
        else:
            won_at = _flip(self.ball)
            self.change_possession(dfn, self.pick_near(dfn, won_at), won_at, "block")


def simulate_match(seed: int, league: dict[str, Any] | None = None, kickoff: datetime = KICKOFF) -> list[dict[str, Any]]:
    """Return every event of the league file's fixture; the same seed always gives the same events."""
    return _Match(league or load_league(), seed, kickoff).run()


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
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=Path, help="write a JSONL replay file here")
    parser.add_argument("--send", action="store_true", help="stream to Azure Event Hubs (reads .env)")
    parser.add_argument("--speed", type=float, default=0.0,
                        help="with --send: multiple of real time, e.g. 30 plays 90 minutes in 3 (0 = no pacing)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    try:
        events = simulate_match(args.seed)
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
