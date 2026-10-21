"""Narrative Agent: explains why a flagged moment matters with a Foundry chat model, falling back to a template."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit

from deepkick.simulator import load_league

log = logging.getLogger("deepkick.narrative")

THEMES = ("control", "chaos", "pressure_shift", "rhythm", "milestone", "result")
HEADLINE_MAX = 70
TIMEOUT_S = 30.0  # per attempt; reasoning models such as gpt-5-mini need well over 4 s on real prompts
REASONING_EFFORT = "low"
MAX_COMPLETION_TOKENS = 1500  # reasoning tokens count against this, so it is well above the reply's length
ATTEMPTS = 2  # the first call plus one retry
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_SENTENCE_END = re.compile(r"[.!?](?:\s|$)")
_PROJECT_PATH = re.compile(r"^/api/projects/[^/]+")
POSITION_WORDS = {
    "GK": "goalkeeper", "RB": "right-back", "LB": "left-back", "CB": "centre-back", "RCB": "right centre-back",
    "LCB": "left centre-back", "CDM": "defensive midfielder", "CM": "central midfielder",
    "RCM": "right central midfielder", "LCM": "left central midfielder", "RM": "right midfielder",
    "LM": "left midfielder", "RW": "right winger", "LW": "left winger", "ST": "striker",
    "RST": "right-sided striker", "LST": "left-sided striker",
}
# Only the codes the league actually uses, as whole words, so club codes such as ASH and CAL are never touched.
LEAGUE_POSITIONS = sorted({p["position"] for c in load_league()["clubs"] for p in c["starters"] + c["bench"]})
_POSITION_CODE = re.compile(r"\b(" + "|".join(LEAGUE_POSITIONS) + r")\b")

SYSTEM_PROMPT = f"""You are a football broadcast analyst. You explain why one flagged match moment matters.

You get a JSON object with `moment` (what happened, with its `facts`) and `match_state` (the live stats).
Reply with one JSON object and nothing else, with exactly these keys:
- "headline": at most 10 words and {HEADLINE_MAX} characters.
- "why_it_matters": one or two sentences, at most 30 words in total.
- "evidence": at least one item, each {{"stat": <key>, "value": <its number>}}, for the facts you relied on.
  `stat` must be a key of `moment.facts` exactly as written there, such as "xg" or "distance_m";
  for a nested fact, join the keys with dots, such as "score.CAL". `value` must be that fact's number.

Numbers:
- Cite only numbers that appear in `moment` or `match_state`, copied exactly as written there.
  Do not round, add, subtract, convert units or count anything yourself. If a number is not in the input, leave it out.
- Write numbers as digits only when you are quoting one of those input numbers.

Language:
- Write the headline and why_it_matters in plain broadcast English, never raw field names: say "possession",
  "shots on target", "expected goals" and "pass accuracy", not possession_pct, shots_on_target, team_xg
  or pass_accuracy_pct.
- Spell out position codes: {", ".join(f"{code} {words}" for code, words in POSITION_WORDS.items())}.
- Keep normal punctuation: a hyphen in scores such as 0-2 and apostrophes such as Calderbrook Athletic's.
- Describe only what the facts show. Make no claims about fatigue, confidence, form or intent.
- Name clubs and players exactly as given in the input. Refer to a player by name or with "they", never "he" or "she".
"""

THEME_BY_TYPE = {
    "goal": "result", "full_time": "result", "half_time": "rhythm", "big_chance": "chaos",
    "pressure_spell": "pressure_shift", "high_press_win": "pressure_shift", "momentum_swing": "control",
    "speed_milestone": "milestone", "distance_milestone": "milestone", "pass_milestone": "milestone",
}
_GOAL_PHRASE = {"opener": "opens the scoring", "equaliser": "levels it", "go_ahead": "puts {team} ahead",
                "extends_lead": "extends the lead", "pulls_back": "pulls one back"}
_OUTCOME = {"goal": "scored", "saved": "saved", "blocked": "blocked", "off_target": "off target"}


def _leaves(value: Any, path: str = "") -> Iterator[tuple[str, float]]:
    """Yield (dotted path, number) for every numeric leaf of a JSON-like value."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _leaves(item, f"{path}.{key}" if path else str(key))
    elif isinstance(value, list):
        for i, item in enumerate(value):
            yield from _leaves(item, f"{path}.{i}")
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        yield path, value


