"""Tests for the Narrative Agent with a mocked Foundry chat client and the seed-42 replay."""

import copy
import json
import logging
from types import SimpleNamespace

import httpx2
import openai
import pytest

from deepkick.agents.narrative import (HEADLINE_MAX, THEMES, TIMEOUT_S, NarrativeAgent, check_output,
                                       foundry_base_url, model_inputs, template_narrative)
from deepkick.moments import PRIORITY, MomentDetector
from deepkick.stats import StatsAgent


class FakeClient:
    """Stands in for openai.OpenAI: each call pops the next reply (a string) or raises it (an exception)."""

    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=reply))])


@pytest.fixture(scope="module")
def detected(replay):
    """(moment, snapshot) pairs for every moment in the replay."""
    stats, detector, out = StatsAgent(), MomentDetector(), []
    for event in replay:
        if stats.update(event):
            snap = stats.snapshot()
            out += [(m, snap) for m in detector.detect(event, snap)]
    return out


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
        "theme": "result",
        "evidence": [{"stat": "xg", "value": f["xg"]}, {"stat": "distance_m", "value": f["distance_m"]},
                     {"stat": "team_shots", "value": f["team_shots"]}],
    })


def _timeout():
    return openai.APITimeoutError(request=httpx2.Request("POST", "https://example.test/openai/v1/chat/completions"))


def test_good_reply_is_used(goal):
    moment, snap = goal
    client = FakeClient(_good_reply(moment))
    out = NarrativeAgent(client, "gpt-4o-mini").explain(moment, snap)
    assert out["fallback"] is False
    assert out | {"fallback": None} == json.loads(_good_reply(moment)) | {"fallback": None}
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
    client = FakeClient(_timeout(), _timeout())
    with caplog.at_level(logging.INFO, logger="deepkick.narrative"):
        out = NarrativeAgent(client, "m").explain(moment, snap)
    assert out == template_narrative(moment, snap) and out["fallback"] is True
    assert len(client.calls) == 2
    assert "APITimeoutError" in caplog.text
    assert f"stage=narrative event_id={moment['event_id']} duration_ms=" in caplog.text
    assert "fallback=yes" in caplog.text


@pytest.mark.parametrize("reply", [
    "not json",
    '{"headline": "Unclosed"',
    "[]",
    json.dumps({"headline": "x", "why_it_matters": "y.", "theme": "result"}),
    json.dumps({"headline": "x" * (HEADLINE_MAX + 1), "why_it_matters": "y.", "theme": "result", "evidence": []}),
    json.dumps({"headline": "x", "why_it_matters": "One. Two. Three.", "theme": "result", "evidence": []}),
    json.dumps({"headline": "x", "why_it_matters": "y.", "theme": "drama", "evidence": []}),
    json.dumps({"headline": "x", "why_it_matters": "y.", "theme": "result", "evidence": [{"stat": "xg"}]}),
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
        if moment["type"] in ("goal", "high_press_win", "speed_milestone", "distance_milestone", "pass_milestone"):
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
