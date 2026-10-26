"""Tests for the Recap Agent and the catch-up on the seed-42 replay, with a fake model client (no network)."""

import asyncio
import json
import logging

import pytest

from deepkick.agents.narrative import NarrativeAgent
from deepkick.agents.persona import PersonaAgent
from deepkick.agents.recap import SYSTEM_PROMPT, RecapAgent, check_recap, goals, recap_inputs, template_recap
from deepkick.contracts import PERSONAS
from deepkick.orchestrator import Orchestrator, replay_events
from deepkick.simulator import clock_seconds
from tests.conftest import REPLAY_PATH, FakeClient, timeout_error

GOALS = 8  # seed 42 ends ASH 3-5 CAL


@pytest.fixture(scope="module")
def finished():
    """An orchestrator that has played the whole replay with templates only (no model settings in tests)."""
    orchestrator = Orchestrator(NarrativeAgent(None, None), PersonaAgent(None, None), recapper=RecapAgent())
    asyncio.run(orchestrator.run(replay_events(REPLAY_PATH)))
    return orchestrator


@pytest.fixture(scope="module")
def full_time(detected):
    """(full_time moment, its snapshot, every moment up to it)."""
    moments = [m for m, _ in detected]
    moment, snapshot = next((m, s) for m, s in detected if m["type"] == "full_time")
    return moment, snapshot, moments


def _mentions_every_goal(text, inputs):
    return all(scorer in text and f"{minute}" in text for scorer, minute in goals(inputs))


def _model_reply(inputs, broken=()):
    """A reply that passes the checks, with the personas in `broken` given an invented number."""
    named = ", ".join(f"{scorer} in minute {minute}" for scorer, minute in goals(inputs))
    score = inputs["match_state"]["score"]
    text = (f"Calderbrook Athletic won it {score['CAL']}-{score['ASH']}. The goals came from {named}. "
            "It was an open, end-to-end match.")
    return json.dumps({p: {"text": text + (" They had 99 chances." if p in broken else "")} for p in PERSONAS})


def test_the_full_time_recap_names_all_eight_goals_with_scorer_and_minute(finished, full_time):
    inputs = recap_inputs(*full_time)
    assert len(goals(inputs)) == GOALS
    for persona in PERSONAS:
        full, half = (r["text"] for r in finished.recaps(persona))
        assert _mentions_every_goal(full, inputs), persona
        assert "Florin Underhay" in full and "Florin Underhay" not in half  # a second-half goal is not in the HT recap
        assert full.startswith("Full-time: Ashvale Wanderers 3-5 Calderbrook Athletic.")


def test_one_model_call_writes_all_three_recaps(full_time, caplog):
    moment, snapshot, moments = full_time
    inputs = recap_inputs(moment, snapshot, moments)
    client = FakeClient(_model_reply(inputs))
    with caplog.at_level(logging.INFO, logger="deepkick.recap"):
        recaps = RecapAgent(client, "m").write(moment, snapshot, moments)
    assert len(client.calls) == 1
    assert all(recaps[p]["fallback"] is False and _mentions_every_goal(recaps[p]["text"], inputs) for p in PERSONAS)
    assert f"stage=recap event_id={moment['event_id']} type=full_time duration_ms=" in caplog.text
    assert "fallback=no sources=analyst:model,casual:model,player:model" in caplog.text


def test_one_failed_recap_is_templated_alone(full_time, caplog):
    moment, snapshot, moments = full_time
    inputs = recap_inputs(moment, snapshot, moments)
    client = FakeClient(_model_reply(inputs, broken={"casual"}))
    with caplog.at_level(logging.INFO, logger="deepkick.recap"):
        recaps = RecapAgent(client, "m").write(moment, snapshot, moments)
    assert len(client.calls) == 1
    assert recaps["casual"] == template_recap(inputs)["casual"]
    assert recaps["analyst"]["fallback"] is False and recaps["player"]["fallback"] is False
    assert "persona=casual fallback=yes: numbers not in the inputs: ['99']" in caplog.text


def test_timeouts_fall_back_to_the_template_after_one_retry(full_time, caplog):
    moment, snapshot, moments = full_time
    client = FakeClient(timeout_error(), timeout_error())
    with caplog.at_level(logging.INFO, logger="deepkick.recap"):
        recaps = RecapAgent(client, "m").write(moment, snapshot, moments)
    assert len(client.calls) == 2
    assert recaps == template_recap(recap_inputs(moment, snapshot, moments))
    assert all(recaps[p]["fallback"] for p in PERSONAS)
    assert "fallback=yes sources=analyst:template,casual:template,player:template" in caplog.text


