"""Tests for the Narrative Agent with a mocked Foundry chat client and the seed-42 replay."""

import copy
import json
import logging
import pytest

from deepkick.agents.narrative import (HEADLINE_MAX, LEAGUE_POSITIONS, POSITION_WORDS, SYSTEM_PROMPT, THEME_BY_TYPE,
                                       THEMES, TIMEOUT_S, NarrativeAgent, check_output, foundry_base_url, model_inputs,
                                       spell_out_positions, template_narrative, theme_for)
from deepkick.moments import PRIORITY
from tests.conftest import FakeClient, rate_limit_error, timeout_error


@pytest.fixture(scope="module")
def goal(detected):
    return next((m, s) for m, s in detected if m["type"] == "goal")


@pytest.fixture(scope="module")
def every_type(detected):
    """One (moment, snapshot) per moment type, with a pass milestone built from a real pass (seed 42 has none)."""
    by_type = {}
    for m, s in detected:
        by_type.setdefault(m["type"], (m, s))
    m, s = by_type["speed_milestone"]
    milestone = copy.deepcopy(m) | {"type": "pass_milestone", "priority": PRIORITY["pass_milestone"],
                                    "facts": {"passes": 50, "passes_completed": 42, "pass_accuracy_pct": 84.0}}
    by_type["pass_milestone"] = (milestone, s)
    return by_type


def _good_reply(moment):
    f = moment["facts"]
    return json.dumps({
        "headline": f"{moment['player']['name']} opens the scoring with a {f['xg']} xG chance",
        "why_it_matters": f"It came from {f['distance_m']} m. That is shot number {f['team_shots']} for the side.",
        "evidence": [{"stat": "xg", "value": f["xg"]}, {"stat": "distance_m", "value": f["distance_m"]},
                     {"stat": "team_shots", "value": f["team_shots"]}],
    })


def test_good_reply_is_used(goal):
    moment, snap = goal
    client = FakeClient(_good_reply(moment))
    out = NarrativeAgent(client, "gpt-4o-mini").explain(moment, snap)
    assert out["fallback"] is False
    assert out == json.loads(_good_reply(moment)) | {"theme": "result", "fallback": False}
    call = client.calls[0]
    assert call["model"] == "gpt-4o-mini" and call["timeout"] == TIMEOUT_S == 30.0
    assert call["reasoning_effort"] == "low" and call["max_completion_tokens"] == 1500
    assert call["response_format"] == {"type": "json_object"}
    assert "only numbers that appear" in call["messages"][0]["content"]
    assert json.loads(call["messages"][1]["content"]) == model_inputs(moment, snap)


def test_invented_number_is_rejected_then_falls_back(goal):
    moment, snap = goal
    bad = json.loads(_good_reply(moment))
    bad["why_it_matters"] = "That makes it 7 goals in 3 games."
    client = FakeClient(json.dumps(bad), json.dumps(bad))
    out = NarrativeAgent(client, "m").explain(moment, snap)
    assert out == template_narrative(moment, snap)
    assert len(client.calls) == 2  # one retry, then the template


def _with(moment, **fields):
    return json.dumps(json.loads(_good_reply(moment)) | fields)


def test_prompt_asks_for_plain_broadcast_english():
    for phrase in ("at most 10 words", "at most 30 words", '"possession"', '"shots on target"', '"expected goals"',
                   "never raw field names", "RCB right centre-back", "0-2", "apostrophes",
                   "key of `moment.facts`", "score.CAL", "at least one item",
                   "Describe only what the facts show", "fatigue, confidence, form or intent"):
        assert phrase in SYSTEM_PROMPT, phrase
    assert '"theme"' not in SYSTEM_PROMPT  # set in code from the moment type


@pytest.mark.parametrize("kind, theme", [
    ("goal", "result"), ("big_chance", "chaos"), ("momentum_swing", "control"), ("pressure_spell", "pressure_shift"),
    ("full_time", "result"), ("half_time", "rhythm"), ("high_press_win", "pressure_shift"),
    ("speed_milestone", "milestone"), ("distance_milestone", "milestone"), ("pass_milestone", "milestone"),
])
def test_theme_is_set_in_code_from_the_moment_type(every_type, kind, theme):
    assert theme_for(kind) == theme
    moment, snap = every_type[kind]
    evidence = template_narrative(moment, snap)["evidence"][:1]  # a real fact of this moment
    # The model may still send a theme of its own; it is ignored.
    reply = json.dumps({"headline": "A moment to note", "why_it_matters": "It shapes the match.",
                        "theme": "momentum", "evidence": evidence})
    out = NarrativeAgent(FakeClient(reply), "m").explain(moment, snap)
    assert out["fallback"] is False and out["theme"] == theme
    assert set(THEME_BY_TYPE) == set(PRIORITY) and set(THEME_BY_TYPE.values()) <= set(THEMES)


