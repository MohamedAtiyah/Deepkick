"""Persona Agent: rewrites a narrative for analyst, casual and player viewers, then picks one overlay per profile."""

from __future__ import annotations

import argparse
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from deepkick.agents.narrative import (_NUMBER, HEADLINE_MAX, POSITION_WORDS, TIMEOUT_S, _clip, allowed_numbers,
                                       ask_with_retry, check_numbers, client_from_env, spell_out_positions)
from deepkick.contracts import PERSONAS, validate_overlay
from deepkick.simulator import clock_seconds, load_league

log = logging.getLogger("deepkick.persona")

CLUB_NAMES = {c["id"]: c["name"] for c in load_league()["clubs"]}
# Overlay priority 1-5 by moment type: goals highest, the many distance milestones lowest.
OVERLAY_PRIORITY = {
    "goal": 5, "full_time": 4, "big_chance": 3, "momentum_swing": 3, "pressure_spell": 3, "half_time": 3,
    "high_press_win": 2, "speed_milestone": 2, "pass_milestone": 2, "distance_milestone": 1,
}
LOW_PRIORITY = 2  # at or below this, a moment for the viewer's non-favourite club is shown as priority 1
VERSION_KEYS = ("headline", "detail", "why")

SYSTEM_PROMPT = f"""You rewrite one football broadcast caption for three kinds of viewer.

You get a JSON object with `moment` (type, clock, team, player and `facts`) and `narrative` (the caption so far,
with the `evidence` numbers it used). Reply with one JSON object and nothing else, with exactly the keys
"analyst", "casual" and "player". Each is an object with exactly "headline", "detail" and "why".
- analyst: precise, and includes the key numbers from `narrative.evidence`.
- casual: plain words with no jargon or abbreviations (say "expected goals", not xG), and at most one number in total.
- player: centred on `moment.player`, with their full name exactly as given in the headline.
  If `moment.player` is null, centre it on `moment.team` instead.

Rules for all three:
- headline at most 10 words and {HEADLINE_MAX} characters; detail and why at most 30 words each.
- Cite only numbers that appear in `moment.facts` or `narrative.evidence`, copied exactly. Never round or calculate.
- Describe only what the facts show. Make no claims about fatigue, confidence, form or intent.
- Plain broadcast English with no raw field names: say "possession", "shots on target", "expected goals".
- Spell out position codes: {", ".join(f"{code} {words}" for code, words in POSITION_WORDS.items())}.
- Keep normal punctuation: a hyphen in scores such as 0-2 and apostrophes such as Calderbrook Athletic's.
- Write clubs and players exactly as given, full names with their spaces. Refer to a player by name or "they".
"""

_CASUAL_HEADLINE = {
    "goal": "Goal for {team}!", "full_time": "Full-time", "half_time": "Half-time", "big_chance": "So close for {who}",
    "momentum_swing": "{team} take control", "pressure_spell": "{team} pile on the pressure",
    "high_press_win": "{who} wins it back high up", "speed_milestone": "What a strike from {who}",
    "distance_milestone": "{who} keeps on running", "pass_milestone": "{who} keeps the ball moving",
}
_CASUAL_LINE = {
    "result": "The scoreboard tells the story.", "chaos": "That was a real chance.",
    "control": "One side is on top right now.", "pressure_shift": "The pressure is building.",
    "rhythm": "A pause in the action.", "milestone": "A personal landmark in this match.",
}


def persona_inputs(moment: dict[str, Any], narrative: dict[str, Any]) -> dict[str, Any]:
    """Build the exact JSON the model sees: the moment without ids, and the narrative with its evidence."""
    player = moment["player"]
    return {
        "moment": {
            "type": moment["type"], "match_clock": moment["match_clock"], "half": moment["half"],
            "team": CLUB_NAMES.get(moment["team"], moment["team"]) if moment["team"] else None,
            "player": {"name": player["name"], "position": POSITION_WORDS.get(player["position"], player["position"])}
            if player else None,
            "facts": moment["facts"],
        },
        "narrative": {k: narrative[k] for k in ("headline", "why_it_matters", "theme", "evidence")},
    }


