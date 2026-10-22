"""Tests for the translate step with a mocked Azure AI Translator (httpx MockTransport, no network)."""

import json
import logging

import httpx
import pytest

from deepkick.agents.persona import template_versions
from deepkick.agents.translate import Translator, protect, translate_url, translate_versions, unprotect
from deepkick.contracts import PERSONAS

URL = "https://api.cognitive.microsofttranslator.com"
ARABIC = "ترجمة: "  # the fake Translator's "Arabic": a prefix on the protected HTML it was sent


def fake_translator(change=lambda text: text, fail=None):
    """A Translator whose HTTP calls are answered in-process; `requests` records every call's texts."""
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        texts = [item["Text"] for item in json.loads(request.content)]
        requests.append({"texts": texts, "params": dict(request.url.params), "headers": request.headers})
        if fail:
            raise fail
        return httpx.Response(200, json=[{"translations": [{"text": ARABIC + change(t), "to": "ar"}]}
                                         for t in texts])

    translator = Translator("test-key", "uaenorth", URL, client=httpx.Client(transport=httpx.MockTransport(handler)))
    return translator, requests


@pytest.fixture(scope="module")
def goal(detected):
    return next(m for m, _ in detected if m["type"] == "goal")


@pytest.fixture(scope="module")
def big_chance(detected):
    return next(m for m, _ in detected if m["type"] == "big_chance")


def test_arabic_comes_back_in_one_batched_call_with_names_kept_in_latin(goal):
    translator, requests = fake_translator()
    english = template_versions(goal)
    arabic = translate_versions(english, translator, "ar", goal["event_id"])
    assert len(requests) == 1 and len(requests[0]["texts"]) <= 9  # all three personas' fields, deduplicated
    assert requests[0]["params"] == {"api-version": "3.0", "to": "ar", "textType": "html"}
    assert requests[0]["headers"]["ocp-apim-subscription-key"] == "test-key"
    assert any('<span class="notranslate">Rafe Ironsby</span>' in t for t in requests[0]["texts"])
    for persona in PERSONAS:
        assert arabic[persona]["fallback"] is False
        for field in ("headline", "detail", "why"):
            assert arabic[persona][field] == ARABIC + english[persona][field]  # spans stripped, names in Latin
    assert "Rafe Ironsby" in arabic["player"]["headline"] and "<span" not in arabic["player"]["headline"]


def test_english_passes_through_with_no_call(goal):
    translator, requests = fake_translator()
    english = template_versions(goal)
    out = translate_versions(english, translator, "en", goal["event_id"])
    assert requests == []
    assert all(out[p] == {k: english[p][k] for k in ("headline", "detail", "why")} | {"fallback": False}
               for p in PERSONAS)


def test_the_cache_prevents_repeat_calls(goal, big_chance):
    translator, requests = fake_translator()
    translate_versions(template_versions(goal), translator, "ar", goal["event_id"])
    translate_versions(template_versions(goal), translator, "ar", goal["event_id"])
    assert len(requests) == 1  # the same strings again: all from the cache
    translate_versions(template_versions(big_chance), translator, "ar", big_chance["event_id"])
    assert len(requests) == 2
    assert not set(requests[1]["texts"]) & set(requests[0]["texts"])  # only strings not seen before are sent


def test_a_dropped_number_falls_back_to_english_for_that_version(goal, caplog):
    english = template_versions(goal)
    target = english["casual"]["headline"]  # "Rafe Ironsby scores, Calderbrook lead 1-0"
    dropped = '<span class="notranslate">1</span>'  # the Translator "loses" the 1 of "1-0"
    translator, _ = fake_translator(change=lambda t: t.replace(dropped, "", 1) if " lead " in t else t)
    with caplog.at_level(logging.INFO, logger="deepkick.translate"):
        arabic = translate_versions(english, translator, "ar", goal["event_id"])
    assert "1-0" in target
    assert arabic["casual"] == {k: english["casual"][k] for k in ("headline", "detail", "why")} | {"fallback": True}
    assert arabic["analyst"]["fallback"] is False and arabic["analyst"]["headline"].startswith(ARABIC)
    assert f"event_id={goal['event_id']} persona=casual fallback=yes" in caplog.text


