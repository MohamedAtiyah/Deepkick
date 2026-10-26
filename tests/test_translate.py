"""Tests for the translate step with a mocked Azure AI Translator (httpx MockTransport, no network)."""

import json
import logging
import re

import httpx
import pytest

from deepkick.agents.persona import template_versions
from deepkick.agents.translate import (_NAME, Translator, placeholder, protect, translate_recap, translate_url,
                                       translate_versions, unprotect)
from deepkick.contracts import PERSONAS

URL = "https://api.cognitive.microsofttranslator.com"
ARABIC = "ترجمة: "  # the fake Translator's "Arabic": a prefix, and every Latin letter outside a protected span
_SPANNED = re.compile(r"(<span[^>]*>.*?</span>)")
_LATIN_WORD = re.compile(r"[A-Za-z]+(?:-[A-Za-z]+)*")


def arabize(markup):
    """The fake translation: Latin letters outside notranslate spans become Arabic ones; spans and entities stay."""
    turn = lambda m: m.group(0) if m.group(0).startswith("&") else chr(0x0627 + ord(m.group(0).lower()) % 20)  # noqa: E731
    return "".join(part if _SPANNED.fullmatch(part) else re.sub(r"&\w+;|[A-Za-z]", turn, part)
                   for part in _SPANNED.split(markup))


def as_arabic(text, translator=None):
    """What the fake Translator makes of `text`: Arabic words and glossary terms, English names, the ARABIC prefix."""
    markup, names = protect(text, glossary=True)
    return unprotect(ARABIC + arabize(markup), names)


