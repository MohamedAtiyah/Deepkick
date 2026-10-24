"""Persona Agent: rewrites a narrative for analyst, casual and player viewers, then picks one overlay per profile."""

from __future__ import annotations

import argparse
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from deepkick.agents.narrative import (_OUTCOME, HEADLINE_MAX, POSITION_WORDS, TIMEOUT_S, _clip, _count, _leaves,
                                       _possessive, allowed_numbers, ask_with_retry, check_numbers, client_from_env,
                                       spell_out_positions, template_narrative)
from deepkick.contracts import PERSONAS, validate_overlay
from deepkick.simulator import clock_seconds, load_league

log = logging.getLogger("deepkick.persona")

CLUB_NAMES = {c["id"]: c["name"] for c in load_league()["clubs"]}
# Overlay priority 1-5 by moment type: goals highest, the many distance milestones lowest.
OVERLAY_PRIORITY = {
    "goal": 5, "full_time": 4, "big_chance": 3, "momentum_swing": 3, "pressure_spell": 3, "half_time": 3,
    "high_press_win": 2, "speed_milestone": 2, "sprint_milestone": 2, "pass_milestone": 2, "distance_milestone": 1,
}
LOW_PRIORITY = 2  # at or below this, a moment for the viewer's non-favourite club is shown as priority 1
VERSION_KEYS = ("headline", "detail", "why")
MIN_SENTENCE_WORDS = 4
# shortcut: a verb lexicon instead of a parser; captions use a narrow football vocabulary and the prompt asks
# for full sentences. Upgrade to a part-of-speech tagger if real verbs get rejected.
_VERB = re.compile(
    r"\b(?:is|are|was|were|be|been|being|has|have|had|do|does|did|can|could|will|would|should|must|"
    r"(?:it|that|there|what)'s|(?!speed\b)[a-z]{3,}ed|[a-z]{3,}ing|lead|leads|win|wins|won|trail|trails|"
    r"hold|holds|held|keep|keeps|kept|give|gives|gave|put|puts|come|comes|came|go|goes|went|get|gets|got|"
    r"take|takes|took|make|makes|made|find|finds|found|hit|hits|beat|beats|scores|shoots|saves|stops|misses|"
    r"fires|slots|buries|reaches|covers|presses|sprints|pulls|levels|finishes|ends|needs|adds|counts|runs|ran|"
    r"sees|saw|tells|shows|stands|sits|lets|says|said|led|lost|struck|drew|drove|broke|chose|threw|stood|"
    r"sent|set|wrote|knew|thought|began|became)\b", re.I)