def test_every_league_position_code_has_words():
    assert LEAGUE_POSITIONS and set(LEAGUE_POSITIONS) <= set(POSITION_WORDS)


@pytest.mark.parametrize("text, expected", [
    ("RCB Dace Blackwen holds the line", "right centre-back Dace Blackwen holds the line"),
    ("The ST and the LW combine for CAL", "The striker and the left winger combine for CAL"),
    ("GK, CDM and RB all involved.", "goalkeeper, defensive midfielder and right-back all involved."),
    ("ASH 0-2 CAL after the ASH-CAL derby", "ASH 0-2 CAL after the ASH-CAL derby"),  # club codes stay
    ("STRIKE from the CBS stand; RCBs and ST2 untouched", "STRIKE from the CBS stand; RCBs and ST2 untouched"),
])
def test_spell_out_positions_replaces_only_standalone_codes(text, expected):
    assert spell_out_positions(text) == expected


def test_position_codes_in_the_model_reply_are_spelled_out(goal):
    moment, snap = goal
    reply = _with(moment, headline=f"{moment['player']['name']}, the ST, scores for CAL",
                  why_it_matters="The RCB was beaten from 9.2 m as CAL took the lead.")
    out = NarrativeAgent(FakeClient(reply), "m").explain(moment, snap)
    assert out["fallback"] is False
    assert out["headline"] == f"{moment['player']['name']}, the striker, scores for CAL"
    assert out["why_it_matters"] == "The right centre-back was beaten from 9.2 m as CAL took the lead."


@pytest.mark.parametrize("evidence, problem", [
    ([], "must not be empty"),
    ([{"stat": "facts.xg", "value": 0.252}], "does not match the moment facts"),  # not a short facts key
    ([{"stat": "possession_pct", "value": None}], "does not match the moment facts"),  # snapshot stat, not a fact
    ([{"stat": "score.CAL", "value": 2}], "does not match the moment facts"),  # real fact, wrong value
    ([{"stat": "xg", "value": 0.29}], "does not match the moment facts"),  # team_xg's value under xg
])
def test_evidence_must_be_facts_keys_with_their_values(goal, evidence, problem):
    moment, snap = goal
    for item in evidence:
        if item["value"] is None:  # a real snapshot number, so only the facts-key rule can reject it
            item["value"] = snap["teams"]["CAL"][item["stat"]]
    with pytest.raises(ValueError, match=problem):
        check_output(_with(moment, evidence=evidence), model_inputs(moment, snap))


def test_nested_facts_keys_are_accepted_as_evidence(goal):
    moment, snap = goal
    evidence = [{"stat": "score.CAL", "value": moment["facts"]["score"]["CAL"]}, {"stat": "xg", "value": 0.252}]
    assert check_output(_with(moment, evidence=evidence), model_inputs(moment, snap))["evidence"] == evidence


def test_empty_evidence_is_retried_then_falls_back(goal):
    moment, snap = goal
    client = FakeClient(_with(moment, evidence=[]), _with(moment, evidence=[]))
    assert NarrativeAgent(client, "m").explain(moment, snap) == template_narrative(moment, snap)
    assert len(client.calls) == 2


def test_xg_scores_and_apostrophes_are_fine(goal):
    moment, snap = goal
    reply = _with(moment, headline="Rafe Ironsby's 0.252 xG chance makes it 0-1",
                  why_it_matters="Calderbrook Athletic's opener came from 9.2 m. That's 2 shots and 0.29 xG.")
    assert check_output(reply, model_inputs(moment, snap))["headline"].endswith("0-1")


def test_invented_evidence_value_is_rejected(goal):
    moment, snap = goal
    bad = json.loads(_good_reply(moment))
    bad["evidence"].append({"stat": "xg", "value": 0.9})
    with pytest.raises(ValueError, match="not in the inputs"):
        check_output(json.dumps(bad), model_inputs(moment, snap))