@pytest.mark.parametrize("text, problem", [
    ("Calderbrook Athletic won it. Rafe Ironsby scored.", "3-6 sentences"),
    ("One. Two. Three. Four. Five. Six. Seven.", "3-6 sentences"),
    ("Calderbrook Athletic won 3-2. It was close. They had 99 shots.", "numbers not in the inputs"),
    ("Calderbrook Athletic won 3-2. It was close. Rafe Ironsby scored in minute 14.", "leaves out goals"),
])
def test_check_rejects_bad_recaps(full_time, text, problem):
    with pytest.raises(ValueError, match=problem):
        check_recap("analyst", {"text": text}, recap_inputs(*full_time))


def test_the_inputs_list_every_goal_so_far_in_order(full_time, detected):
    assert [(g["minute"], g["scorer"]) for g in recap_inputs(*full_time)["goals"]] == [
        (14, "Rafe Ironsby"), (20, "Caius Mossington"), (22, "Caius Mossington"), (29, "Corin Rookwell"),
        (33, "Caius Mossington"), (51, "Florin Underhay"), (59, "Rafe Ironsby"), (90, "Corin Rookwell")]
    half_time = next((m, s) for m, s in detected if m["type"] == "half_time")
    assert len(recap_inputs(*half_time, full_time[2])["goals"]) == 5


def test_a_scorers_goals_can_share_one_mention(full_time):
    text = ("Calderbrook Athletic won 5-3. Rafe Ironsby (14', 59') and Caius Mossington (20', 22', 33') scored for "
            "them. Corin Rookwell (29', 90') and Florin Underhay (51') replied for Ashvale Wanderers.")
    assert check_recap("casual", {"text": text}, recap_inputs(*full_time))["text"] == text


def test_every_voice_must_name_the_goals_and_their_minutes_never_count_against_casual():
    # Live seed-42 runs: the analyst wrote only stats, and casual's three-number cap dropped goal minutes.
    assert "Every voice names every goal in `goals`" in SYSTEM_PROMPT
    assert "analyst: the goals, then the numbers" in SYSTEM_PROMPT
    assert "at most three numbers besides the\n  goal minutes" in SYSTEM_PROMPT


def test_templates_pass_their_own_checks(full_time):
    inputs = recap_inputs(*full_time)
    for persona, version in template_recap(inputs).items():
        assert check_recap(persona, {"text": version["text"]}, inputs)


def test_catch_up_at_30_minutes_contains_nothing_after_30_minutes(finished):
    catch = finished.catch_up("casual", 10, at="30:00")
    assert catch["at"] == "30:00" and catch["from"] == "20:00" and catch["minutes"] == 10
    clocks = [m["match_clock"] for m in catch["moments"]]
    assert clocks and all("20:00" < c <= "30:00" for c in clocks)
    assert clocks == sorted(clocks, reverse=True)  # newest first
    # The score at 30:00, not at full time; the 20:00 score counts only goals at or before 20:00.
    goals_by_30 = [e for e in finished._timeline if e["moment"]["type"] == "goal"
                   and clock_seconds(e["moment"]) <= 30 * 60]
    assert catch["score"]["now"] == goals_by_30[-1]["score"] != {"ASH": 2, "CAL": 3}
    assert catch["score"]["goals"] == sum(catch["score"]["now"].values()) - sum(catch["score"]["then"].values())
    assert catch["momentum"]["team"] in ("ASH", "CAL")
    later = {e["moment"]["moment_id"] for e in finished._timeline if clock_seconds(e["moment"]) > 30 * 60}
    assert later and not {m["moment_id"] for m in catch["moments"]} & later


def test_catch_up_headlines_follow_the_viewers_persona(finished):
    casual = finished.catch_up("casual", 30, at="45:00")["moments"]
    analyst = finished.catch_up("analyst", 30, at="45:00")["moments"]
    assert [m["match_clock"] for m in casual] == [m["match_clock"] for m in analyst]
    assert [m["headline"] for m in casual] != [m["headline"] for m in analyst]