SYSTEM_PROMPT = f"""You rewrite one football broadcast caption for three viewers, each in an unmistakable
voice of its own.

You get a JSON object with `moment` (type, clock, team, player and `facts`) and `narrative` (the caption so far,
with the `evidence` numbers it used). Reply with one JSON object and nothing else, with exactly the keys
"analyst", "casual" and "player". Each is an object with exactly "headline", "detail" and "why".

The three voices:
- analyst: reads the game only through what is measured: expected goals, shot counts, shot distance,
  possession, pass accuracy, momentum and speed. Blunt and direct, mildly exasperated when the numbers show a side
  falling short, and always cites the one number from `narrative.evidence` that proves the point.
- casual: an attacker's eye for flair and space. Notices the run before the pass, the first touch, the angle.
  Warm, economical and admiring, in the present tense. Almost no jargon (say "expected goals", not xG) and at most
  two numbers.
- player: the penalty-box finisher. Only what the forward sees and feels: the yard of space, the angle, the distance
  from goal, whether that was a chance they should have taken. Third person but close in. Ruthless about a miss,
  delighted by a clean finish. Centred on `moment.player`, with their full name exactly as given in the headline.
  If `moment.player` is null, centre it on `moment.team` instead.

The facts hold no player positions, keeper position or defensive shape. Frame the facts in your voice, but never
state that a player was out of position or switched off, that a line broke, or where the keeper was, unless a fact
says so. Never use shape words such as cover, line, screening or second ball to explain why something happened.

Rules for all three:
- headline at most 10 words and {HEADLINE_MAX} characters; detail and why at most 30 words each.
- detail and why are each a full sentence of at least 4 words with a verb, never a bare number or a fragment.
- Cite only numbers that appear in `moment.facts` or `narrative.evidence`, copied exactly. Never round or calculate.
- Describe only what the facts show. Make no claims about fatigue, confidence, intent, form or effort.
- Never say one event caused another unless the facts say so.
- A shot speed (shot_speed_kmh) is the ball's speed and a shot distance (shot_distance_m) is the distance from goal.
  Never describe either as a player running. Only run_speed_kmh and run_distance_m describe a player's run.
- Plain broadcast English with no raw field names: say "possession", "shots on target", "expected goals".
- Spell out position codes: {", ".join(f"{code} {words}" for code, words in POSITION_WORDS.items())}.
- Keep normal punctuation: a hyphen in scores such as 0-2 and apostrophes such as Calderbrook Athletic's.
- Write clubs and players exactly as given, full names with their spaces. Refer to a player by name or "they".

Worked examples to match (fictional players):

A goal. Rafe Ironsby, striker, scores for Calderbrook Athletic. Facts: xg 0.252, distance_m 9.2, team_shots 2,
score ASH 0 CAL 1, player_goals 1.
{{"analyst": {{"headline": "0.252 xG from 9.2 m, and Rafe Ironsby scores",
             "detail": "Calderbrook have 2 shots, 0.29 xG and a 1-0 lead.",
             "why": "A 0.252 xG chance is converted, and it is ASH 0-1 CAL."}},
 "casual": {{"headline": "Rafe Ironsby slots it home, Calderbrook lead 1-0",
            "detail": "It is a calm, clean finish from close range.",
            "why": "It is Rafe Ironsby's first goal of the match."}},
 "player": {{"headline": "Rafe Ironsby, 9.2 m out, makes no mistake",
            "detail": "Rafe Ironsby has a 0.252 xG chance from 9.2 m and puts it away.",
            "why": "It is a clean finish, and Rafe Ironsby's first goal of the match."}}}}

A saved big chance. Soren Fallowmere, left-back, shoots for Ashvale Wanderers. Facts: xg 0.215, distance_m 10.6,
outcome saved, team_shots 4, team_xg 0.28.
{{"analyst": {{"headline": "0.215 xG from 10.6 m, and the keeper saves",
             "detail": "Ashvale now have 4 shots worth 0.28 xG in total.",
             "why": "That is a 0.215 xG chance saved, with nothing to show for it."}},
 "casual": {{"headline": "Soren Fallowmere lets fly and the keeper says no",
            "detail": "A crisp shot from the left-back, saved from 10.6 m out.",
            "why": "Ashvale keep getting shots away."}},
 "player": {{"headline": "Soren Fallowmere should have done better from 10.6 m",
            "detail": "Soren Fallowmere has a 0.215 xG chance at 10.6 m and the keeper saves it.",
            "why": "A 0.215 xG chance, and the keeper wins this one."}}}}
"""

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
    for key in ("detail", "why"):
        if len(version[key].split()) < MIN_SENTENCE_WORDS or not _VERB.search(version[key]):
            raise ValueError(f"{persona} {key} must be a sentence of at least {MIN_SENTENCE_WORDS} words with a "
                             f"verb, got {version[key]!r}")
    # Facts, evidence values, half and match clock; the narrative's own text is not a source.
    check_numbers(version.values(), allowed_numbers(inputs))
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
    "x_m": "{v} m up the pitch", "team_pressures_won": "{v} pressure{s} won",
    "shot_speed_kmh": "a {v} km/h shot", "shot_distance_m": "{v} m from goal",
    "player_top_shot_speed_kmh": "{who_s} fastest shot {v} km/h",
    "team_top_shot_speed_kmh": "{team_s} fastest shot {v} km/h",
    "run_speed_kmh": "a {v} km/h run", "run_distance_m": "a {v} m run",
    "team_top_run_speed_kmh": "{team_s} fastest run {v} km/h",
    "milestone_km": "the {v} km mark", "passes": "{v} pass{es}", "passes_completed": "{v} completed",
    "pass_accuracy_pct": "{v}% pass accuracy", "possession_pct": "{v}% possession",
    "final_third_entries": "{v} final-third entr{ies}", "value": "{v}% of the momentum",
}