def model_inputs(moment: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    """Build the exact JSON the model sees: the moment without ids and a trimmed snapshot."""
    player = moment["player"]
    state = {k: snapshot[k] for k in ("match_clock", "half", "status", "score", "possession_team", "pressure_index",
                                      "momentum_window_minutes", "teams") if k in snapshot}
    if player and player["id"] in snapshot.get("players", {}):
        state["player"] = snapshot["players"][player["id"]]
    return {
        "moment": {
            "type": moment["type"], "match_clock": moment["match_clock"], "half": moment["half"],
            "team": moment["team"],
            "player": {"name": player["name"], "position": player["position"]} if player else None,
            "facts": moment["facts"],
        },
        "match_state": state,
    }


def allowed_numbers(inputs: dict[str, Any]) -> set[float]:
    """Every number the narrative may cite: numeric values in the inputs plus the parts of the match clock."""
    numbers = {float(v) for _, v in _leaves(inputs)}
    numbers.update(float(n) for n in _NUMBER.findall(inputs["moment"]["match_clock"]))
    return numbers


def check_output(content: str, inputs: dict[str, Any]) -> dict[str, Any]:
    """Parse and validate a model reply; raise ValueError if its shape is wrong or it cites a number not in `inputs`."""
    try:
        out = json.loads(content)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"malformed JSON: {exc}") from exc
    # The theme is set in code from the moment type, so a stray "theme" from the model is ignored.
    if not isinstance(out, dict) or set(out) - {"theme"} != {"headline", "why_it_matters", "evidence"}:
        raise ValueError(f"wrong keys: {sorted(out) if isinstance(out, dict) else type(out).__name__}")
    headline, why, evidence = out["headline"], out["why_it_matters"], out["evidence"]
    if not isinstance(headline, str) or not headline.strip() or len(headline) > HEADLINE_MAX:
        raise ValueError(f"headline must be 1-{HEADLINE_MAX} characters")
    if not isinstance(why, str) or not why.strip() or len(_SENTENCE_END.findall(why.strip())) > 2:
        raise ValueError("why_it_matters must be one or two sentences")
    if not isinstance(evidence, list) or not all(
            isinstance(e, dict) and set(e) == {"stat", "value"} and isinstance(e["stat"], str)
            and isinstance(e["value"], (int, float)) and not isinstance(e["value"], bool) for e in evidence):
        raise ValueError("evidence must be a list of {stat, value} with numeric values")

    allowed = allowed_numbers(inputs)
    invented = [n for n in _NUMBER.findall(f"{headline} {why}") if float(n) not in allowed]
    invented += [str(e["value"]) for e in evidence if float(e["value"]) not in allowed]
    if invented:
        raise ValueError(f"numbers not in the inputs: {invented}")
    if not evidence:
        raise ValueError("evidence must not be empty")
    # Each evidence item must be a key of the moment facts ("xg", or "score.CAL" when nested) holding that value.
    facts = dict(_leaves(inputs["moment"]["facts"]))
    wrong = [e for e in evidence if e["stat"] not in facts or float(facts[e["stat"]]) != float(e["value"])]
    if wrong:
        raise ValueError(f"evidence does not match the moment facts: {wrong}")
    return {"headline": headline, "why_it_matters": why, "evidence": evidence}


def spell_out_positions(text: str) -> str:
    """Replace standalone league position codes (RCB, ST…) with words; club codes such as ASH are left alone."""
    return _POSITION_CODE.sub(lambda m: POSITION_WORDS.get(m.group(0), m.group(0)), text)


def theme_for(moment_type: str) -> str:
    """The narrative theme for a moment type; set in code, never chosen by the model."""
    return THEME_BY_TYPE.get(moment_type, "rhythm")


def _possessive(name: str) -> str:
    return name + ("'" if name.endswith("s") else "'s")


