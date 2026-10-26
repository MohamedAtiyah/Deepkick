"""Tests for the Persona Agent and viewer selection with a mocked Foundry chat client and the seed-42 replay."""

import json
import re
import logging

import pytest

from deepkick.agents.narrative import TIMEOUT_S, _leaves, template_narrative
from deepkick.agents.persona import (FACT_LABELS, OVERLAY_PRIORITY, SYSTEM_PROMPT, PersonaAgent, check_version, cited,
                                     fact_label, key_facts, parse_versions, persona_inputs, select_overlay,
                                     template_versions)
from deepkick.contracts import PERSONAS, validate_overlay
from deepkick.moments import PRIORITY
from tests.conftest import FakeClient, timeout_error


@pytest.fixture(scope="module")
def narrated(detected):
    """(moment, template narrative) pairs for every moment in the replay."""
    return [(m, template_narrative(m, s)) for m, s in detected]


@pytest.fixture(scope="module")
def goal(narrated):
    return next((m, n) for m, n in narrated if m["type"] == "goal")


def _model_evidence(moment, narrative):
    """What a model-written version cites: the narrative's checked evidence, values copied from the facts."""
    return cited(moment, [e["stat"] for e in narrative["evidence"]])


def _of(narrated, kind, team=None):
    return next((m, n) for m, n in narrated if m["type"] == kind and team in (None, m["team"]))


def _good_reply(moment):
    name, f = moment["player"]["name"], moment["facts"]
    return json.dumps({
        "analyst": {"headline": f"{name} scores from {f['distance_m']} m",
                    "detail": f"It was a {f['xg']} xG chance, shot number {f['team_shots']} for Calderbrook Athletic.",
                    "why": "It makes the score ASH 0-1 CAL."},
        "casual": {"headline": f"{name} gives Calderbrook Athletic the lead",
                   "detail": "It is a close-range finish from the striker.",
                   "why": "It is the first goal of the match."},
        "player": {"headline": f"{name} opens the scoring", "detail": f"{name} finished from {f['distance_m']} m.",
                   "why": f"It is {name}'s first goal of the match."},
    })


def _profile(persona="analyst", club=None, player=None):
    return {"persona": persona, "favourite_club": club, "favourite_player": player, "lang": "en"}


def test_three_personas_come_from_one_cached_call(goal):
    moment, narrative = goal
    client = FakeClient(_good_reply(moment))
    agent = PersonaAgent(client, "gpt-5-mini")
    overlays = {p: agent.overlay(moment, narrative, _profile(p)) for p in PERSONAS}
    assert len(client.calls) == 1  # switching persona never calls the model again
    call = client.calls[0]
    assert call["timeout"] == TIMEOUT_S == 30.0 and call["model"] == "gpt-5-mini"
    assert json.loads(call["messages"][1]["content"]) == persona_inputs(moment, narrative)
    reply = json.loads(_good_reply(moment))
    for persona, overlay in overlays.items():
        validate_overlay(overlay)
        assert overlay["persona"] == persona and overlay["lang"] == "en"
        assert {k: overlay["payload"][k] for k in ("headline", "detail", "why")} == reply[persona]
        assert overlay["payload"]["player"] == moment["player"]["name"]
    assert not any(v["fallback"] for v in agent.versions(moment, narrative).values()) and len(client.calls) == 1


def test_overlay_timing_comes_from_the_match_clock(narrated):
    for moment, narrative in narrated:
        overlay = select_overlay(moment, template_versions(moment), _profile())
        minutes, seconds = moment["match_clock"].split(":")
        assert overlay["show_at_ms"] == (int(minutes) * 60 + int(seconds)) * 1000
        assert overlay["duration_ms"] == (8000 if moment["type"] == "goal" else 6000)
        assert overlay["overlay_id"] == f"{moment['moment_id']}-analyst-en"


def test_every_moment_type_has_an_overlay_priority():
    assert set(OVERLAY_PRIORITY) == set(PRIORITY)
    assert max(OVERLAY_PRIORITY, key=OVERLAY_PRIORITY.get) == "goal"