# Readable labels for the "How we know" panel, by dotted fact key; {club} stands for a club code and is shown as the
# club's short name. "<moment type>:<key>" overrides a key whose meaning depends on the moment.
FACT_LABELS = {
    "xg": "Expected goals", "team_xg": "Team expected goals", "distance_m": "Shot distance (m)",
    "distance_milestone:distance_m": "Distance covered (m)", "milestone_km": "Milestone (km)",
    "team_shots": "Team shots", "shots": "Shots in the spell", "window_minutes": "Window (minutes)",
    "x_m": "Ball won (m up the pitch)", "team_pressures_won": "Team pressures won", "player_goals": "Goals this match",
    "shot_speed_kmh": "Shot speed (km/h)", "shot_distance_m": "Shot distance (m)",
    "player_top_shot_speed_kmh": "Player's fastest shot (km/h)",
    "team_top_shot_speed_kmh": "Team's fastest shot (km/h)",
    "run_speed_kmh": "Run speed (km/h)", "run_distance_m": "Run distance (m)",
    "team_top_run_speed_kmh": "Team's fastest run (km/h)",
    "passes": "Passes", "passes_completed": "Passes completed", "pass_accuracy_pct": "Pass accuracy (%)",
    "score.{club}": "{club} goals",
    "momentum.{club}.value": "{club} momentum", "momentum.{club}.shots": "{club} shots in the spell",
    "momentum.{club}.xg": "{club} expected goals in the spell",
    "momentum.{club}.possession_pct": "{club} possession in the spell (%)",
    "momentum.{club}.final_third_entries": "{club} final-third entries",
    "teams.{club}.shots": "{club} shots", "teams.{club}.shots_on_target": "{club} shots on target",
    "teams.{club}.xg": "{club} expected goals", "teams.{club}.possession_pct": "{club} possession (%)",
    "teams.{club}.pass_accuracy_pct": "{club} pass accuracy (%)",
}


def fact_label(stat: str, moment_type: str) -> str:
    """The readable label for a dotted fact key, such as momentum.CAL.value -> "Calderbrook momentum"."""
    club = next((part for part in stat.split(".") if part in CLUB_NAMES), None)
    key = ".".join("{club}" if part == club else part for part in stat.split("."))
    label = FACT_LABELS.get(f"{moment_type}:{key}") or FACT_LABELS.get(key)
    if label is None:
        log.warning("stage=persona no label for fact %s; showing the key", stat)
        return stat
    return label.format(club=_short(club)) if club else label


def cited(moment: dict[str, Any], stats: list[str]) -> list[dict[str, Any]]:
    """Evidence items {stat, label, value} for the numeric facts named in `stats`, values copied from the facts."""
    facts = dict(_leaves(moment["facts"]))
    return [{"stat": stat, "label": fact_label(stat, moment["type"]), "value": facts[stat]}
            for stat in dict.fromkeys(stats) if stat in facts]


