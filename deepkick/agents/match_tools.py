"""Match tools: deterministic, spoiler-safe lookups over the events and moments up to a match clock."""

from __future__ import annotations

import logging
import unicodedata
from typing import Any

from deepkick.simulator import clock_seconds, load_league
from deepkick.stats import StatsAgent

LEAGUE = load_league()
PLAYERS = {p["id"]: p | {"team": c["id"]} for c in LEAGUE["clubs"] for p in c["starters"] + c["bench"]}
CLUBS = {c["id"]: c for c in LEAGUE["clubs"]}
MAX_ROWS = 10


def _norm(text: str) -> str:
    """Case- and accent-insensitive form of a name: "Ébbe  Cauldwell" -> "ebbe cauldwell"."""
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return " ".join(plain.casefold().split())


def _is_run(event: dict[str, Any]) -> bool:
    """A real run, not a 5-minute tracking summary (a summary ends where it starts, at the player's slot)."""
    return event["event_type"] == "player_run" and event["payload"]["end"] != event["coordinates"]


def _who(player_id: str) -> dict[str, Any]:
    p = PLAYERS[player_id]
    return {"id": player_id, "name": p["name"], "team": p["team"], "position": p["position"], "number": p["number"]}


class MatchData:
    """Events and moments at or before `until` (default: everything given), and the Stats Agent snapshot there."""

    def __init__(self, events: list[dict[str, Any]], moments: list[dict[str, Any]], until: str | None = None) -> None:
        cut = clock_seconds({"match_clock": until}) if until else None
        self.events = [e for e in events if cut is None or clock_seconds(e) <= cut]
        self.moments = [m for m in moments if cut is None or clock_seconds(m) <= cut]
        agent = StatsAgent()
        quiet = logging.getLogger("deepkick.stats")
        level, quiet.level = quiet.level, logging.WARNING  # a replay of the past is not a handoff to log per event
        try:
            for event in self.events:
                agent.update(event)
        finally:
            quiet.setLevel(level)
        self.snapshot = agent.snapshot()
        self.clock = self.snapshot["match_clock"]

    def team_id(self, team: str | None) -> str | None:
        """A club id from an id, a name or part of one; None if nothing matches."""
        if not team:
            return None
        q = _norm(team)
        return next((cid for cid, c in CLUBS.items() if q in (_norm(cid), _norm(c["name"]), _norm(c["short_name"]))),
                    next((cid for cid, c in CLUBS.items() if q in _norm(c["name"])), None))


def find_player(data: MatchData, name: str) -> dict[str, Any]:
    """The league player a name means: full name, surname or first name, then any partial match; ties are candidates."""
    q = _norm(name)
    if not q:
        return {"error": "Give a player's name.", "data_until": data.clock}
    levels = [
        [pid for pid, p in PLAYERS.items() if _norm(p["name"]) == q],
        [pid for pid, p in PLAYERS.items() if q in _norm(p["name"]).split()],
        [pid for pid, p in PLAYERS.items() if q in _norm(p["name"])],
    ]
    found = next((level for level in levels if level), [])
    if len(found) == 1:
        return {"player": _who(found[0]), "data_until": data.clock}
    if found:
        return {"candidates": [_who(pid) for pid in found][:MAX_ROWS], "data_until": data.clock}
    return {"error": f"No player in this match is called {name!r}.", "data_until": data.clock}