def test_evidence_must_name_the_stat_that_holds_the_value(goal):
    moment, snap = goal
    bad = json.loads(_good_reply(moment))
    bad["evidence"][0]["stat"] = "distance_m"  # a real number, filed under the wrong stat
    with pytest.raises(ValueError, match="evidence does not match"):
        check_output(json.dumps(bad), model_inputs(moment, snap))


def test_retry_recovers_after_a_bad_first_reply(goal):
    moment, snap = goal
    client = FakeClient("not json", _good_reply(moment))
    assert NarrativeAgent(client, "m").explain(moment, snap)["fallback"] is False
    assert len(client.calls) == 2


def test_timeout_falls_back_after_one_retry(goal, caplog):
    moment, snap = goal
    client = FakeClient(timeout_error(), timeout_error())
    with caplog.at_level(logging.INFO, logger="deepkick.narrative"):
        out = NarrativeAgent(client, "m").explain(moment, snap)
    assert out == template_narrative(moment, snap) and out["fallback"] is True
    assert len(client.calls) == 2
    assert "APITimeoutError" in caplog.text
    assert f"stage=narrative event_id={moment['event_id']} duration_ms=" in caplog.text
    assert "fallback=yes" in caplog.text


EVIDENCE = [{"stat": "xg", "value": 0.252}]  # valid for the first goal, so each case below fails for one reason


@pytest.mark.parametrize("reply", [
    "not json",
    '{"headline": "Unclosed"',
    "[]",
    json.dumps({"headline": "x", "why_it_matters": "y."}),
    json.dumps({"headline": "x" * (HEADLINE_MAX + 1), "why_it_matters": "y.", "evidence": EVIDENCE}),
    json.dumps({"headline": "x", "why_it_matters": "One. Two. Three.", "evidence": EVIDENCE}),
    json.dumps({"headline": "x", "why_it_matters": "y.", "mood": "tense", "evidence": EVIDENCE}),
    json.dumps({"headline": "x", "why_it_matters": "y.", "evidence": [{"stat": "xg"}]}),
])
def test_malformed_reply_falls_back(goal, reply):
    moment, snap = goal
    out = NarrativeAgent(FakeClient(reply, reply), "m").explain(moment, snap)
    assert out == template_narrative(moment, snap)


def test_any_client_error_never_reaches_the_caller(goal):
    moment, snap = goal
    out = NarrativeAgent(FakeClient(RuntimeError("boom"), KeyError("x")), "m").explain(moment, snap)
    assert out["fallback"] is True