class _Reads(dict):
    """Moment facts that note the dotted path of every value read, so a template's evidence is exactly what it used."""

    def __init__(self, data: dict[str, Any], seen: list[str], path: str = "") -> None:
        super().__init__(data)
        self._seen, self._path = seen, path

    def _note(self, key: str, value: Any) -> Any:
        path = f"{self._path}.{key}" if self._path else str(key)
        if isinstance(value, dict):
            return _Reads(value, self._seen, path)
        self._seen.append(path)
        return value

    def __getitem__(self, key: str) -> Any:
        return self._note(key, super().__getitem__(key))

    def get(self, key: str, default: Any = None) -> Any:
        return self[key] if key in self else default

    def items(self) -> list[tuple[str, Any]]:  # type: ignore[override]
        return [(key, self._note(key, value)) for key, value in super().items()]


def key_facts(moment: dict[str, Any], evidence: list[dict[str, Any]], limit: int = 4,
              used: list[str] | None = None) -> list[str]:
    """Evidence in plain words for the analyst template, most telling first.

    The score keeps both sides, unknown keys are skipped, and the score and the player's goal tally lead,
    then facts about the moment's own team, then the other club's. The stats behind the phrases returned are
    added to `used`.
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
        ranked.append((0 if lead else 2 if other_club else 1, phrase, item["stat"]))
    out: list[str] = []
    stats: dict[str, list[str]] = {}
    for _, phrase, stat in sorted(ranked, key=lambda r: r[0]):
        stats.setdefault(phrase, []).append(stat)  # both sides of a score share one phrase
        if phrase not in out:
            out.append(phrase)
    if used is not None:
        used.extend(stat for phrase in out[:limit] for stat in stats[phrase])
    return out[:limit]


def _short(team_id: str) -> str:
    return CLUB_NAMES.get(team_id, team_id).split()[0]  # "Calderbrook" for Calderbrook Athletic


def casual_template(moment: dict[str, Any]) -> dict[str, str]:
    """A plain-words casual version built only from the moment facts, naming the key fact of each moment type."""
    f, kind = moment["facts"], moment["type"]
    team = CLUB_NAMES.get(moment["team"], moment["team"]) if moment["team"] else ""
    who = moment["player"]["name"] if moment["player"] else team
    if kind in ("goal", "half_time", "full_time"):
        (home, h), (away, a) = f["score"].items()
    if kind == "goal":
        us, them = (home, away) if moment["team"] == home else (away, home)
        gf, ga = f["score"][us], f["score"][them]
        state = (f"{_short(us)} lead {gf}-{ga}" if gf > ga else f"it's {gf}-{ga}" if gf == ga
                 else f"{_short(us)} trail {gf}-{ga}")
        nth = _ORDINAL[f["player_goals"] - 1] if 1 <= f["player_goals"] <= len(_ORDINAL) else f"{f['player_goals']}th"
        return {"headline": f"{who} scores, {state}", "detail": f"Finished from {f['distance_m']} metres out.",
                "why": f"It's {_possessive(who)} {nth} goal of the match."}
    if kind in ("half_time", "full_time"):
        ht, at = f["teams"][home]["shots"], f["teams"][away]["shots"]
        leader = home if h > a else away if a > h else None
        if kind == "half_time":
            why = f"{CLUB_NAMES[leader]} lead at the break." if leader else "It's all square at the break."
        else:
            why = f"{CLUB_NAMES[leader]} win the match." if leader else "It ends all square."
        whistle = "Half-time" if kind == "half_time" else "Full-time"
        return {"headline": f"{whistle}: {_short(home)} {h}-{a} {_short(away)}",
                # Two plain clauses rather than "13 shots to Calderbrook's 17", which the Translator garbles.
                "detail": f"{_short(home)} had {_count(ht, 'shot')} and {_short(away)} had {_count(at, 'shot')}.",
                "why": why}
    if kind == "big_chance":
        return {"headline": f"Big chance for {who}",
                "detail": f"The shot from {f['distance_m']} metres was {_OUTCOME[f['outcome']]}.",
                "why": f"{team} have had {_count(f['team_shots'], 'shot')} so far."}
    if kind == "pressure_spell":
        return {"headline": f"{team} pile on the pressure",
                "detail": f"{team} have {_count(f['shots'], 'shot')} in {f['window_minutes']} minutes.",
                "why": f"The latest came from {who}."}
    if kind == "high_press_win":
        return {"headline": f"{who} wins the ball back high up",
                "detail": f"{who} took it off {f['target']} {f['x_m']} metres up the pitch.",
                "why": f"{team} have won the ball back {_count(f['team_pressures_won'], 'time')} by pressing."}
    if kind == "momentum_swing":
        to, m = CLUB_NAMES[f["to_team"]], f["momentum"][f["to_team"]]
        return {"headline": f"{to} take control",
                "detail": f"{to} have had {m['possession_pct']}% of the ball "
                          f"in the last {f['window_minutes']} minutes.",
                "why": f"They have {_count(m['shots'], 'shot')} and {m['final_third_entries']} final-third entries "
                       f"in that spell."}
    if kind == "speed_milestone":
        top = f["team_top_shot_speed_kmh"]
        fastest = (f"That is {_possessive(team)} fastest shot so far." if top == f["shot_speed_kmh"]
                   else f"{_possessive(team)} fastest shot so far is {top} km/h.")
        return {"headline": f"{who} fires a shot at {f['shot_speed_kmh']} km/h",
                "detail": f"From {f['shot_distance_m']} metres out, it was {_OUTCOME[f['outcome']]}.", "why": fastest}
    if kind == "sprint_milestone":
        top = f["team_top_run_speed_kmh"]
        fastest = (f"That is {_possessive(team)} fastest run so far." if top == f["run_speed_kmh"]
                   else f"{_possessive(team)} fastest run so far is {top} km/h.")
        return {"headline": f"{who} sprints at {f['run_speed_kmh']} km/h",
                "detail": f"{who} hit that speed on a {f['run_distance_m']} metre run.", "why": fastest}
    if kind == "distance_milestone":
        return {"headline": f"{who} passes {f['milestone_km']} km",
                "detail": f"{who} has covered {f['distance_m']} metres so far.",
                "why": f"That's after {int(moment['match_clock'].split(':')[0])} minutes."}
    if kind == "pass_milestone":
        return {"headline": f"{who} reaches {f['passes']} passes",
                "detail": f"{f['passes_completed']} of them found a teammate.",
                "why": f"That is {f['pass_accuracy_pct']}% accuracy."}
    return {"headline": f"{kind.replace('_', ' ').capitalize()} at {moment['match_clock']}",
            "detail": f"{team or 'The match'} moves on from here.", "why": f"{who or 'The match'} is involved in this."}


def analyst_headline(moment: dict[str, Any]) -> str:
    """A precise analyst headline built from the moment facts, carrying the moment's key number."""
    f, kind = moment["facts"], moment["type"]
    team = CLUB_NAMES.get(moment["team"], moment["team"]) if moment["team"] else ""
    who = moment["player"]["name"] if moment["player"] else team
    if kind in ("half_time", "full_time"):
        (home, h), (away, a) = f["score"].items()
        whistle = "HT" if kind == "half_time" else "FT"
        return f"{whistle} {home} {h}-{a} {away}, xG {f['teams'][home]['xg']} to {f['teams'][away]['xg']}"
    if kind == "momentum_swing":
        return f"{CLUB_NAMES[f['to_team']]} take {f['momentum'][f['to_team']]['value']}% of the momentum"
    if kind == "goal":
        return f"{who} scores: {f['xg']} xG from {f['distance_m']} m"
    if kind == "big_chance":
        return f"Big chance: {f['xg']} xG for {who}"
    if kind == "pressure_spell":
        return f"{team}: {f['shots']} shots, {f['xg']} xG in {f['window_minutes']} minutes"
    if kind == "high_press_win":
        return f"{who} wins the ball {f['x_m']} m up the pitch"
    if kind == "speed_milestone":
        return f"{f['shot_speed_kmh']} km/h shot from {f['shot_distance_m']} m"
    if kind == "sprint_milestone":
        return f"{f['run_speed_kmh']} km/h over a {f['run_distance_m']} m run"
    if kind == "distance_milestone":
        return f"{who} reaches {f['distance_m']} m covered"
    if kind == "pass_milestone":
        return f"{who}: {f['passes']} passes at {f['pass_accuracy_pct']}% accuracy"
    return f"{kind.replace('_', ' ').capitalize()} at {moment['match_clock']}"


