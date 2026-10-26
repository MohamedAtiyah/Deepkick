"""Recap Agent: half-time and full-time recaps for three personas in one model call, plus the model-free catch-up."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from deepkick.agents.narrative import (_NUMBER, _SENTENCE_END, TIMEOUT_S, _leaves, ask_with_retry, check_numbers,
                                       client_from_env)
from deepkick.contracts import PERSONAS
from deepkick.simulator import clock_seconds

log = logging.getLogger("deepkick.recap")

RECAP_TYPES = ("half_time", "full_time")
SENTENCES = (3, 6)  # 6: the goals take a sentence before the voice's own numbers
TEAM_STATS = ("goals", "shots", "shots_on_target", "xg", "possession_pct", "pass_accuracy_pct", "tackles_won",
              "pressures_won")

SYSTEM_PROMPT = f"""You write the half-time or full-time recap of a football match for three viewers.

You get a JSON object with `recap` ("half_time" or "full_time"), `match_state` (score and team stats at that clock),
`goals` (every goal so far, in order: minute, team and scorer) and `moments` (every flagged moment so far: type,
match_clock, minute, team, player and facts).
Reply with one JSON object and nothing else, with exactly the keys "analyst", "casual" and "player".
Each is an object with exactly one key, "text": the recap, {SENTENCES[0]} to {SENTENCES[1]} sentences.

The three voices:
- analyst: the goals, then the numbers: expected goals, shots, shots on target, possession and pass accuracy. Blunt
  and direct.
- casual: warm and plain, the story of the half or the match, almost no jargon and at most three numbers besides the
  goal minutes.
- player: the finishers' view: the goals and the big chances, who took theirs and who did not.