def test_favourite_club_or_player_adds_one_capped_at_five(narrated):
    moment, narrative = _of(narrated, "big_chance", "ASH")
    versions = template_versions(moment)
    base = OVERLAY_PRIORITY["big_chance"]
    assert select_overlay(moment, versions, _profile())["priority"] == base
    assert select_overlay(moment, versions, _profile(club="ASH"))["priority"] == base + 1
    assert select_overlay(moment, versions, _profile(player=moment["player"]["id"]))["priority"] == base + 1
    goal, goal_narrative = _of(narrated, "goal")
    assert select_overlay(goal, template_versions(goal), _profile(club=goal["team"]))["priority"] == 5


def test_match_wide_moments_involve_the_favourite_club(narrated):
    moment, narrative = _of(narrated, "full_time")
    versions = template_versions(moment)
    assert select_overlay(moment, versions, _profile(club="CAL"))["priority"] == OVERLAY_PRIORITY["full_time"] + 1


def test_small_moments_for_the_other_club_drop_to_one(narrated):
    moment, narrative = _of(narrated, "high_press_win", "CAL")
    versions = template_versions(moment)
    assert OVERLAY_PRIORITY["high_press_win"] > 1
    assert select_overlay(moment, versions, _profile(club="ASH"))["priority"] == 1
    big, big_narrative = _of(narrated, "big_chance", "CAL")  # above the low-priority line, so it keeps its priority
    assert select_overlay(big, template_versions(big),
                          _profile(club="ASH"))["priority"] == OVERLAY_PRIORITY["big_chance"]


@pytest.mark.parametrize("club", [None, "ASH", "CAL"])
@pytest.mark.parametrize("persona", PERSONAS)
def test_goals_are_never_dropped(narrated, persona, club):
    goals = [(m, n) for m, n in narrated if m["type"] == "goal"]
    assert len(goals) == 5
    for moment, narrative in goals:
        overlay = select_overlay(moment, template_versions(moment), _profile(persona, club, "ASH02"))
        assert overlay["priority"] == 5 and overlay["payload"]["headline"]


def test_timeout_falls_back_to_templates_after_one_retry(goal, caplog):
    moment, narrative = goal
    client = FakeClient(timeout_error(), timeout_error())
    with caplog.at_level(logging.INFO, logger="deepkick.persona"):
        versions = PersonaAgent(client, "m").versions(moment, narrative)
    assert versions == template_versions(moment)
    assert all(versions[p]["fallback"] is True for p in PERSONAS)
    assert len(client.calls) == 2
    assert "APITimeoutError" in caplog.text
    assert f"stage=persona event_id={moment['event_id']} duration_ms=" in caplog.text
    assert "fallback=yes sources=analyst:template,casual:template,player:template" in caplog.text


@pytest.mark.parametrize("persona, field, text, problem", [
    ("analyst", "detail", "It was a 0.9 xG chance, the best of the match.", "numbers not in the inputs"),
    ("analyst", "why", "0.215", "sentence of at least 4 words with a verb"),  # a bare number
    ("casual", "detail", "Big chance.", "sentence of at least 4 words with a verb"),  # a two-word fragment
    ("player", "why", "A 0.252 xG chance.", "sentence of at least 4 words with a verb"),  # four words, no verb
    ("player", "why", "It is Rafe Ironsby's 12th goal this season.", "numbers not in the inputs"),
    ("player", "headline", "A cool finish to open the scoring", "player headline must name"),
    ("casual", "headline", "", "non-empty"),
])
def test_check_rejects_invented_numbers_and_broken_versions(goal, persona, field, text, problem):
    moment, narrative = goal
    version = json.loads(_good_reply(moment))[persona] | {field: text}
    with pytest.raises(ValueError, match=problem):
        check_version(persona, version, persona_inputs(moment, narrative))


def test_one_failed_version_is_templated_alone(goal, caplog):
    moment, narrative = goal
    reply = json.loads(_good_reply(moment))
    reply["analyst"]["why"] = "That makes it 7 goals in 3 games."  # invented numbers
    client = FakeClient(json.dumps(reply))
    with caplog.at_level(logging.INFO, logger="deepkick.persona"):
        versions = PersonaAgent(client, "m").versions(moment, narrative)
    assert len(client.calls) == 1  # no retry: the other two versions are kept
    assert versions["analyst"] == template_versions(moment)["analyst"]
    for persona in ("casual", "player"):
        assert versions[persona] == reply[persona] | {"evidence": _model_evidence(moment, narrative), "fallback": False}
    assert f"event_id={moment['event_id']} persona=analyst fallback=yes: numbers not in the inputs" in caplog.text
    assert "fallback=yes sources=analyst:template,casual:model,player:model cached=no" in caplog.text