def _count(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _clip(text: str) -> str:
    return text if len(text) <= HEADLINE_MAX else text[:HEADLINE_MAX - 1].rstrip() + "…"


def template_narrative(moment: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    """Build a headline and why_it_matters straight from the moment facts and snapshot, with `fallback: true`."""
    f, kind = moment["facts"], moment["type"]
    teams = snapshot.get("teams", {})

    def name(tid: str) -> str:
        return teams.get(tid, {}).get("name") or tid

    team = name(moment["team"]) if moment["team"] else ""
    player = moment["player"]["name"] if moment["player"] else ""

    if kind in ("half_time", "full_time"):
        (home, h), (away, a) = f["score"].items()
        ht, at = f["teams"][home], f["teams"][away]
        headline = f"{'Half' if kind == 'half_time' else 'Full'}-time: {name(home)} {h}-{a} {name(away)}"
        why = (f"{name(home)} had {_count(ht['shots'], 'shot')} and {ht['xg']} xG to {_possessive(name(away))} "
               f"{at['shots']} and {at['xg']}. "
               f"Possession split {ht['possession_pct']}% to {at['possession_pct']}%.")
    elif kind == "goal":
        (home, h), (away, a) = f["score"].items()
        headline = f"GOAL! {player} {_GOAL_PHRASE[f['kind']].format(team=team)}: {home} {h}-{a} {away}"
        why = (f"{player} scored from {f['distance_m']} m with a {f['xg']} xG chance. "
               f"{team} now have {_count(f['team_shots'], 'shot')} worth {f['team_xg']} xG.")
    elif kind == "big_chance":
        headline = f"Big chance for {team}: {player}, {f['xg']} xG"
        why = (f"The shot from {f['distance_m']} m was {_OUTCOME[f['outcome']]}. "
               f"{team} have {_count(f['team_shots'], 'shot')} worth {f['team_xg']} xG.")
    elif kind == "pressure_spell":
        headline = f"{team} turn the screw: {f['shots']} shots in {f['window_minutes']} minutes"
        why = f"Those {f['shots']} shots are worth {f['xg']} xG between them."
    elif kind == "high_press_win":
        headline = f"{player} wins it back high up the pitch"
        why = (f"{player} pressed {f['target']} into a turnover {f['x_m']} m up the pitch. "
               f"{team} have won {_count(f['team_pressures_won'], 'pressure')} so far.")
    elif kind == "momentum_swing":
        m = f["momentum"][f["to_team"]]
        headline = f"Momentum swings to {name(f['to_team'])}"
        why = (f"{name(f['to_team'])} hold {m['value']}% of the momentum over the last {f['window_minutes']} "
               f"minutes, with {_count(m['shots'], 'shot')}, {m['final_third_entries']} final-third entries "
               f"and {m['possession_pct']}% of the ball.")
    elif kind == "speed_milestone":
        headline = f"{player} hits a {f['speed_kmh']} km/h strike"
        why = (f"The shot from {f['distance_m']} m was {_OUTCOME[f['outcome']]}. "
               f"{_possessive(team)} fastest shot so far is {f['team_top_speed_kmh']} km/h.")
    elif kind == "distance_milestone":
        headline = f"{player} passes {f['milestone_km']} km covered"
        why = f"{player} has run {f['distance_m']} m so far for {team}."
    elif kind == "pass_milestone":
        headline = f"{player} reaches {f['passes']} passes"
        why = (f"{f['passes_completed']} of those {f['passes']} passes found a teammate, "
               f"{f['pass_accuracy_pct']}% accuracy.")
    else:
        headline = f"{kind.replace('_', ' ').capitalize()} at {moment['match_clock']}"
        why = f"{team or 'The match'} moves on."
    return {"headline": _clip(headline), "why_it_matters": why, "theme": theme_for(kind),
            "evidence": [{"stat": path, "value": v} for path, v in _leaves(f)], "fallback": True}


def foundry_base_url(endpoint: str) -> str:
    """Return the `/openai/v1/` base URL for a Foundry endpoint pasted as a bare host, a project endpoint or a target URI.

    Keeps only scheme, host and an optional `/api/projects/<name>`, so any trailing `/openai/...`, `/responses`
    or query string is dropped instead of doubling the path.
    """
    parts = urlsplit(endpoint.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"FOUNDRY_ENDPOINT must be an http(s) URL, got {endpoint!r}")
    project = _PROJECT_PATH.match(parts.path)
    return f"{parts.scheme}://{parts.netloc}{project.group(0) if project else ''}/openai/v1/"


def _client_from_env() -> tuple[Any, str] | None:
    """Build an OpenAI client for the Foundry deployment from FOUNDRY_* environment variables, or None if unset."""
    endpoint, deployment, key = (os.environ.get(k) for k in
                                 ("FOUNDRY_ENDPOINT", "FOUNDRY_MODEL_DEPLOYMENT", "FOUNDRY_API_KEY"))
    if not (endpoint and deployment and key):
        log.warning("stage=narrative FOUNDRY_* settings missing; every narrative will use the template")
        return None
    try:
        base = foundry_base_url(endpoint)
    except ValueError as exc:
        log.warning("stage=narrative %s; every narrative will use the template", exc)
        return None
    from openai import OpenAI

    log.info("stage=narrative foundry base_path=%s deployment=%s", urlsplit(base).path, deployment)
    # The agent does its own single retry so each attempt is logged; the SDK's retries are off.
    return OpenAI(base_url=base, api_key=key, timeout=TIMEOUT_S, max_retries=0), deployment


class NarrativeAgent:
    """Turns a moment and the Stats Agent snapshot into a short explained narrative; never raises."""

    def __init__(self, client: Any | None = None, deployment: str | None = None, timeout_s: float = TIMEOUT_S) -> None:
        if client is None:
            client, deployment = _client_from_env() or (None, None)
        self.client, self.deployment, self.timeout_s = client, deployment, timeout_s

    def explain(self, moment: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
        """Return {headline, why_it_matters, theme, evidence, fallback} for one moment."""
        start = time.monotonic()
        event_id = moment.get("event_id") if isinstance(moment, dict) else None
        narrative = None
        try:
            if self.client is not None:
                narrative = self._ask_model(moment, snapshot)
            if narrative is None:
                narrative = template_narrative(moment, snapshot)
        except Exception:
            log.exception("stage=narrative event_id=%s could not build a narrative", event_id)
            narrative = {"headline": "Key moment", "why_it_matters": "A key moment in the match.",
                         "theme": "rhythm", "evidence": [], "fallback": True}
        log.info("stage=narrative event_id=%s duration_ms=%.0f fallback=%s",
                 event_id, (time.monotonic() - start) * 1000, "yes" if narrative["fallback"] else "no")
        return narrative

    def _ask_model(self, moment: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any] | None:
        """Call the model up to ATTEMPTS times; return a checked narrative, or None if every attempt failed."""
        inputs = model_inputs(moment, snapshot)
        messages = [{"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(inputs)}]
        for attempt in range(1, ATTEMPTS + 1):
            try:
                response = self.client.chat.completions.create(
                    model=self.deployment, messages=messages, response_format={"type": "json_object"},
                    reasoning_effort=REASONING_EFFORT, max_completion_tokens=MAX_COMPLETION_TOKENS,
                    timeout=self.timeout_s)
                checked = check_output(response.choices[0].message.content, inputs)
                return {"headline": _clip(spell_out_positions(checked["headline"])),
                        "why_it_matters": spell_out_positions(checked["why_it_matters"]),
                        "theme": theme_for(moment["type"]), "evidence": checked["evidence"], "fallback": False}
            except Exception as exc:  # timeouts, API errors and rejected replies all get the retry, then the template
                log.warning("stage=narrative event_id=%s attempt=%d failed: %s: %s",
                            moment["event_id"], attempt, type(exc).__name__, exc)
        return None


def main(argv: list[str] | None = None) -> None:
    """CLI: replay a match, explain its moments with the Narrative Agent and print them as JSON lines."""
    from dotenv import load_dotenv

    from deepkick.moments import MomentDetector
    from deepkick.stats import StatsAgent

    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("replay", type=Path, help="JSONL file written by `python -m deepkick.simulator --out`")
    parser.add_argument("--min-priority", type=int, default=2, help="skip moments below this priority")
    parser.add_argument("--limit", type=int, default=5, help="stop after this many moments (0 = all)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    load_dotenv()

    stats, detector, agent, done = StatsAgent(), MomentDetector(), NarrativeAgent(), 0
    with args.replay.open() as f:
        for line in f:
            event = json.loads(line)
            if not stats.update(event):
                continue
            snapshot = stats.snapshot()
            for moment in detector.detect(event, snapshot):
                if moment["priority"] < args.min_priority:
                    continue
                print(json.dumps({"moment_id": moment["moment_id"], **agent.explain(moment, snapshot)}))
                done += 1
                if done == args.limit:
                    return


if __name__ == "__main__":
    main()