Rules for all three:
- Every voice names every goal in `goals` with its scorer and its `minute` (for example "Rafe Ironsby scored in
  minute 14" or "Rafe Ironsby (14')"). A player's goals can share one mention: "Caius Mossington (20', 22', 33')".
  Leave out no goal, however many there are; the goals come before the other numbers.
- Cite only numbers that appear in the input, copied exactly. Never round, add, subtract or count anything yourself.
- Describe only what the facts show. Make no claims about fatigue, confidence, intent, form or effort.
- Plain broadcast English with no raw field names: say "possession", "shots on target", "expected goals".
- Write clubs and players exactly as given, full names with their spaces. Refer to a player by name or "they".
"""


def _minute(moment: dict[str, Any]) -> int:
    """The football minute of a moment: 13:52 on the clock is the 14th minute."""
    return clock_seconds(moment) // 60 + 1


def recap_inputs(moment: dict[str, Any], snapshot: dict[str, Any], moments: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the exact JSON the model sees: score and team stats at the recap's clock, the goals and moments so far."""
    at = clock_seconds(moment)
    so_far = [{"type": m["type"], "match_clock": m["match_clock"], "minute": _minute(m), "team": m["team"],
               "player": m["player"]["name"] if m["player"] else None, "facts": m["facts"]}
              for m in moments if clock_seconds(m) <= at]
    return {
        "recap": moment["type"],
        "match_state": {"match_clock": snapshot["match_clock"], "score": snapshot["score"],
                        "teams": {tid: {"name": t["name"]} | {k: t[k] for k in TEAM_STATS}
                                  for tid, t in snapshot["teams"].items()}},
        "goals": [{"minute": m["minute"], "team": m["team"], "scorer": m["player"]}
                  for m in so_far if m["type"] == "goal"],
        "moments": so_far,
    }


def goals(inputs: dict[str, Any]) -> list[tuple[str, int]]:
    """(scorer, minute) for every goal in the recap inputs, in match order."""
    return [(g["scorer"], g["minute"]) for g in inputs["goals"]]


def allowed(inputs: dict[str, Any]) -> set[float]:
    """Every number a recap may cite: the numeric values in the inputs plus the parts of every match clock."""
    numbers = {float(v) for _, v in _leaves(inputs)}
    clocks = [inputs["match_state"]["match_clock"]] + [m["match_clock"] for m in inputs["moments"]]
    numbers.update(float(n) for clock in clocks for n in _NUMBER.findall(clock))
    return numbers


def check_recap(persona: str, version: Any, inputs: dict[str, Any]) -> dict[str, str]:
    """Validate one persona's recap; raise ValueError on a bad shape, length, invented number or a missing goal."""
    if not isinstance(version, dict) or set(version) != {"text"} or not isinstance(version["text"], str):
        raise ValueError(f"{persona} must be an object with a text")
    text = version["text"].strip()
    sentences = len(_SENTENCE_END.findall(text))
    if not SENTENCES[0] <= sentences <= SENTENCES[1]:
        raise ValueError(f"{persona} must be {SENTENCES[0]}-{SENTENCES[1]} sentences, got {sentences}")
    check_numbers([text], allowed(inputs))
    cited = set(_NUMBER.findall(text))
    missing = [f"{scorer} {minute}'" for scorer, minute in goals(inputs)
               if scorer not in text or str(minute) not in cited]
    if missing:
        raise ValueError(f"{persona} leaves out goals: {missing}")
    return {"text": text}


def template_recap(inputs: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Recaps built from the facts alone: the score, every goal with scorer and minute, and three stats per persona."""
    state = inputs["match_state"]
    (home, h), (away, a) = state["score"].items()
    ht, at = state["teams"][home], state["teams"][away]
    hn, an = ht["name"], at["name"]
    whistle = "Half-time" if inputs["recap"] == "half_time" else "Full-time"
    scored = goals(inputs)
    opening = f"{whistle}: {hn} {h}-{a} {an}."
    listed = ", ".join(f"{scorer} ({minute}')" for scorer, minute in scored)
    goal_line = f"Goals: {listed}." if scored else "There have been no goals."
    lines = {
        "shots": f"Shots: {hn} {ht['shots']}, {an} {at['shots']}.",
        "shots_on_target": f"Shots on target: {hn} {ht['shots_on_target']}, {an} {at['shots_on_target']}.",
        "xg": f"Expected goals: {hn} {ht['xg']}, {an} {at['xg']}.",
        "possession_pct": f"Possession: {hn} {ht['possession_pct']}%, {an} {at['possession_pct']}%.",
        "pass_accuracy_pct": f"Pass accuracy: {hn} {ht['pass_accuracy_pct']}%, {an} {at['pass_accuracy_pct']}%.",
    }
    top = {"analyst": ("xg", "shots_on_target", "pass_accuracy_pct"), "casual": ("shots", "possession_pct", "xg"),
           "player": ("shots_on_target", "shots", "xg")}  # each persona's top three stats
    return {p: {"text": " ".join([opening, goal_line, *(lines[k] for k in top[p])]), "fallback": True}
            for p in PERSONAS}


class RecapAgent:
    """Writes the three persona recaps of a half or a match in one model call; never raises."""

    def __init__(self, client: Any | None = None, deployment: str | None = None, timeout_s: float = TIMEOUT_S) -> None:
        if client is None:
            client, deployment = client_from_env("recap") or (None, None)
        self.client, self.deployment, self.timeout_s = client, deployment, timeout_s

    def write(self, moment: dict[str, Any], snapshot: dict[str, Any],
              moments: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Return {analyst, casual, player}, each {text, fallback}, for a half_time or full_time moment."""
        start, event_id, result = time.monotonic(), moment.get("event_id"), None
        try:
            inputs = recap_inputs(moment, snapshot, moments)
            templates = template_recap(inputs)
            if self.client is not None:
                result = ask_with_retry(self.client, self.deployment, self.timeout_s, SYSTEM_PROMPT,
                                        json.dumps(inputs), lambda content: self._merge(content, inputs, templates,
                                                                                        event_id),
                                        "recap", event_id)
            result = result or templates
        except Exception:
            log.exception("stage=recap event_id=%s could not build a recap", event_id)
            result = {p: {"text": f"{moment['type'].replace('_', '-').capitalize()}.", "fallback": True}
                      for p in PERSONAS}
        sources = ",".join(f"{p}:{'template' if result[p]['fallback'] else 'model'}" for p in PERSONAS)
        log.info("stage=recap event_id=%s type=%s duration_ms=%.0f fallback=%s sources=%s", event_id, moment["type"],
                 (time.monotonic() - start) * 1000, "yes" if "template" in sources else "no", sources)
        return result

    @staticmethod
    def _merge(content: str, inputs: dict[str, Any], templates: dict[str, dict[str, Any]],
               event_id: str | None) -> dict[str, dict[str, Any]]:
        """Keep each recap that passes its checks and template the rest; raise if none passes so the call is retried."""
        try:
            raw = json.loads(content)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"malformed JSON: {exc}") from exc
        if not isinstance(raw, dict) or set(raw) != set(PERSONAS):
            raise ValueError(f"wrong keys: {sorted(raw) if isinstance(raw, dict) else type(raw).__name__}")
        out, failed = {}, {}
        for persona in PERSONAS:
            try:
                out[persona] = check_recap(persona, raw[persona], inputs) | {"fallback": False}
            except ValueError as exc:
                out[persona], failed[persona] = templates[persona], exc
        if len(failed) == len(PERSONAS):
            raise ValueError(f"every recap failed: {'; '.join(str(e) for e in failed.values())}")
        for persona, exc in failed.items():
            log.warning("stage=recap event_id=%s persona=%s fallback=yes: %s; rejected=%r", event_id, persona, exc,
                        raw[persona])
        return out


def select_recap(record: dict[str, Any], persona: str, lang: str) -> dict[str, Any]:
    """One stored recap for a viewer: the persona's text in `lang`, or in English (and `lang` "en") if it was not
    translated."""
    moment, versions = record["moment"], record["versions"]
    version = record.get("translations", {}).get(lang, {}).get(persona) if lang != "en" else None
    translated = version is not None and not version["fallback"]
    return {"recap_id": f"{moment['moment_id']}-{persona}-{lang}", "type": moment["type"],
            "match_clock": moment["match_clock"], "score": record["score"], "persona": persona,
            "lang": lang if translated else "en", "text": (version if translated else versions[persona])["text"],
            "source": "template" if versions[persona]["fallback"] else "model"}


def catch_up(timeline: list[dict[str, Any]], headlines: dict[str, str], at: str, minutes: int) -> dict[str, Any]:
    """What happened in the `minutes` before match clock `at`, from moments at or before `at` only; no model call.

    `timeline` holds every detected moment in match order with the score and momentum right after it;
    `headlines` maps a released moment's id to the viewer's one-line headline.
    """
    now = clock_seconds({"match_clock": at})
    since = max(0, now - minutes * 60)
    past = [entry for entry in timeline if clock_seconds(entry["moment"]) <= now]
    before = [entry for entry in past if clock_seconds(entry["moment"]) <= since]
    score_now = past[-1]["score"] if past else {}
    score_then = before[-1]["score"] if before else {team: 0 for team in score_now}
    momentum = past[-1]["momentum"] if past else {}
    leader = max(momentum, key=momentum.get) if momentum else None
    window = [entry["moment"] for entry in past if clock_seconds(entry["moment"]) > since]
    return {
        "at": at, "minutes": minutes, "from": f"{since // 60:02d}:{since % 60:02d}",
        "score": {"then": score_then, "now": score_now, "goals": sum(score_now.values()) - sum(score_then.values())},
        "momentum": {"team": leader, "value": momentum.get(leader)} if leader else None,
        "moments": [{"moment_id": m["moment_id"], "match_clock": m["match_clock"], "type": m["type"],
                     "headline": headlines[m["moment_id"]]} for m in window if m["moment_id"] in headlines][::-1],
    }