def test_a_templated_version_never_mixes_in_model_text(goal):
    # Regression for event 00043: the template used to reuse the (model-written) narrative's sentences.
    moment, narrative = goal
    model_narrative = narrative | {"headline": "Model headline from the narrative call",
                                   "why_it_matters": "Model sentence one. Model sentence two."}
    reply = json.loads(_good_reply(moment))
    reply["casual"]["why"] = "That makes it 7 goals in 3 games."  # fails, so casual is templated
    versions = PersonaAgent(FakeClient(json.dumps(reply)), "m").versions(moment, model_narrative)
    assert versions["casual"] == template_versions(moment)["casual"]
    for persona in PERSONAS:
        assert "Model" not in " ".join(template_versions(moment)[persona][k] for k in ("headline", "detail", "why"))
    # Model versions stay wholly model.
    assert versions["analyst"] == reply["analyst"] | {"evidence": _model_evidence(moment, model_narrative),
                                                      "fallback": False}


def test_every_version_failing_retries_then_falls_back(goal):
    moment, narrative = goal
    reply = json.loads(_good_reply(moment))
    for persona in PERSONAS:
        reply[persona]["why"] = "That makes it 7 goals in 3 games."
    client = FakeClient(json.dumps(reply), json.dumps(reply))
    versions = PersonaAgent(client, "m").versions(moment, narrative)
    assert len(client.calls) == 2 and versions == template_versions(moment)


@pytest.mark.parametrize("content", ["not json", "[]", json.dumps({"analyst": {}, "casual": {}})])
def test_a_reply_without_all_three_versions_is_retried(goal, content):
    moment, narrative = goal
    with pytest.raises(ValueError):
        parse_versions(content)
    client = FakeClient(content, _good_reply(moment))
    versions = PersonaAgent(client, "m").versions(moment, narrative)
    assert len(client.calls) == 2 and not any(v["fallback"] for v in versions.values())


def test_narrative_text_is_not_a_number_source(goal):
    moment, narrative = goal
    narrative = narrative | {"why_it_matters": "They now have 44 touches in the box."}
    version = json.loads(_good_reply(moment))["analyst"] | {"why": "They now have 44 touches in the box."}
    with pytest.raises(ValueError, match="numbers not in the inputs"):
        check_version("analyst", version, persona_inputs(moment, narrative))


def test_position_codes_in_replies_are_spelled_out(goal):
    moment, narrative = goal
    version = json.loads(_good_reply(moment))["casual"] | {"detail": "It is a close-range finish from the ST."}
    assert check_version("casual", version, persona_inputs(moment, narrative))["detail"] == \
        "It is a close-range finish from the striker."


def test_templates_pass_the_same_checks_for_every_moment(narrated):
    for moment, narrative in narrated:
        versions = template_versions(moment)
        for persona in PERSONAS:
            text = {k: versions[persona][k] for k in ("headline", "detail", "why")}
            check_version(persona, text, persona_inputs(moment, narrative))
            assert "_" not in " ".join(text.values()), (moment["moment_id"], persona)  # no raw field names
        assert len(versions["casual"]["headline"].split()) <= 10, moment["moment_id"]
        if moment["player"]:
            assert moment["player"]["name"] in versions["player"]["headline"]
        if "score" in moment["facts"]:  # the score keeps both sides
            (home, h), (away, a) = moment["facts"]["score"].items()
            assert f"{home} {h}-{a} {away}" in versions["analyst"]["detail"], moment["moment_id"]


def test_key_facts_use_plain_words_and_the_full_score(goal):
    moment, _ = goal
    evidence = [{"stat": "xg", "value": 0.252}, {"stat": "score.CAL", "value": 1}, {"stat": "player_goals", "value": 1},
                {"stat": "team_shots", "value": 1}, {"stat": "outcome_code", "value": 3}]
    assert key_facts(moment, evidence, limit=5) == ["ASH 0-1 CAL", "Rafe Ironsby's first goal", "0.252 xG",
                                                    "1 team shot"]  # unknown keys are skipped, never printed raw


