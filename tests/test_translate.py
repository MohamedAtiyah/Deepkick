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


def spell(name):
    """The fake transliteration: each Latin letter becomes an Arabic letter, so a name is all Arabic script."""
    return "".join(chr(0x0627 + (ord(c.lower()) - ord("a")) % 20) if c.isalpha() else c for c in name)


STUCK = "Fenn Grimsdyke"  # the live service hands this full name back unchanged; word by word it spells it


def transliterated(request: httpx.Request) -> httpx.Response | None:
    """The fake Translator's answer to a transliterate request, or None for any other request."""
    if not request.url.path.endswith("/transliterate"):
        return None
    return httpx.Response(200, json=[{"text": item["Text"] if item["Text"] == STUCK else spell(item["Text"]),
                                      "script": "Arab"} for item in json.loads(request.content)])


def as_arabic(text, translator):
    """What the fake Translator makes of `text`: names in Arabic script, glossary terms, the ARABIC prefix."""
    return unprotect(ARABIC + protect(text, translator.names_for("ar"), glossary=True))


def fake_translator(change=lambda text: text, fail=None):
    """A Translator whose HTTP calls are answered in-process; `requests` records every translate call's texts."""
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        if (spelled := transliterated(request)) is not None:
            return spelled
        texts = [item["Text"] for item in json.loads(request.content)]
        requests.append({"texts": texts, "params": dict(request.url.params), "headers": request.headers})
        if fail:
            raise fail
        return httpx.Response(200, json=[{"translations": [{"text": ARABIC + change(t), "to": "ar"}]}
                                         for t in texts])

    translator = Translator("test-key", "uaenorth", URL, client=httpx.Client(transport=httpx.MockTransport(handler)))
    return translator, requests


def _english(version):
    """A version as translate_versions returns it untranslated: its text and its English evidence."""
    return {k: version[k] for k in ("headline", "detail", "why", "evidence")}


@pytest.fixture(scope="module")
def goal(detected):
    return next(m for m, _ in detected if m["type"] == "goal")


@pytest.fixture(scope="module")
def big_chance(detected):
    return next(m for m, _ in detected if m["type"] == "big_chance")


def test_arabic_comes_back_in_one_batched_call_with_names_in_arabic_script(goal):
    translator, requests = fake_translator()
    english = template_versions(goal)
    arabic = translate_versions(english, translator, "ar", goal["event_id"])
    labels = {e["label"] for p in PERSONAS for e in english[p]["evidence"]}
    # All three personas' fields and every evidence label, deduplicated, in one call.
    assert len(requests) == 1 and len(requests[0]["texts"]) <= 9 + len(labels)
    assert requests[0]["params"] == {"api-version": "3.0", "to": "ar", "textType": "html"}
    assert requests[0]["headers"]["ocp-apim-subscription-key"] == "test-key"
    assert any(f'<span class="notranslate">{spell("Rafe Ironsby")}</span>' in t for t in requests[0]["texts"])
    for persona in PERSONAS:
        assert arabic[persona]["fallback"] is False
        for field in ("headline", "detail", "why"):
            assert arabic[persona][field] == as_arabic(english[persona][field], translator)  # spans stripped
        # Labels are translated; values are the English evidence's values, untouched.
        assert [e["label"] for e in arabic[persona]["evidence"]] == [as_arabic(e["label"], translator)
                                                                      for e in english[persona]["evidence"]]
        assert [e["value"] for e in arabic[persona]["evidence"]] == [e["value"] for e in english[persona]["evidence"]]
    headline = arabic["player"]["headline"]
    assert spell("Rafe Ironsby") in headline and "Rafe" not in headline and "<span" not in headline


def test_names_are_transliterated_once_club_codes_stay_and_a_failure_keeps_latin(goal, monkeypatch):
    translator, _ = fake_translator()
    names = translator.names_for("ar")
    assert names["Calderbrook Athletic"] == spell("Calderbrook Athletic") and "ASH" not in names
    assert names[STUCK] == f"{spell('Fenn')} {spell('Grimsdyke')}"  # retried word by word
    assert translator.names_for("ar") is names and translator.names_for("en") == {}  # cached; English untouched
    calls = []

    def down(request):
        calls.append(request.url.path)
        return httpx.Response(503)

    broken = Translator("k", "r", URL, client=httpx.Client(transport=httpx.MockTransport(down)))
    assert broken.names_for("ar") == {} and broken.names_for("ar") == {}
    assert calls == ["/transliterate"]  # not retried on every translation
    assert protect("Rafe Ironsby", {}) == '<span class="notranslate">Rafe Ironsby</span>'  # Latin, as before


def test_football_terms_go_in_already_translated():
    sent = protect("Full-time: Ashvale had 13 shots.", glossary=True)
    kept = lambda words: f'<span class="notranslate">{words}</span>'  # noqa: E731
    assert kept("نهاية المباراة") in sent and kept("تسديدات") in sent
    assert "shots" not in sent and "Full-time" not in sent
    assert "shots" in protect("Ashvale had 13 shots.")  # English and other languages are not changed


def test_english_passes_through_with_no_call(goal):
    translator, requests = fake_translator()
    english = template_versions(goal)
    out = translate_versions(english, translator, "en", goal["event_id"])
    assert requests == []
    assert all(out[p] == _english(english[p]) | {"fallback": False}
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
    assert arabic["casual"] == _english(english["casual"]) | {"fallback": True}
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
        assert arabic[persona] == _english(english[persona]) | {"fallback": True}
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
    # Seen live once names were in Arabic script: the name run into the next Arabic word ("أولابيفي").
    assert unprotect('هدف <span class="notranslate">أولابي</span>في المباراة') == "هدف أولابي في المباراة"
    # Also seen live: a number run into the next name ("17Calderbrook").
    assert unprotect('على <span class="notranslate">17</span>Calderbrook.') == "على 17 Calderbrook."
    assert unprotect("سجل 2-3 و 9.2 م") == "سجل 2-3 و 9.2 م"  # scores and decimals are left alone


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
        if (spelled := transliterated(request)) is not None:
            return spelled
        if replies:
            return replies.pop(0)
        texts = [item["Text"] for item in json.loads(request.content)]
        return httpx.Response(200, json=[{"translations": [{"text": ARABIC + t}]} for t in texts])

    translator = Translator("k", "r", URL, client=httpx.Client(transport=httpx.MockTransport(handler)))
    arabic = translate_versions(template_versions(goal), translator, "ar", goal["event_id"])
    assert waits == [3.0, 4.0]  # Retry-After first, then the schedule's next step
    assert all(v["fallback"] is False for v in arabic.values())  # two 429s, and still within the normal attempts