def player_template(moment: dict[str, Any]) -> dict[str, str]:
    """A player-focus version built from the moment facts, around the player's name (or the club's, if none)."""
    f, kind, clock = moment["facts"], moment["type"], moment["match_clock"]
    team = CLUB_NAMES.get(moment["team"], moment["team"]) if moment["team"] else ""
    who = moment["player"]["name"] if moment["player"] else team
    if kind in ("half_time", "full_time"):
        (home, _), (away, _) = f["score"].items()
        hn, an, ht, at = CLUB_NAMES[home], CLUB_NAMES[away], f["teams"][home], f["teams"][away]
        return {"headline": f"{hn} v {an} {'at the break' if kind == 'half_time' else 'at the final whistle'}",
                "detail": f"{hn} had {ht['shots_on_target']} shots on target to "
                          f"{_possessive(an)} {at['shots_on_target']}.",
                "why": f"{hn} had {ht['possession_pct']}% of the ball."}
    if kind == "momentum_swing":
        to, m = CLUB_NAMES[f["to_team"]], f["momentum"][f["to_team"]]
        return {"headline": f"Momentum with {to}",
                "detail": f"{to} made {m['final_third_entries']} final-third entries in {f['window_minutes']} minutes.",
                "why": f"{to} had {_count(m['shots'], 'shot')} in that spell."}
    if kind == "goal":
        nth = _ORDINAL[f["player_goals"] - 1] if 1 <= f["player_goals"] <= len(_ORDINAL) else f"{f['player_goals']}th"
        return {"headline": f"{_possessive(who)} {nth} goal of the match",
                "detail": f"{who} finished a {f['xg']} xG chance from {f['distance_m']} m.",
                "why": f"{who} now has {_count(f['player_goals'], 'goal')} in this match."}
    if kind == "big_chance":
        return {"headline": f"{who} goes close",
                "detail": f"{_possessive(who)} {f['xg']} xG chance from {f['distance_m']} m was "
                          f"{_OUTCOME[f['outcome']]}.",
                "why": f"{who} had the chance at {clock}."}
    if kind == "pressure_spell":
        return {"headline": f"{who} adds to {_possessive(team)} pressure",
                "detail": f"{who} took the latest of {_possessive(team)} {f['shots']} shots.",
                "why": f"{_possessive(who)} shot came at {clock}."}
    if kind == "high_press_win":
        return {"headline": f"{who} wins it high up the pitch",
                "detail": f"{who} won it back from {f['target']} {f['x_m']} m up the pitch.",
                "why": f"{who} pressed at {clock}."}
    if kind == "speed_milestone":
        return {"headline": f"{_possessive(who)} {f['shot_speed_kmh']} km/h shot",
                "detail": f"{_possessive(who)} shot from {f['shot_distance_m']} m was {_OUTCOME[f['outcome']]}.",
                "why": f"{_possessive(who)} fastest shot so far is {f['player_top_shot_speed_kmh']} km/h."}
    if kind == "sprint_milestone":
        return {"headline": f"{who} at full speed: {f['run_speed_kmh']} km/h",
                "detail": f"{who} covered {f['run_distance_m']} m on the run.",
                "why": f"{who} hit that speed at {clock}."}
    if kind == "distance_milestone":
        return {"headline": f"{_possessive(who)} {f['milestone_km']} km mark",
                "detail": f"{who} has run {f['distance_m']} m in this match.",
                "why": f"{who} reached it at {clock}."}
    if kind == "pass_milestone":
        return {"headline": f"{who}: {f['passes']} passes and counting",
                "detail": f"{who} has completed {f['passes_completed']} of {f['passes']} passes.",
                "why": f"{_possessive(who)} pass accuracy is {f['pass_accuracy_pct']}%."}
    return {"headline": f"{who or 'The match'} at {clock}", "detail": f"{who or 'The match'} is involved in this.",
            "why": f"It was logged at {clock}."}