def test_key_facts_put_the_moments_own_team_first(narrated):
    moment, narrative = _of(narrated, "momentum_swing")
    facts = key_facts(moment, narrative["evidence"])
    assert facts and all(f.startswith(moment["team"]) or "window" in f for f in facts)


def test_prompt_rules_on_causation_effort_and_casual_numbers():
    for phrase in ("Never say one event caused another unless the facts say so",
                   "no claims about fatigue, confidence, intent, form or effort", "at most\n  two numbers"):
        assert phrase in SYSTEM_PROMPT, phrase


def test_prompt_gives_each_persona_its_own_voice():
    for phrase in ("analyst: reads the game only through what is measured", "expected goals, shot counts, shot distance",
                   "possession, pass accuracy, momentum and speed", "Blunt and direct, mildly exasperated",
                   "cites the one number", "Never use shape words such as cover, line, screening or second ball",
                   "a full sentence of at least 4 words with a verb",
                   "casual: an attacker's eye for flair and space", "the run before the pass",
                   "Warm, economical and admiring, in the present tense",
                   "player: the penalty-box finisher", "the yard of space", "Third person but close in",
                   "Ruthless about a miss,\n  delighted by a clean finish",
                   "never\nstate that a player was out of position or switched off"):
        assert phrase in SYSTEM_PROMPT, phrase


def _prompt_examples():
    """The worked examples in the prompt, as the dicts the model is asked to match."""
    decoder, found, at = json.JSONDecoder(), [], 0
    while (at := SYSTEM_PROMPT.find('{"analyst"', at)) != -1:
        example, at = decoder.raw_decode(SYSTEM_PROMPT, at)
        found.append(example)
    return found


def test_prompt_examples_pass_the_same_checks_as_the_model(narrated):
    goal = next((m, n) for m, n in narrated if m["type"] == "goal")
    saved = next((m, n) for m, n in narrated if m["type"] == "big_chance" and m["facts"]["outcome"] == "saved")
    examples = _prompt_examples()
    assert len(examples) == 2
    for example, (moment, narrative) in zip(examples, (goal, saved)):
        assert moment["player"]["name"] in example["player"]["headline"]
        for persona in PERSONAS:
            check_version(persona, example[persona], persona_inputs(moment, narrative))  # numbers come from the facts
            assert len(example[persona]["headline"].split()) <= 10
            assert all(len(example[persona][k].split()) <= 30 for k in ("detail", "why"))
            text = " ".join(example[persona].values())
            assert not re.search(r"\b(he|she|his|her)\b", text, re.I), text


def test_prompt_names_only_fictional_league_players_and_clubs():
    from deepkick.simulator import load_league

    league = load_league()
    names = {p["name"] for c in league["clubs"] for p in c["starters"] + c["bench"]}
    examples = " ".join(json.dumps(e) for e in _prompt_examples())
    assert {"Rafe Ironsby", "Soren Fallowmere"} <= names
    capitalised_pairs = set(re.findall(r"\b[A-Z][a-z]+ [A-Z][a-z]+\b", examples))
    clubs = {c["name"] for c in league["clubs"]}
    assert capitalised_pairs <= names | clubs, capitalised_pairs - names - clubs


FILLER = ("The scoreboard tells the story", "It could shape what comes next", "A personal landmark in this match",
          "is at the centre of this", "A pause in the action", "One side is on top", "That was a real chance",
          "The pressure is building")


def _key_fact(moment):
    """The fact each moment type's casual template must name."""
    f, name = moment["facts"], moment["player"]["name"] if moment["player"] else None
    if moment["type"] == "goal":
        us = f["score"][moment["team"]]
        them = next(v for k, v in f["score"].items() if k != moment["team"])
        return [name, f"{us}-{them}"]
    if moment["type"] in ("half_time", "full_time"):
        return ["-".join(str(v) for v in f["score"].values())]
    return {"big_chance": [name, str(f.get("distance_m"))], "pressure_spell": [str(f.get("shots"))],
            "high_press_win": [name, f.get("target")], "momentum_swing": [str(f.get("momentum", {}).get(
                f.get("to_team"), {}).get("possession_pct"))],
            "speed_milestone": [str(f.get("shot_speed_kmh"))], "sprint_milestone": [name, str(f.get("run_speed_kmh"))],
            "distance_milestone": [name, f"{f.get('milestone_km')} km"],
            "pass_milestone": [name, str(f.get("passes"))]}[moment["type"]]


