"""Persona Agent: rewrites a narrative for analyst, casual and player viewers, then picks one overlay per profile."""

from __future__ import annotations

import argparse
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from deepkick.agents.narrative import (_NUMBER, HEADLINE_MAX, POSITION_WORDS, TIMEOUT_S, _clip, _possessive,
                                       allowed_numbers, ask_with_retry, check_numbers, client_from_env,
                                       spell_out_positions)
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
CASUAL_MAX_NUMBERS = 2  # a score such as 1-0 counts as one number, a clock time such as 13:52 as none
_TIME = re.compile(r"\b\d{1,3}:\d{2}\b")
_SCORE = re.compile(r"\b\d+ ?[-–] ?\d+\b")

SYSTEM_PROMPT = f"""You rewrite one football broadcast caption for three kinds of viewer.

You get a JSON object with `moment` (type, clock, team, player and `facts`) and `narrative` (the caption so far,
with the `evidence` numbers it used). Reply with one JSON object and nothing else, with exactly the keys
"analyst", "casual" and "player". Each is an object with exactly "headline", "detail" and "why".
- analyst: precise, and includes the key numbers from `narrative.evidence`.
- casual: plain words with no jargon or abbreviations (say "expected goals", not xG), and at most two numbers in
  total. A score such as 1-0 counts as one number.
- player: centred on `moment.player`, with their full name exactly as given in the headline.
  If `moment.player` is null, centre it on `moment.team` instead.

Rules for all three:
- headline at most 10 words and {HEADLINE_MAX} characters; detail and why at most 30 words each.
- Cite only numbers that appear in `moment.facts` or `narrative.evidence`, copied exactly. Never round or calculate.
- Describe only what the facts show. Make no claims about fatigue, confidence, intent, form or effort.
- Never say one event caused another unless the facts say so.
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


def count_numbers(text: str) -> int:
    """Count the numbers a viewer reads: a score such as 1-0 is one number and a clock time such as 13:52 is none."""
    text = _TIME.sub(" ", text)
    return len(_SCORE.findall(text)) + len(_NUMBER.findall(_SCORE.sub(" ", text)))


def parse_versions(content: str) -> dict[str, Any]:
    """Parse a model reply into its three raw versions; raise ValueError if it is not JSON with exactly those keys."""
    try:
        out = json.loads(content)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"malformed JSON: {exc}") from exc
    if not isinstance(out, dict) or set(out) != set(PERSONAS):
        raise ValueError(f"wrong keys: {sorted(out) if isinstance(out, dict) else type(out).__name__}")
    return out


def check_version(persona: str, version: Any, inputs: dict[str, Any]) -> dict[str, str]:
    """Validate one persona version and return it cleaned; raise ValueError on a bad shape or an invented number."""
    if not isinstance(version, dict) or set(version) != set(VERSION_KEYS) or not all(
            isinstance(version[k], str) and version[k].strip() for k in VERSION_KEYS):
        raise ValueError(f"{persona} must have non-empty headline, detail and why")
    if len(version["headline"]) > HEADLINE_MAX:
        raise ValueError(f"{persona} headline is over {HEADLINE_MAX} characters")
    # Facts, evidence values, half and match clock; the narrative's own text is not a source.
    check_numbers(version.values(), allowed_numbers(inputs))
    if persona == "casual" and count_numbers(" ".join(version.values())) > CASUAL_MAX_NUMBERS:
        raise ValueError(f"casual version cites more than {CASUAL_MAX_NUMBERS} numbers")
    player = inputs["moment"]["player"]
    if persona == "player" and player and player["name"] not in version["headline"]:
        raise ValueError(f"player headline must name {player['name']}")
    return {"headline": _clip(spell_out_positions(version["headline"])),
            "detail": spell_out_positions(version["detail"]), "why": spell_out_positions(version["why"])}


_ORDINAL = ("first", "second", "third", "fourth", "fifth", "sixth")
# Plain words for each fact key; a key not listed here is left out of the template, never printed raw.
_FACT_WORDS = {
    "xg": "{v} xG", "team_xg": "{v} team xG", "distance_m": "{v} m", "team_shots": "{v} team shot{s}",
    "shots": "{v} shot{s}", "shots_on_target": "{v} on target", "window_minutes": "a {v}-minute window",
    "x_m": "{v} m up the pitch", "team_pressures_won": "{v} pressure{s} won", "speed_kmh": "{v} km/h",
    "player_top_speed_kmh": "{who_s} fastest {v} km/h", "team_top_speed_kmh": "{team_s} fastest {v} km/h",
    "milestone_km": "the {v} km mark", "passes": "{v} pass{es}", "passes_completed": "{v} completed",
    "pass_accuracy_pct": "{v}% pass accuracy", "possession_pct": "{v}% possession",
    "final_third_entries": "{v} final-third entr{ies}", "value": "{v}% of the momentum",
}


def key_facts(moment: dict[str, Any], evidence: list[dict[str, Any]], limit: int = 4) -> list[str]:
    """Evidence in plain words for the analyst template, most telling first.

    The score keeps both sides, unknown keys are skipped, and the score and the player's goal tally lead,
    then facts about the moment's own team, then the other club's.
    """
    facts = moment["facts"]
    team = CLUB_NAMES.get(moment["team"], moment["team"]) if moment["team"] else "The match"
    who = moment["player"]["name"] if moment["player"] else team
    ranked: list[tuple[int, str]] = []
    for item in evidence:
        parts, value = item["stat"].split("."), item["value"]
        if parts[0] == "score" and isinstance(facts.get("score"), dict) and len(facts["score"]) == 2:
            (home, h), (away, a) = facts["score"].items()
            phrase = f"{home} {h}-{a} {away}"
        elif parts[-1] == "player_goals":
            nth = _ORDINAL[value - 1] if 1 <= value <= len(_ORDINAL) else f"{value}th"
            phrase = f"{_possessive(who)} {nth} goal"
        elif parts[-1] in _FACT_WORDS:
            plural = {"s": "", "es": "", "ies": "y"} if value == 1 else {"s": "s", "es": "es", "ies": "ies"}
            phrase = _FACT_WORDS[parts[-1]].format(v=value, who_s=_possessive(who), team_s=_possessive(team), **plural)
            if len(parts) > 1:
                phrase = f"{parts[-2]} {phrase}"  # a per-club fact such as teams.ASH.shots -> "ASH 5 shots"
        else:
            continue
        lead = parts[0] == "score" or parts[-1] == "player_goals"
        other_club = len(parts) > 1 and parts[-2] != moment["team"]
        ranked.append((0 if lead else 2 if other_club else 1, phrase))
    out: list[str] = []
    for _, phrase in sorted(ranked, key=lambda r: r[0]):
        if phrase not in out:
            out.append(phrase)
    return out[:limit]


def template_versions(moment: dict[str, Any], narrative: dict[str, Any]) -> dict[str, Any]:
    """Build all three versions from the narrative text and evidence, each with `fallback: true`."""
    team = CLUB_NAMES.get(moment["team"], moment["team"]) if moment["team"] else "The match"
    who = moment["player"]["name"] if moment["player"] else team
    headline, why = narrative["headline"], narrative["why_it_matters"]
    numbers = ", ".join(key_facts(moment, narrative["evidence"]))
    casual_line = _CASUAL_LINE.get(narrative["theme"], "A moment worth noting.")
    casual_headline = _CASUAL_HEADLINE.get(moment["type"], "Key moment").format(team=team, who=who)
    first_sentence = re.split(r"(?<=[.!?])\s", why)[0]
    if not moment["player"]:
        player_headline, player_why = headline, casual_line
    else:
        # The casual headline has no numbers, so clipping it can never cut a number in half.
        player_headline = headline if who in headline else _clip(f"{who}: {casual_headline}")
        player_why = f"{who} is at the centre of this {moment['type'].replace('_', ' ')}."
    casual_why = (first_sentence if count_numbers(first_sentence) <= CASUAL_MAX_NUMBERS
                  else "It could shape what comes next.")
    return {
        "analyst": {"headline": headline, "detail": f"Key numbers: {numbers}." if numbers else why, "why": why,
                    "fallback": True},
        "casual": {"headline": casual_headline, "detail": casual_line, "why": casual_why, "fallback": True},
        "player": {"headline": player_headline, "detail": why, "why": player_why, "fallback": True},
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
        """Return {analyst, casual, player}, each with its own `fallback`, calling the model at most once per moment."""
        start = time.monotonic()
        event_id, cached = moment.get("event_id"), moment.get("moment_id") in self.cache
        if not cached:
            result = None
            try:
                if self.client is not None:
                    inputs = persona_inputs(moment, narrative)
                    result = ask_with_retry(
                        self.client, self.deployment, self.timeout_s, SYSTEM_PROMPT, json.dumps(inputs),
                        lambda content: self._merge(content, inputs, moment, narrative), "persona", event_id)
                if result is None:
                    result = template_versions(moment, narrative)
            except Exception:
                log.exception("stage=persona event_id=%s could not build persona versions", event_id)
                line = {"headline": "Key moment", "detail": "A key moment in the match.", "why": "It shapes the match.",
                        "fallback": True}
                result = {persona: dict(line) for persona in PERSONAS}
            self.cache[moment["moment_id"]] = result
        result = self.cache[moment["moment_id"]]
        templated = [p for p in PERSONAS if result[p]["fallback"]]
        log.info("stage=persona event_id=%s duration_ms=%.0f fallback=%s templated=%s cached=%s", event_id,
                 (time.monotonic() - start) * 1000, "yes" if templated else "no", ",".join(templated) or "none",
                 "yes" if cached else "no")
        return result

    def _merge(self, content: str, inputs: dict[str, Any], moment: dict[str, Any],
               narrative: dict[str, Any]) -> dict[str, Any]:
        """Keep each version that passes its checks and template the rest; raise if none passes so the call is retried.

        A retry for one failed version would be a whole new model call (up to the 30 s timeout), so it gets its
        template instead.
        """
        raw, templates, out, failed = parse_versions(content), template_versions(moment, narrative), {}, {}
        for persona in PERSONAS:
            try:
                out[persona] = check_version(persona, raw[persona], inputs) | {"fallback": False}
            except ValueError as exc:
                out[persona], failed[persona] = templates[persona], exc
        if len(failed) == len(PERSONAS):
            raise ValueError(f"every version failed: {'; '.join(str(e) for e in failed.values())}")
        for persona, exc in failed.items():
            log.warning("stage=persona event_id=%s persona=%s fallback=yes: %s", moment["event_id"], persona, exc)
        return out

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