def fake_translator(change=lambda text: text, fail=None):
    """A Translator whose HTTP calls are answered in-process; `requests` records every translate call's texts.

    `change` edits each text the Translator was sent before it is "translated" (to lose a number or a name)."""
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        texts = [item["Text"] for item in json.loads(request.content)]
        requests.append({"texts": texts, "params": dict(request.url.params), "headers": request.headers})
        if fail:
            raise fail
        return httpx.Response(200, json=[{"translations": [{"text": ARABIC + arabize(change(t)), "to": "ar"}]}
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


def test_arabic_comes_back_in_one_batched_call_with_names_in_english(goal):
    translator, requests = fake_translator()
    english = template_versions(goal)
    arabic = translate_versions(english, translator, "ar", goal["event_id"])
    labels = {e["label"] for p in PERSONAS for e in english[p]["evidence"]}
    # All three personas' fields and every evidence label, deduplicated, in one call.
    assert len(requests) == 1 and len(requests[0]["texts"]) <= 9 + len(labels)
    assert requests[0]["params"] == {"api-version": "3.0", "to": "ar", "textType": "html"}
    assert requests[0]["headers"]["ocp-apim-subscription-key"] == "test-key"
    # Names go to the Translator as placeholders, never as text it could translate or transliterate.
    assert any(f'<span class="notranslate">{placeholder(0)}</span>' in t for t in requests[0]["texts"])
    assert not any("Ironsby" in t or "Calderbrook" in t for t in requests[0]["texts"])
    for persona in PERSONAS:
        assert arabic[persona]["fallback"] is False
        for field in ("headline", "detail", "why"):
            assert arabic[persona][field] == as_arabic(english[persona][field], translator)  # spans stripped
        # Labels are translated; values are the English evidence's values, untouched.
        assert [e["label"] for e in arabic[persona]["evidence"]] == [as_arabic(e["label"], translator)
                                                                      for e in english[persona]["evidence"]]
        assert [e["value"] for e in arabic[persona]["evidence"]] == [e["value"] for e in english[persona]["evidence"]]
    headline = arabic["player"]["headline"]
    assert "Rafe Ironsby" in headline and "ZQ" not in headline and "<span" not in headline


def _english_words(text):
    """The Latin-script words in a text."""
    return _LATIN_WORD.findall(text)


def test_every_roster_name_stays_in_english_in_every_arabic_overlay_and_recap(detected):
    translator, _ = fake_translator()
    for moment, _ in detected:
        english = template_versions(moment)
        arabic = translate_versions(english, translator, "ar", moment["event_id"])
        texts = [(english[p][k], arabic[p][k]) for p in PERSONAS for k in ("headline", "detail", "why")]
        texts += [(e["label"], a["label"]) for p in PERSONAS
                  for e, a in zip(english[p]["evidence"], arabic[p]["evidence"])]
        for source, translated in texts:
            names = _NAME.findall(source)
            assert all(n in translated for n in names), (source, translated)
            # Everything else became Arabic: the only English words left are the names.
            assert set(_english_words(translated)) <= {w for n in names for w in n.split()}, translated
    recap = {p: {"text": "Full-time: Ashvale Wanderers 3-5 Calderbrook Athletic. Rafe Ironsby's double led them.",
                 "fallback": False} for p in PERSONAS}
    for p, version in translate_recap(recap, translator, "ar", "e").items():
        assert not version["fallback"] and all(n in version["text"] for n in
                                               ("Ashvale Wanderers", "Calderbrook Athletic", "Rafe Ironsby"))


def test_possessives_keep_the_english_name_and_leave_the_s_to_the_translator():
    markup, names = protect("Rafe Ironsby's shot and Ironsby’s finish")
    assert names == ["Rafe Ironsby", "Ironsby"]
    assert "</span>'s" in markup and "</span>’s" in markup  # the possessive is grammar for the Translator
    arabic = as_arabic("It was Ironsby's goal.")
    assert "Ironsby" in arabic and "'s" not in arabic


def test_a_lost_name_placeholder_falls_back_to_english_for_that_version(goal, caplog):
    english = template_versions(goal)
    lost = f'<span class="notranslate">{placeholder(0)}</span>'
    translator, _ = fake_translator(change=lambda t: t.replace(lost, "", 1) if t.startswith(lost) and " lead " in t
                                    else t)
    with caplog.at_level(logging.WARNING, logger="deepkick.translate"):
        arabic = translate_versions(english, translator, "ar", goal["event_id"])
    assert english["casual"]["headline"].startswith("Rafe Ironsby scores")
    assert arabic["casual"] == _english(english["casual"]) | {"fallback": True}
    assert arabic["analyst"]["fallback"] is False
    assert "a name placeholder was lost" in caplog.text
    assert unprotect(f"{ARABIC}{placeholder(0)} {placeholder(0)}", ["Rafe Ironsby"]) is None  # doubled is lost too


def test_football_terms_go_in_already_translated():
    sent, _ = protect("Full-time: Ashvale had 13 shots.", glossary=True)
    kept = lambda words: f'<span class="notranslate">{words}</span>'  # noqa: E731
    assert kept("نهاية المباراة") in sent and kept("تسديدات") in sent
    assert "shots" not in sent and "Full-time" not in sent
    assert "shots" in protect("Ashvale had 13 shots.")[0]  # English and other languages are not changed


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
    dropped = '<span class="notranslate">1</span>-'  # the Translator "loses" the 1 of "1-0"
    translator, _ = fake_translator(change=lambda t: t.replace(dropped, "-", 1) if " lead " in t else t)
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
    sent, names = protect("Rafe Ironsby scores, Calderbrook lead 1-0 & ASH 0-1 CAL")
    n = lambda text: f'<span class="notranslate">{text}</span>'  # noqa: E731
    p = placeholder
    assert sent == (f"{n(p(0))} scores, {n(p(1))} lead {n('1')}-{n('0')} &amp; {n(p(2))} {n('0')}-{n('1')} {n(p(3))}")
    assert names == ["Rafe Ironsby", "Calderbrook", "ASH", "CAL"]
    assert unprotect(sent, names) == "Rafe Ironsby scores, Calderbrook lead 1-0 & ASH 0-1 CAL"


def test_a_dropped_space_beside_a_protected_name_is_restored():
    # Seen from the live Translator: the space after the span went missing.
    returned = 'هذه هي أسرع جولة <span class="notranslate">Calderbrook Athletic</span>حتى الآن.'
    assert unprotect(returned) == "هذه هي أسرع جولة Calderbrook Athletic حتى الآن."
    assert unprotect("<span>Rafe Ironsby</span>تجري بسرعة") == "Rafe Ironsby تجري بسرعة"
    # Seen live: a protected span run into the next Arabic word.
    assert unprotect(f'هدف <span class="notranslate">{placeholder(0)}</span>في المباراة',
                     ["Lucan Ullaby"]) == "هدف Lucan Ullaby في المباراة"
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
        if replies:
            return replies.pop(0)
        texts = [item["Text"] for item in json.loads(request.content)]
        return httpx.Response(200, json=[{"translations": [{"text": ARABIC + t}]} for t in texts])

    translator = Translator("k", "r", URL, client=httpx.Client(transport=httpx.MockTransport(handler)))
    arabic = translate_versions(template_versions(goal), translator, "ar", goal["event_id"])
    assert waits == [3.0, 4.0]  # Retry-After first, then the schedule's next step
    assert all(v["fallback"] is False for v in arabic.values())  # two 429s, and still within the normal attempts