def test_every_casual_template_names_its_key_fact_without_filler(narrated):
    moments = {m["type"]: m for m, _ in narrated}
    speed = moments["speed_milestone"]
    moments["pass_milestone"] = speed | {"type": "pass_milestone", "moment_id": "pass-50",
                                         "facts": {"passes": 50, "passes_completed": 42, "pass_accuracy_pct": 84.0}}
    assert set(moments) == set(PRIORITY)
    for moment, _ in narrated + [(moments["pass_milestone"], None)]:
        versions = template_versions(moment)
        casual = " ".join(versions["casual"][k] for k in ("headline", "detail", "why"))
        for fact in _key_fact(moment):
            assert fact in casual, (moment["moment_id"], fact, casual)
        everything = " ".join(versions[p][k] for p in PERSONAS for k in ("headline", "detail", "why"))
        assert not any(line in everything for line in FILLER), (moment["moment_id"], everything)
        if moment["type"] == "speed_milestone":
            assert "strike" not in everything.lower() and "km/h" in versions["casual"]["headline"]
            assert not any(w in everything for w in ("sprint", " run ", "ran ")), everything
        if moment["type"] == "sprint_milestone":
            assert "sprints" in versions["casual"]["headline"] and "shot" not in everything, everything


def test_goal_casual_names_the_scorer_and_the_score(goal):
    moment, _ = goal
    assert template_versions(moment)["casual"]["headline"] == "Rafe Ironsby scores, Calderbrook lead 1-0"