def test_missing_foundry_settings_use_the_template(goal, monkeypatch, caplog):
    for key in ("FOUNDRY_ENDPOINT", "FOUNDRY_MODEL_DEPLOYMENT", "FOUNDRY_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    moment, snap = goal
    with caplog.at_level(logging.WARNING, logger="deepkick.narrative"):
        agent = NarrativeAgent()
    assert agent.client is None and "FOUNDRY_* settings missing" in caplog.text
    assert agent.explain(moment, snap) == template_narrative(moment, snap)


PROJECT_BASE = "https://deepkick-test.services.ai.azure.com/api/projects/deepkick/openai/v1/"
HOST_BASE = "https://deepkick-test.openai.azure.com/openai/v1/"


@pytest.mark.parametrize("endpoint, base", [
    # project endpoint, and full target URIs pasted from the project's deployment page
    ("https://deepkick-test.services.ai.azure.com/api/projects/deepkick", PROJECT_BASE),
    ("https://deepkick-test.services.ai.azure.com/api/projects/deepkick/", PROJECT_BASE),
    ("https://deepkick-test.services.ai.azure.com/api/projects/deepkick/openai/v1/chat/completions"
     "?api-version=preview", PROJECT_BASE),
    ("https://deepkick-test.services.ai.azure.com/api/projects/deepkick/openai/v1/responses", PROJECT_BASE),
    # bare host, and full target URIs on the same host
    ("https://deepkick-test.openai.azure.com", HOST_BASE),
    (" https://deepkick-test.openai.azure.com/ ", HOST_BASE),
    ("https://deepkick-test.openai.azure.com/openai/v1", HOST_BASE),
    ("https://deepkick-test.openai.azure.com/openai/v1/chat/completions", HOST_BASE),
    ("https://deepkick-test.openai.azure.com/openai/deployments/gpt-5-mini/chat/completions"
     "?api-version=2025-04-01-preview", HOST_BASE),
    ("https://deepkick-test.openai.azure.com/responses", HOST_BASE),
])
def test_endpoint_is_normalised_to_one_base_url(endpoint, base):
    assert foundry_base_url(endpoint) == base


@pytest.mark.parametrize("endpoint", ["deepkick-test.openai.azure.com", "ftp://deepkick-test", "https://"])
def test_endpoint_without_scheme_and_host_is_rejected(endpoint):
    with pytest.raises(ValueError, match="FOUNDRY_ENDPOINT"):
        foundry_base_url(endpoint)


def test_client_is_built_for_the_v1_endpoint_and_logs_its_path_not_its_key(monkeypatch, caplog):
    monkeypatch.setenv("FOUNDRY_ENDPOINT", "https://deepkick-test.services.ai.azure.com/api/projects/deepkick/"
                                           "openai/v1/chat/completions?api-version=preview")
    monkeypatch.setenv("FOUNDRY_MODEL_DEPLOYMENT", "gpt-5-mini")
    monkeypatch.setenv("FOUNDRY_API_KEY", "test-key-do-not-log")
    with caplog.at_level(logging.INFO, logger="deepkick.narrative"):
        agent = NarrativeAgent()
    assert str(agent.client.base_url) == PROJECT_BASE
    assert agent.deployment == "gpt-5-mini"
    assert agent.client.max_retries == 0 and agent.client.timeout == TIMEOUT_S
    assert caplog.text.count("base_path=/api/projects/deepkick/openai/v1/") == 1
    assert "test-key-do-not-log" not in caplog.text and "deepkick-test" not in caplog.text


def test_bad_endpoint_uses_the_template(goal, monkeypatch, caplog):
    monkeypatch.setenv("FOUNDRY_ENDPOINT", "deepkick-test.openai.azure.com")
    monkeypatch.setenv("FOUNDRY_MODEL_DEPLOYMENT", "gpt-5-mini")
    monkeypatch.setenv("FOUNDRY_API_KEY", "test-key")
    with caplog.at_level(logging.WARNING, logger="deepkick.narrative"):
        agent = NarrativeAgent()
    assert agent.client is None and "must be an http(s) URL" in caplog.text
    assert agent.explain(*goal)["fallback"] is True


def test_template_covers_every_moment_type(every_type):
    assert set(every_type) == set(PRIORITY)
    for kind, (moment, snap) in every_type.items():
        out = template_narrative(moment, snap)
        assert out["fallback"] is True, kind
        assert out["theme"] in THEMES, kind
        assert 0 < len(out["headline"]) <= HEADLINE_MAX, kind
        # The template obeys the same rules as the model: shape, sentence count and only quoted numbers.
        checked = check_output(json.dumps({k: v for k, v in out.items() if k != "fallback"}),
                               model_inputs(moment, snap))
        assert checked["evidence"] == out["evidence"], kind


def test_fallback_text_keeps_the_players_full_name(detected, every_type):
    named = [(m, s) for m, s in detected if m["player"]] + [every_type["pass_milestone"]]
    assert {"Rafe Ironsby", "Lucan Ullaby", "Corin Rookwell"} <= {m["player"]["name"] for m, _ in named}
    for moment, snap in named:
        name = moment["player"]["name"]
        assert " " in name
        out = template_narrative(moment, snap)
        if moment["type"] in ("goal", "high_press_win", "speed_milestone", "sprint_milestone", "distance_milestone",
                              "pass_milestone"):
            assert name in out["headline"], moment["moment_id"]
        if moment["type"] in ("goal", "high_press_win", "distance_milestone"):
            assert name in out["why_it_matters"], moment["moment_id"]
        assert name.replace(" ", "") not in out["headline"] + out["why_it_matters"], moment["moment_id"]


def test_template_passes_the_number_check_for_every_replay_moment(detected):
    for moment, snap in detected:
        out = template_narrative(moment, snap)
        check_output(json.dumps({k: v for k, v in out.items() if k != "fallback"}), model_inputs(moment, snap))


def test_model_inputs_leave_out_ids(goal):
    moment, snap = goal
    sent = json.dumps(model_inputs(moment, snap))
    assert moment["event_id"] not in sent and moment["player"]["id"] not in sent
    assert moment["player"]["name"] in sent


def test_good_reply_logs_fallback_no(goal, caplog):
    moment, snap = goal
    with caplog.at_level(logging.INFO, logger="deepkick.narrative"):
        NarrativeAgent(FakeClient(_good_reply(moment)), "m").explain(moment, snap)
    assert f"stage=narrative event_id={moment['event_id']} duration_ms=" in caplog.text
    assert "fallback=no" in caplog.text


def test_cli_prints_narratives_without_calling_a_model(monkeypatch, capsys):
    import dotenv

    from deepkick.agents.narrative import main
    from tests.conftest import REPLAY_PATH

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)  # never read a developer's real .env here
    for key in ("FOUNDRY_ENDPOINT", "FOUNDRY_MODEL_DEPLOYMENT", "FOUNDRY_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    main([str(REPLAY_PATH), "--limit", "3"])
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(lines) == 3 and all(n["fallback"] and n["moment_id"] for n in lines)


def test_prompt_says_shot_speed_and_distance_are_not_a_run():
    assert "a shot distance (shot_distance_m) is the distance from goal" in SYSTEM_PROMPT
    assert "Never describe either as a player running" in SYSTEM_PROMPT


@pytest.mark.parametrize("stat, ok", [("shot_speed_kmh", True), ("speed_kmh", False), ("run_speed_kmh", False)])
def test_speed_milestone_evidence_uses_the_shot_keys(every_type, stat, ok):
    moment, snap = every_type["speed_milestone"]
    value = moment["facts"]["shot_speed_kmh"]
    reply = json.dumps({"headline": f"A {value} km/h shot", "why_it_matters": "It was blocked.",
                        "evidence": [{"stat": stat, "value": value}]})
    if ok:
        check_output(reply, model_inputs(moment, snap))
    else:
        with pytest.raises(ValueError, match="evidence does not match"):
            check_output(reply, model_inputs(moment, snap))


def test_speed_and_sprint_templates_never_mix_up_ball_and_runner(every_type):
    shot = template_narrative(*every_type["speed_milestone"])
    sprint = template_narrative(*every_type["sprint_milestone"])
    assert "shot" in shot["headline"] and not any(w in shot["headline"] + shot["why_it_matters"] for w in ("run", "sprint"))
    assert "sprints" in sprint["headline"] and "shot" not in sprint["headline"] + sprint["why_it_matters"]


@pytest.fixture
def sleeps(monkeypatch):
    """Record backoff waits instead of sleeping."""
    import deepkick.agents.narrative as narrative

    waits = []
    monkeypatch.setattr(narrative, "_sleep", waits.append)
    return waits


def test_a_429_backs_off_then_the_model_reply_ships(goal, sleeps):
    moment, snap = goal
    client = FakeClient(rate_limit_error(), _good_reply(moment))
    out = NarrativeAgent(client, "m").explain(moment, snap)
    assert out["fallback"] is False and sleeps == [2.0] and len(client.calls) == 2


def test_retry_after_header_is_respected(goal, sleeps):
    moment, snap = goal
    client = FakeClient(rate_limit_error(retry_after="5"), rate_limit_error(retry_after="soon"), _good_reply(moment))
    assert NarrativeAgent(client, "m").explain(moment, snap)["fallback"] is False
    assert sleeps == [5.0, 4.0]  # the header wins; an unreadable header falls back to the schedule


def test_retry_after_is_capped_at_30_seconds(goal, sleeps):
    moment, snap = goal
    client = FakeClient(rate_limit_error(retry_after="120"), _good_reply(moment))
    assert NarrativeAgent(client, "m").explain(moment, snap)["fallback"] is False
    assert sleeps == [30.0]


def test_429s_do_not_use_up_the_normal_attempts(goal, sleeps):
    moment, snap = goal
    # Three 429s and one bad reply would exhaust two attempts if 429s counted; here the second attempt succeeds.
    client = FakeClient(rate_limit_error(), rate_limit_error(), "not json", rate_limit_error(), _good_reply(moment))
    out = NarrativeAgent(client, "m").explain(moment, snap)
    assert out["fallback"] is False and sleeps == [2.0, 4.0, 8.0] and len(client.calls) == 5


def test_429s_after_the_backoff_runs_out_count_as_attempts(goal, sleeps):
    from deepkick.agents.narrative import rate_limit_count

    moment, snap = goal
    before = rate_limit_count()
    client = FakeClient(*[rate_limit_error() for _ in range(5)])
    assert NarrativeAgent(client, "m").explain(moment, snap) == template_narrative(moment, snap)
    assert sleeps == [2.0, 4.0, 8.0] and len(client.calls) == 5  # three backoffs, then the two normal attempts
    assert rate_limit_count() - before == 5
