"""Tests for the Persona Agent and viewer selection with a mocked Foundry chat client and the seed-42 replay."""

import json
import logging

import pytest

from deepkick.agents.narrative import TIMEOUT_S, template_narrative
from deepkick.agents.persona import (OVERLAY_PRIORITY, PersonaAgent, check_versions, persona_inputs, select_overlay,
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


def _of(narrated, kind, team=None):
    return next((m, n) for m, n in narrated if m["type"] == kind and team in (None, m["team"]))


def _good_reply(moment):
    name, f = moment["player"]["name"], moment["facts"]
    return json.dumps({
        "analyst": {"headline": f"{name} scores from {f['distance_m']} m",
                    "detail": f"A {f['xg']} xG chance, shot number {f['team_shots']} for Calderbrook Athletic.",
                    "why": "It makes the score ASH 0-1 CAL."},
        "casual": {"headline": f"{name} gives Calderbrook Athletic the lead",
                   "detail": "A close-range finish from the striker.", "why": "It is the first goal of the match."},
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
    assert agent.versions(moment, narrative)["fallback"] is False and len(client.calls) == 1


def test_overlay_timing_comes_from_the_match_clock(narrated):
    for moment, narrative in narrated:
        overlay = select_overlay(moment, template_versions(moment, narrative), _profile())
        minutes, seconds = moment["match_clock"].split(":")
        assert overlay["show_at_ms"] == (int(minutes) * 60 + int(seconds)) * 1000
        assert overlay["duration_ms"] == (8000 if moment["type"] == "goal" else 6000)
        assert overlay["overlay_id"] == f"{moment['moment_id']}-analyst-en"


def test_every_moment_type_has_an_overlay_priority():
    assert set(OVERLAY_PRIORITY) == set(PRIORITY)
    assert max(OVERLAY_PRIORITY, key=OVERLAY_PRIORITY.get) == "goal"


def test_favourite_club_or_player_adds_one_capped_at_five(narrated):
    moment, narrative = _of(narrated, "big_chance", "ASH")
    versions = template_versions(moment, narrative)
    base = OVERLAY_PRIORITY["big_chance"]
    assert select_overlay(moment, versions, _profile())["priority"] == base
    assert select_overlay(moment, versions, _profile(club="ASH"))["priority"] == base + 1
    assert select_overlay(moment, versions, _profile(player=moment["player"]["id"]))["priority"] == base + 1
    goal, goal_narrative = _of(narrated, "goal")
    assert select_overlay(goal, template_versions(goal, goal_narrative), _profile(club=goal["team"]))["priority"] == 5


def test_match_wide_moments_involve_the_favourite_club(narrated):
    moment, narrative = _of(narrated, "full_time")
    versions = template_versions(moment, narrative)
    assert select_overlay(moment, versions, _profile(club="CAL"))["priority"] == OVERLAY_PRIORITY["full_time"] + 1


def test_small_moments_for_the_other_club_drop_to_one(narrated):
    moment, narrative = _of(narrated, "high_press_win", "CAL")
    versions = template_versions(moment, narrative)
    assert OVERLAY_PRIORITY["high_press_win"] > 1
    assert select_overlay(moment, versions, _profile(club="ASH"))["priority"] == 1
    big, big_narrative = _of(narrated, "big_chance", "CAL")  # above the low-priority line, so it keeps its priority
    assert select_overlay(big, template_versions(big, big_narrative),
                          _profile(club="ASH"))["priority"] == OVERLAY_PRIORITY["big_chance"]


@pytest.mark.parametrize("club", [None, "ASH", "CAL"])
@pytest.mark.parametrize("persona", PERSONAS)
def test_goals_are_never_dropped(narrated, persona, club):
    goals = [(m, n) for m, n in narrated if m["type"] == "goal"]
    assert len(goals) == 5
    for moment, narrative in goals:
        overlay = select_overlay(moment, template_versions(moment, narrative), _profile(persona, club, "ASH02"))
        assert overlay["priority"] == 5 and overlay["payload"]["headline"]


def test_timeout_falls_back_to_templates_after_one_retry(goal, caplog):
    moment, narrative = goal
    client = FakeClient(timeout_error(), timeout_error())
    with caplog.at_level(logging.INFO, logger="deepkick.persona"):
        versions = PersonaAgent(client, "m").versions(moment, narrative)
    assert versions == template_versions(moment, narrative) and versions["fallback"] is True
    assert len(client.calls) == 2
    assert "APITimeoutError" in caplog.text
    assert f"stage=persona event_id={moment['event_id']} duration_ms=" in caplog.text and "fallback=yes" in caplog.text


@pytest.mark.parametrize("persona, field, text, problem", [
    ("analyst", "detail", "A 0.9 xG chance, the best of the match.", "numbers not in the inputs"),
    ("player", "why", "It is Rafe Ironsby's 12th goal this season.", "numbers not in the inputs"),
    ("casual", "detail", "A 0.252 xG chance from 9.2 m.", "more than one number"),
    ("player", "headline", "A cool finish to open the scoring", "player headline must name"),
    ("casual", "headline", "", "non-empty"),
])
def test_check_rejects_invented_numbers_and_broken_versions(goal, persona, field, text, problem):
    moment, narrative = goal
    reply = json.loads(_good_reply(moment))
    reply[persona][field] = text
    with pytest.raises(ValueError, match=problem):
        check_versions(json.dumps(reply), persona_inputs(moment, narrative))


def test_invented_number_falls_back_after_two_attempts(goal):
    moment, narrative = goal
    reply = json.loads(_good_reply(moment))
    reply["analyst"]["why"] = "That makes it 7 goals in 3 games."
    client = FakeClient(json.dumps(reply), json.dumps(reply))
    assert PersonaAgent(client, "m").versions(moment, narrative)["fallback"] is True
    assert len(client.calls) == 2


def test_narrative_text_is_not_a_number_source(goal):
    moment, narrative = goal
    narrative = narrative | {"why_it_matters": "They now have 44 touches in the box."}
    reply = json.loads(_good_reply(moment))
    reply["analyst"]["why"] = "They now have 44 touches in the box."
    with pytest.raises(ValueError, match="numbers not in the inputs"):
        check_versions(json.dumps(reply), persona_inputs(moment, narrative))


def test_position_codes_in_replies_are_spelled_out(goal):
    moment, narrative = goal
    reply = json.loads(_good_reply(moment))
    reply["casual"]["detail"] = "A close-range finish from the ST."
    assert check_versions(json.dumps(reply), persona_inputs(moment, narrative))["casual"]["detail"] == \
        "A close-range finish from the striker."


def test_templates_pass_the_same_checks_for_every_moment(narrated):
    for moment, narrative in narrated:
        versions = template_versions(moment, narrative)
        check_versions(json.dumps({p: versions[p] for p in PERSONAS}), persona_inputs(moment, narrative))
        assert len(versions["casual"]["headline"].split()) <= 10, moment["moment_id"]
        if moment["player"]:
            assert moment["player"]["name"] in versions["player"]["headline"]


def test_missing_foundry_settings_use_templates_without_a_call(goal, monkeypatch):
    for key in ("FOUNDRY_ENDPOINT", "FOUNDRY_MODEL_DEPLOYMENT", "FOUNDRY_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    moment, narrative = goal
    agent = PersonaAgent()
    assert agent.client is None and agent.versions(moment, narrative) == template_versions(moment, narrative)


def test_model_inputs_leave_out_ids(goal):
    moment, narrative = goal
    sent = json.dumps(persona_inputs(moment, narrative))
    assert moment["event_id"] not in sent and moment["player"]["id"] not in sent
    assert "Calderbrook Athletic" in sent and "striker" in sent


def test_cli_prints_valid_overlays_without_calling_a_model(monkeypatch, capsys):
    import dotenv

    from deepkick.agents.persona import main
    from tests.conftest import REPLAY_PATH

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)  # never read a developer's real .env here
    for key in ("FOUNDRY_ENDPOINT", "FOUNDRY_MODEL_DEPLOYMENT", "FOUNDRY_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    main([str(REPLAY_PATH), "--persona", "player", "--club", "ASH", "--player", "ASH08", "--limit", "8"])
    overlays = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(overlays) == 8
    for overlay in overlays:
        validate_overlay(overlay)
        assert overlay["persona"] == "player"