def test_missing_foundry_settings_use_templates_without_a_call(goal, monkeypatch):
    for key in ("FOUNDRY_ENDPOINT", "FOUNDRY_MODEL_DEPLOYMENT", "FOUNDRY_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    moment, narrative = goal
    agent = PersonaAgent()
    assert agent.client is None and agent.versions(moment, narrative) == template_versions(moment)


def test_model_inputs_leave_out_ids(goal):
    moment, narrative = goal
    sent = json.dumps(persona_inputs(moment, narrative))
    assert moment["event_id"] not in sent and moment["player"]["id"] not in sent
    assert "Calderbrook Athletic" in sent and "striker" in sent


def test_cli_prints_valid_overlays_without_calling_a_model(monkeypatch, capsys):
    from deepkick.agents.persona import main
    from tests.conftest import REPLAY_PATH

    main([str(REPLAY_PATH), "--persona", "player", "--club", "ASH", "--player", "ASH08", "--limit", "8"])
    overlays = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(overlays) == 8
    for overlay in overlays:
        validate_overlay(overlay)
        assert overlay["persona"] == "player"


def test_persona_prompt_says_shot_speed_and_distance_are_not_a_run():
    assert "a shot distance (shot_distance_m) is the distance from goal" in SYSTEM_PROMPT
    assert "Never describe either as a player running" in SYSTEM_PROMPT


def test_each_persona_has_its_own_headline_detail_and_why(narrated):
    moments = {m["type"]: m for m, _ in narrated}
    moments["pass_milestone"] = moments["speed_milestone"] | {
        "type": "pass_milestone", "moment_id": "pass-50",
        "facts": {"passes": 50, "passes_completed": 42, "pass_accuracy_pct": 84.0}}
    assert set(moments) == set(PRIORITY)
    for kind, moment in moments.items():
        versions = template_versions(moment)
        headlines = [versions[p]["headline"] for p in PERSONAS]
        assert len(set(headlines)) == 3, (kind, headlines)
        lines = [versions[p][k] for p in PERSONAS for k in ("detail", "why")]
        assert len(set(lines)) == 6, (kind, lines)
        assert any(c.isdigit() for c in versions["analyst"]["headline"]), kind  # the analyst gets a key number
        name = moment["player"]["name"] if moment["player"] else None
        assert not name or versions["player"]["headline"].startswith(name), kind
        for persona in PERSONAS:
            assert len(versions[persona]["headline"].split()) <= 10, (kind, persona)
            for key in ("detail", "why"):
                assert len(versions[persona][key].split()) <= 30, (kind, persona, key)


def test_analyst_voice_has_no_shape_vocabulary():
    analyst = SYSTEM_PROMPT[SYSTEM_PROMPT.index("- analyst:"):SYSTEM_PROMPT.index("- casual:")]
    for word in ("cover", "second ball", "screening", "line", "shape", "organiser"):
        assert word not in analyst, word


@pytest.mark.parametrize("text", ["The keeper saves it.", "Calderbrook lead 1-0 now.", "It is Rafe Ironsby's first.",
                                  "That was a 0.252 xG chance."])
def test_a_short_full_sentence_passes(goal, text):
    moment, narrative = goal
    version = json.loads(_good_reply(moment))["casual"] | {"detail": text}
    assert check_version("casual", version, persona_inputs(moment, narrative))["detail"] == text


def test_a_bare_number_reply_is_retried(goal):
    moment, narrative = goal
    bare = json.loads(_good_reply(moment))
    for persona in PERSONAS:
        bare[persona]["why"] = "0.252"
    client = FakeClient(json.dumps(bare), _good_reply(moment))
    versions = PersonaAgent(client, "m").versions(moment, narrative)
    assert len(client.calls) == 2 and not any(v["fallback"] for v in versions.values())


def _check_evidence(moment, version, overlay):
    """Every evidence value equals its fact's value, and the overlay shows exactly the version's labels and values."""
    facts = dict(_leaves(moment["facts"]))
    assert all(e["value"] == facts[e["stat"]] for e in version["evidence"])
    assert overlay["payload"]["evidence"] == [{"label": e["label"], "value": e["value"]} for e in version["evidence"]]


def test_template_evidence_is_the_facts_each_template_read(narrated):
    for moment, _ in narrated:
        versions = template_versions(moment)
        for persona in PERSONAS:
            overlay = select_overlay(moment, versions, _profile(persona))
            assert overlay["payload"]["source"] == "template" and overlay["payload"]["evidence"], moment["type"]
            _check_evidence(moment, versions[persona], overlay)
    goal = next(m for m, _ in narrated if m["type"] == "goal")
    # The casual goal template says the scorer, the score, their goal tally and the distance; not xG or team shots.
    stats = [e["stat"] for e in template_versions(goal)["casual"]["evidence"]]
    assert sorted(stats) == sorted([*(f"score.{c}" for c in goal["facts"]["score"]), "player_goals", "distance_m"])


def test_model_evidence_is_the_narratives_checked_facts(goal):
    moment, narrative = goal
    f = moment["facts"]
    # A model narrative cites a few facts; its values are re-copied from the facts, never taken from model text.
    model_narrative = narrative | {"fallback": False,
                                   "evidence": [{"stat": "xg", "value": f["xg"]}, {"stat": "score.CAL", "value": 1}]}
    versions = PersonaAgent(FakeClient(_good_reply(moment)), "m").versions(moment, model_narrative)
    for persona in PERSONAS:
        overlay = select_overlay(moment, versions, _profile(persona))
        assert overlay["payload"]["source"] == "model"
        assert overlay["payload"]["evidence"] == [{"label": "Expected goals", "value": f["xg"]},
                                                  {"label": "Calderbrook goals", "value": f["score"]["CAL"]}]
        _check_evidence(moment, versions[persona], overlay)


def test_every_fact_key_has_a_readable_label(narrated):
    for moment, _ in narrated:
        for stat, _ in _leaves(moment["facts"]):
            label = fact_label(stat, moment["type"])
            assert label != stat and "_" not in label and "ASH" not in label and "CAL" not in label, stat
    assert fact_label("momentum.CAL.value", "momentum_swing") == "Calderbrook momentum"
    assert fact_label("distance_m", "goal") == "Shot distance (m)"
    assert fact_label("distance_m", "distance_milestone") == "Distance covered (m)"
    assert {"passes", "passes_completed", "pass_accuracy_pct"} <= FACT_LABELS.keys()  # pass milestones: not in seed 42