def check_versions(content: str, inputs: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Parse and validate the three persona versions; raise ValueError on a bad shape or a number not in the inputs."""
    try:
        out = json.loads(content)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"malformed JSON: {exc}") from exc
    if not isinstance(out, dict) or set(out) != set(PERSONAS):
        raise ValueError(f"wrong keys: {sorted(out) if isinstance(out, dict) else type(out).__name__}")
    allowed = allowed_numbers(inputs)  # facts, evidence values, half and match clock; narrative text is not a source
    for persona in PERSONAS:
        version = out[persona]
        if not isinstance(version, dict) or set(version) != set(VERSION_KEYS) or not all(
                isinstance(version[k], str) and version[k].strip() for k in VERSION_KEYS):
            raise ValueError(f"{persona} must have non-empty headline, detail and why")
        if len(version["headline"]) > HEADLINE_MAX:
            raise ValueError(f"{persona} headline is over {HEADLINE_MAX} characters")
        check_numbers(version.values(), allowed)
    if len(_NUMBER.findall(" ".join(out["casual"].values()))) > 1:
        raise ValueError("casual version cites more than one number")
    player = inputs["moment"]["player"]
    if player and player["name"] not in out["player"]["headline"]:
        raise ValueError(f"player headline must name {player['name']}")
    return {persona: {"headline": _clip(spell_out_positions(out[persona]["headline"])),
                      "detail": spell_out_positions(out[persona]["detail"]),
                      "why": spell_out_positions(out[persona]["why"])} for persona in PERSONAS}


def _fact(stat: str, value: float) -> str:
    """One evidence item in words with its unit, e.g. "speed 129.6 km/h" for speed_kmh."""
    words = re.sub(r"\bxg\b", "xG", stat.replace(".", " ").replace("_", " "))
    for suffix, unit in ((" kmh", " km/h"), (" pct", "%"), (" km", " km"), (" m", " m")):
        if words.endswith(suffix):
            return f"{words[:-len(suffix)]} {value}{unit}"
    return f"{words} {value}"


def _at_most_one_number(text: str) -> bool:
    return len(_NUMBER.findall(text)) <= 1


def template_versions(moment: dict[str, Any], narrative: dict[str, Any]) -> dict[str, Any]:
    """Build all three versions from the narrative text and evidence, with `fallback: true`."""
    team = CLUB_NAMES.get(moment["team"], moment["team"]) if moment["team"] else "The match"
    who = moment["player"]["name"] if moment["player"] else team
    headline, why = narrative["headline"], narrative["why_it_matters"]
    numbers = ", ".join(_fact(e["stat"], e["value"]) for e in narrative["evidence"][:4])
    casual_line = _CASUAL_LINE.get(narrative["theme"], "A moment worth noting.")
    casual_headline = _CASUAL_HEADLINE.get(moment["type"], "Key moment").format(team=team, who=who)
    first_sentence = re.split(r"(?<=[.!?])\s", why)[0]
    if not moment["player"]:
        player_headline, player_why = headline, casual_line
    else:
        # The casual headline has no numbers, so clipping it can never cut a number in half.
        player_headline = headline if who in headline else _clip(f"{who}: {casual_headline}")
        player_why = f"{who} is at the centre of this {moment['type'].replace('_', ' ')}."
    return {
        "analyst": {"headline": headline, "detail": f"Key numbers: {numbers}." if numbers else why, "why": why},
        "casual": {"headline": casual_headline, "detail": casual_line,
                   "why": first_sentence if _at_most_one_number(first_sentence) else "It could shape what comes next."},
        "player": {"headline": player_headline, "detail": why, "why": player_why},
        "fallback": True,
    }


def involves(moment: dict[str, Any], club: str | None, player_id: str | None) -> bool:
    """True if the moment is about the viewer's favourite club or player (match-wide moments involve both clubs)."""
    if player_id and moment["player"] and moment["player"]["id"] == player_id:
        return True
    if not club:
        return False
    return moment["team"] == club if moment["team"] else club in moment["facts"].get("score", {})


def select_overlay(moment: dict[str, Any], versions: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    """Pick the profile's persona version and wrap it as a validated overlay; plain Python, no model."""
    club, player_id = profile.get("favourite_club"), profile.get("favourite_player")
    priority = OVERLAY_PRIORITY.get(moment["type"], 1)
    if involves(moment, club, player_id):
        priority = min(5, priority + 1)
    elif club and moment["team"] and moment["type"] != "goal" and priority <= LOW_PRIORITY:
        priority = 1  # a small moment for the other club; goals always keep their priority
    persona, lang = profile.get("persona"), profile.get("lang", "en")
    version = versions.get(persona) or {}
    overlay = {
        "overlay_id": f"{moment['moment_id']}-{persona}-{lang}",
        "type": moment["type"],
        "match_clock": moment["match_clock"],
        "show_at_ms": clock_seconds(moment) * 1000,
        "duration_ms": 8000 if moment["type"] == "goal" else 6000,
        "priority": priority,
        "persona": persona,
        "lang": lang,
        "payload": {"player": moment["player"]["name"] if moment["player"] else None,
                    **{k: version.get(k) for k in VERSION_KEYS}},
    }
    validate_overlay(overlay)
    return overlay


class PersonaAgent:
    """Makes all three persona versions of a moment in one model call, cached by moment id; never raises."""

    def __init__(self, client: Any | None = None, deployment: str | None = None, timeout_s: float = TIMEOUT_S) -> None:
        if client is None:
            client, deployment = client_from_env("persona") or (None, None)
        self.client, self.deployment, self.timeout_s = client, deployment, timeout_s
        self.cache: dict[str, dict[str, Any]] = {}

    def versions(self, moment: dict[str, Any], narrative: dict[str, Any]) -> dict[str, Any]:
        """Return {analyst, casual, player, fallback} for a moment, calling the model at most once per moment id."""
        start = time.monotonic()
        event_id, cached = moment.get("event_id"), moment.get("moment_id") in self.cache
        if not cached:
            result = None
            try:
                if self.client is not None:
                    inputs = persona_inputs(moment, narrative)
                    result = ask_with_retry(
                        self.client, self.deployment, self.timeout_s, SYSTEM_PROMPT, json.dumps(inputs),
                        lambda content: check_versions(content, inputs) | {"fallback": False}, "persona", event_id)
                if result is None:
                    result = template_versions(moment, narrative)
            except Exception:
                log.exception("stage=persona event_id=%s could not build persona versions", event_id)
                line = {"headline": "Key moment", "detail": "A key moment in the match.", "why": "It shapes the match."}
                result = {persona: dict(line) for persona in PERSONAS} | {"fallback": True}
            self.cache[moment["moment_id"]] = result
        result = self.cache[moment["moment_id"]]
        log.info("stage=persona event_id=%s duration_ms=%.0f fallback=%s cached=%s", event_id,
                 (time.monotonic() - start) * 1000, "yes" if result["fallback"] else "no", "yes" if cached else "no")
        return result

    def overlay(self, moment: dict[str, Any], narrative: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
        """Return the validated overlay for one viewer profile, reusing the cached versions."""
        return select_overlay(moment, self.versions(moment, narrative), profile)


def main(argv: list[str] | None = None) -> None:
    """CLI: replay a match, explain and personalise its moments, and print one overlay per moment as JSON lines."""
    from dotenv import load_dotenv

    from deepkick.agents.narrative import NarrativeAgent
    from deepkick.moments import MomentDetector
    from deepkick.stats import StatsAgent

    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("replay", type=Path, help="JSONL file written by `python -m deepkick.simulator --out`")
    parser.add_argument("--persona", choices=PERSONAS, default="casual")
    parser.add_argument("--club", help="favourite club code, e.g. ASH")
    parser.add_argument("--player", help="favourite player id, e.g. ASH09")
    parser.add_argument("--limit", type=int, default=8, help="stop after this many overlays (0 = all)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    load_dotenv()

    profile = {"persona": args.persona, "favourite_club": args.club, "favourite_player": args.player, "lang": "en"}
    stats, detector, narrator, personas, done = StatsAgent(), MomentDetector(), NarrativeAgent(), PersonaAgent(), 0
    with args.replay.open() as f:
        for line in f:
            event = json.loads(line)
            if not stats.update(event):
                continue
            snapshot = stats.snapshot()
            for moment in detector.detect(event, snapshot):
                print(json.dumps(personas.overlay(moment, narrator.explain(moment, snapshot), profile)))
                done += 1
                if done == args.limit:
                    return


if __name__ == "__main__":
    main()