def template_versions(moment: dict[str, Any]) -> dict[str, Any]:
    """Build all three versions from the moment facts alone, each with its own headline and `fallback: true`.

    Nothing comes from the narrative, which may be model-written, so a templated version is wholly template.
    Each version's `evidence` is the numeric facts its template read.
    """
    seen: dict[str, list[str]] = {p: [] for p in PERSONAS}
    reads = {p: moment | {"facts": _Reads(moment["facts"], seen[p])} for p in PERSONAS}
    analyst = reads["analyst"]
    narrative = template_narrative(analyst, {"teams": {cid: {"name": name} for cid, name in CLUB_NAMES.items()}})
    numbers = ", ".join(key_facts(analyst, narrative["evidence"], used=seen["analyst"]))
    why = narrative["why_it_matters"]
    headline = analyst_headline(analyst)
    casual, player = casual_template(reads["casual"]), player_template(reads["player"])
    return {
        "analyst": {"headline": _clip(headline), "detail": f"The key numbers are {numbers}." if numbers else why,
                    "why": why, "evidence": cited(moment, seen["analyst"]), "fallback": True},
        "casual": casual | {"headline": _clip(casual["headline"]), "evidence": cited(moment, seen["casual"]),
                            "fallback": True},
        # Player headlines start with the name, so clipping can never cut it.
        "player": player | {"headline": _clip(player["headline"]), "evidence": cited(moment, seen["player"]),
                            "fallback": True},
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
    english = version = versions.get(persona) or {}
    if lang != "en":  # the translated version, if the Translator step made one; else the English text ships
        version = versions.get("translations", {}).get(lang, {}).get(persona) or version
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
                    **{k: version.get(k) for k in VERSION_KEYS},
                    "evidence": [{"label": e["label"], "value": e["value"]}
                                 for e in version.get("evidence", english.get("evidence", []))],
                    "source": "template" if english.get("fallback", True) else "model"},
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
                    result = template_versions(moment)
            except Exception:
                log.exception("stage=persona event_id=%s could not build persona versions", event_id)
                line = {"headline": "Key moment", "detail": "A key moment in the match.", "why": "It shapes the match.",
                        "evidence": [], "fallback": True}
                result = {persona: dict(line) for persona in PERSONAS}
            self.cache[moment["moment_id"]] = result
        result = self.cache[moment["moment_id"]]
        sources = ",".join(f"{p}:{'template' if result[p]['fallback'] else 'model'}" for p in PERSONAS)
        log.info("stage=persona event_id=%s duration_ms=%.0f fallback=%s sources=%s cached=%s", event_id,
                 (time.monotonic() - start) * 1000, "yes" if "template" in sources else "no", sources,
                 "yes" if cached else "no")
        return result

    def _merge(self, content: str, inputs: dict[str, Any], moment: dict[str, Any],
               narrative: dict[str, Any]) -> dict[str, Any]:
        """Keep each version that passes its checks and template the rest; raise if none passes so the call is retried.

        A retry for one failed version would be a whole new model call (up to the 30 s timeout), so it gets its
        template instead.
        """
        raw, templates, out, failed = parse_versions(content), template_versions(moment), {}, {}
        for persona in PERSONAS:
            try:
                # The model version cites the narrative's evidence, whose stats check_output matched to the facts.
                out[persona] = check_version(persona, raw[persona], inputs) | {
                    "evidence": cited(moment, [e["stat"] for e in narrative["evidence"]]), "fallback": False}
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