def player_stats(data: MatchData, player_id: str) -> dict[str, Any]:
    """One player's numbers so far: passes, shots, goals, tackles, runs, distance, top speed, minutes, key moments."""
    if player_id not in PLAYERS:
        return {"error": f"Unknown player id {player_id!r}; use find_player first.", "data_until": data.clock}
    s = data.snapshot["players"].get(player_id, {})
    mine = [e for e in data.events if (e.get("player") or {}).get("id") == player_id]
    runs = [e for e in mine if _is_run(e)]
    actions = [e for e in mine if e["event_type"] != "player_run" or _is_run(e)]  # tracking summaries aren't play
    return {"player": _who(player_id), "data_until": data.clock,
            "passes_completed": s.get("passes_completed", 0), "passes_attempted": s.get("passes", 0),
            "pass_accuracy_pct": s.get("pass_accuracy_pct", 0.0), "shots": s.get("shots", 0),
            "goals": s.get("goals", 0), "xg": s.get("xg", 0.0), "tackles": s.get("tackles", 0),
            "tackles_won": s.get("tackles_won", 0), "pressures": s.get("pressures", 0),
            "pressures_won": s.get("pressures_won", 0), "runs": len(runs), "distance_m": s.get("distance_m", 0.0),
            "top_speed_kmh": max((e["payload"]["speed_kmh"] for e in runs), default=0.0),
            "minutes_involved": len({clock_seconds(e) // 60 for e in actions}),
            "key_moments": [{"match_clock": m["match_clock"], "type": m["type"]} for m in data.moments
                            if (m.get("player") or {}).get("id") == player_id][:MAX_ROWS]}


def top_sprints(data: MatchData, n: int = 5, team: str | None = None) -> dict[str, Any]:
    """The fastest runs so far, fastest first: player, team, speed_kmh, distance_m and match_clock."""
    tid = data.team_id(team)
    if team and tid is None:
        return {"error": f"No club called {team!r}.", "data_until": data.clock}
    runs = [e for e in data.events if _is_run(e) and (tid is None or e["team"] == tid)]
    runs.sort(key=lambda e: (-e["payload"]["speed_kmh"], clock_seconds(e)))
    return {"data_until": data.clock, "team": tid, "sprints": [
        {"player": e["player"]["name"], "team": e["team"], "speed_kmh": e["payload"]["speed_kmh"],
         "distance_m": e["payload"]["distance_m"], "match_clock": e["match_clock"]}
        for e in runs[:max(1, min(int(n), MAX_ROWS))]]}


def team_stats(data: MatchData, team: str) -> dict[str, Any]:
    """A club's numbers so far: score, shots, xG, possession, passing, pressing and its momentum share."""
    tid = data.team_id(team)
    if tid is None:
        return {"error": f"No club called {team!r}; the clubs are {', '.join(CLUBS)}.", "data_until": data.clock}
    t = data.snapshot["teams"].get(tid, {})
    other = next((cid for cid in data.snapshot["score"] if cid != tid), None)
    return {"team": tid, "name": CLUBS[tid]["name"], "data_until": data.clock,
            "goals": data.snapshot["score"].get(tid, 0), "conceded": data.snapshot["score"].get(other, 0),
            "shots": t.get("shots", 0), "shots_on_target": t.get("shots_on_target", 0), "xg": t.get("xg", 0.0),
            "possession_pct": t.get("possession_pct", 0.0), "passes": t.get("passes", 0),
            "pass_accuracy_pct": t.get("pass_accuracy_pct", 0.0), "pressures": t.get("pressures", 0),
            "pressures_won": t.get("pressures_won", 0), "tackles_won": t.get("tackles_won", 0),
            "momentum_share_last_5_min": t.get("momentum", {}).get("value", 0.0)}


def compare_players(data: MatchData, a: str, b: str) -> dict[str, Any]:
    """Two players' stats side by side (player ids, as find_player returns them)."""
    return {"a": player_stats(data, a), "b": player_stats(data, b), "data_until": data.clock}


def list_moments(data: MatchData, type: str | None = None) -> dict[str, Any]:  # noqa: A002 (the tool's own name)
    """The flagged moments so far, newest first, optionally of one type (goal, big_chance, momentum_swing…)."""
    kinds = sorted({m["type"] for m in data.moments})
    chosen = [m for m in data.moments if type in (None, m["type"])]
    if type and not chosen:
        return {"error": f"No {type!r} moments yet.", "types_so_far": kinds, "data_until": data.clock}
    return {"data_until": data.clock, "count": len(chosen), "moments": [
        {"match_clock": m["match_clock"], "type": m["type"], "team": m["team"],
         "player": m["player"]["name"] if m["player"] else None} for m in chosen[::-1][:MAX_ROWS * 2]]}


def goals(data: MatchData) -> dict[str, Any]:
    """Every goal so far, in order: minute, scorer, club and the score after it."""
    scored = [m for m in data.moments if m["type"] == "goal"]
    return {"data_until": data.clock, "score": data.snapshot["score"], "goals": [
        {"match_clock": m["match_clock"], "minute": clock_seconds(m) // 60 + 1, "team": m["team"],
         "scorer": m["player"]["name"], "score_after": m["facts"]["score"]} for m in scored]}


def shots(data: MatchData, team: str | None = None) -> dict[str, Any]:
    """Shots so far, newest first, with totals: player, xG, distance, outcome and ball speed."""
    tid = data.team_id(team)
    if team and tid is None:
        return {"error": f"No club called {team!r}.", "data_until": data.clock}
    taken = [e for e in data.events if e["event_type"] == "shot" and (tid is None or e["team"] == tid)]
    return {"data_until": data.clock, "team": tid, "total": len(taken),
            "on_target": sum(e["payload"]["outcome"] in ("goal", "saved") for e in taken),
            "goals": sum(e["payload"]["outcome"] == "goal" for e in taken),
            "xg": round(sum(e["payload"]["xg"] for e in taken), 2), "shots": [
                {"match_clock": e["match_clock"], "team": e["team"], "player": e["player"]["name"],
                 "xg": e["payload"]["xg"], "distance_m": e["payload"]["distance_m"],
                 "outcome": e["payload"]["outcome"], "speed_kmh": e["payload"]["speed_kmh"]}
                for e in taken[::-1][:MAX_ROWS * 2]]}


TOOLS = {f.__name__: f for f in (find_player, player_stats, top_sprints, team_stats, compare_players, list_moments,
                                 goals, shots)}


def run_tool(data: MatchData, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Call one tool by name with the model's arguments; a bad name or argument is an error result, never a raise."""
    tool = TOOLS.get(name)
    if tool is None:
        return {"error": f"Unknown tool {name!r}.", "data_until": data.clock}
    try:
        return tool(data, **arguments)
    except (TypeError, ValueError) as exc:
        return {"error": f"Bad arguments for {name}: {exc}", "data_until": data.clock}