def test_arabic_indic_digits_come_back_as_western_digits(goal):
    indic = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")
    translator, _ = fake_translator(change=lambda t: t.translate(indic))
    english = template_versions(goal)
    arabic = translate_versions(english, translator, "ar", goal["event_id"])
    assert all(arabic[p]["fallback"] is False for p in PERSONAS)
    assert "9.2" in arabic["casual"]["detail"]  # checkable, and the number check passed


def test_a_timeout_falls_back_to_english_with_the_flag_after_the_usual_attempts(goal, caplog):
    translator, requests = fake_translator(fail=httpx.ReadTimeout("timed out"))
    english = template_versions(goal)
    with caplog.at_level(logging.INFO, logger="deepkick.translate"):
        arabic = translate_versions(english, translator, "ar", goal["event_id"])
    assert len(requests) == 2  # the first call plus one retry, as everywhere else
    for persona in PERSONAS:
        assert arabic[persona] == {k: english[persona][k] for k in ("headline", "detail", "why")} | {"fallback": True}
    assert "ReadTimeout" in caplog.text
    assert f"stage=translate event_id={goal['event_id']} duration_ms=" in caplog.text and "fallback=yes" in caplog.text


def test_a_malformed_reply_falls_back_too(goal):
    def handler(request):
        return httpx.Response(200, json={"error": "nope"})
    translator = Translator("k", "r", URL, client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert all(v["fallback"] for v in translate_versions(template_versions(goal), translator, "ar", "e").values())


def test_names_are_protected_and_restored():
    sent = protect("Rafe Ironsby scores, Calderbrook lead 1-0 & ASH 0-1 CAL")
    n = lambda text: f'<span class="notranslate">{text}</span>'  # noqa: E731
    assert sent == (f"{n('Rafe Ironsby')} scores, {n('Calderbrook')} lead {n('1')}-{n('0')} &amp; "
                    f"{n('ASH')} {n('0')}-{n('1')} {n('CAL')}")
    assert unprotect(sent) == "Rafe Ironsby scores, Calderbrook lead 1-0 & ASH 0-1 CAL"


def test_a_dropped_space_beside_a_protected_name_is_restored():
    # Seen from the live Translator: the space after the span went missing.
    returned = 'هذه هي أسرع جولة <span class="notranslate">Calderbrook Athletic</span>حتى الآن.'
    assert unprotect(returned) == "هذه هي أسرع جولة Calderbrook Athletic حتى الآن."
    assert unprotect("<span>Rafe Ironsby</span>تجري بسرعة") == "Rafe Ironsby تجري بسرعة"


@pytest.mark.parametrize("endpoint, url", [
    ("https://api.cognitive.microsofttranslator.com/", "https://api.cognitive.microsofttranslator.com/translate"),
    ("https://deepkick-tr.cognitiveservices.azure.com/",
     "https://deepkick-tr.cognitiveservices.azure.com/translator/text/v3.0/translate"),
    ("https://deepkick-tr.cognitiveservices.azure.com/translator/text/v3.0/translate",
     "https://deepkick-tr.cognitiveservices.azure.com/translator/text/v3.0/translate"),
])
def test_translate_url_accepts_the_global_or_a_resource_endpoint(endpoint, url):
    assert translate_url(endpoint) == url


def test_missing_settings_mean_no_translator(monkeypatch, caplog):
    for key in ("KEY", "REGION", "ENDPOINT"):
        monkeypatch.delenv(f"AZURE_TRANSLATOR_{key}", raising=False)
    with caplog.at_level(logging.WARNING, logger="deepkick.translate"):
        assert Translator.from_env() is None
    assert "AZURE_TRANSLATOR_* settings missing" in caplog.text


def test_a_429_backs_off_without_using_up_an_attempt(goal, monkeypatch):
    import deepkick.agents.narrative as narrative

    waits, replies = [], [httpx.Response(429, headers={"retry-after": "3"}), httpx.Response(429)]
    monkeypatch.setattr(narrative, "_sleep", waits.append)

    def handler(request):
        if replies:
            return replies.pop(0)
        texts = [item["Text"] for item in json.loads(request.content)]
        return httpx.Response(200, json=[{"translations": [{"text": ARABIC + t}]} for t in texts])

    translator = Translator("k", "r", URL, client=httpx.Client(transport=httpx.MockTransport(handler)))
    arabic = translate_versions(template_versions(goal), translator, "ar", goal["event_id"])
    assert waits == [3.0, 4.0]  # Retry-After first, then the schedule's next step
    assert all(v["fallback"] is False for v in arabic.values())  # two 429s, and still within the normal attempts
