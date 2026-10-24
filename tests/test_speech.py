"""Tests for spoken commentary with a mocked Azure AI Speech endpoint (httpx MockTransport, no network)."""

import logging
import xml.etree.ElementTree as ET

import httpx
import pytest
from fastapi.testclient import TestClient

from deepkick.agents.speech import OUTPUT_FORMAT, PROSODY, VOICES, Speech, spoken_text, ssml
from deepkick.api import create_app
from deepkick.contracts import LANGS, PERSONAS
from deepkick.orchestrator import replay_events
from tests.conftest import REPLAY_PATH
from tests.test_api import _orchestrator, _wait_until

MP3 = b"ID3fake-mp3"
NS = {"s": "http://www.w3.org/2001/10/synthesis"}


def fake_speech(*statuses):
    """A Speech client answered in-process: one status per call (200 returns MP3), repeating the last."""
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        status = statuses[min(len(requests), len(statuses)) - 1]
        if isinstance(status, Exception):
            raise status
        return httpx.Response(status, content=MP3 if status == 200 else b"")

    speech = Speech("test-key", "uaenorth", client=httpx.Client(transport=httpx.MockTransport(handler)))
    return speech, requests


def test_voice_map_covers_every_persona_and_language():
    assert set(VOICES) == {(p, lang) for p in PERSONAS for lang in LANGS}
    assert len(set(VOICES.values())) == len(VOICES)  # one voice each
    assert all(VOICES[(p, "en")].startswith("en-GB-") and VOICES[(p, "ar")].startswith("ar-") for p in PERSONAS)
    assert all(voice.endswith("Neural") for voice in VOICES.values())
    assert PROSODY == {"analyst": {"rate": "-5%"}, "casual": {"rate": "+10%", "pitch": "+5%"}, "player": {}}


def test_ssml_escapes_apostrophes_ampersands_and_markup():
    text = "Calderbrook Athletic's Rafe Ironsby & Co. <score> \"2-1\""
    for persona in PERSONAS:
        doc = ssml(text, persona, "en")
        assert "&apos;" in doc and "&amp;" in doc and "&lt;score&gt;" in doc and "&quot;" in doc
        root = ET.fromstring(doc)  # well-formed, so the service will not reject it
        voice = root.find("s:voice", NS)
        assert root.get("{http://www.w3.org/XML/1998/namespace}lang") == "en-GB"
        assert voice.get("name") == VOICES[(persona, "en")]
        assert "".join(voice.itertext()) == text  # the words spoken are exactly the text
        prosody = voice.find("s:prosody", NS)
        assert (prosody.attrib if prosody is not None else {}) == PROSODY[persona]


def test_arabic_uses_an_arabic_voice_and_locale():
    root = ET.fromstring(ssml("ريف آيرونزبي يسجل", "casual", "ar"))
    assert root.get("{http://www.w3.org/XML/1998/namespace}lang") == "ar-SA"
    assert root.find("s:voice", NS).get("name") == VOICES[("casual", "ar")]


def test_spoken_text_is_the_headline_then_the_detail():
    assert spoken_text({"headline": "Rafe Ironsby scores!", "detail": "From 9.2 m."}) == \
        "Rafe Ironsby scores! From 9.2 m."  # no extra full stop after the headline's own punctuation
    assert spoken_text({"headline": "Rafe Ironsby scores", "detail": "From 9.2 m."}) == \
        "Rafe Ironsby scores. From 9.2 m."


def test_synthesis_posts_ssml_to_the_regional_endpoint_and_caches_by_overlay():
    speech, requests = fake_speech(200)
    assert speech.synthesize("Hello", "analyst", "en", overlay_id="o-analyst-en") == MP3
    assert speech.synthesize("Hello", "analyst", "en", overlay_id="o-analyst-en") == MP3
    assert len(requests) == 1  # the second is a cache hit
    request = requests[0]
    assert str(request.url) == "https://uaenorth.tts.speech.microsoft.com/cognitiveservices/v1"
    assert request.headers["X-Microsoft-OutputFormat"] == OUTPUT_FORMAT == "audio-24khz-48kbitrate-mono-mp3"
    assert request.headers["Content-Type"] == "application/ssml+xml"
    assert request.headers["Ocp-Apim-Subscription-Key"] == "test-key"
    assert ET.fromstring(request.content).find("s:voice", NS).get("name") == VOICES[("analyst", "en")]
    speech.synthesize("Hello", "casual", "en", overlay_id="o-casual-en")
    assert len(requests) == 2  # another persona is another clip


@pytest.mark.parametrize("failure", [500, httpx.ReadTimeout("timed out")])
def test_failure_returns_none_after_two_attempts_and_is_not_cached(failure, caplog):
    speech, requests = fake_speech(failure)
    with caplog.at_level(logging.INFO, logger="deepkick.speech"):
        assert speech.synthesize("Hello", "player", "en", overlay_id="o", event_id="E1") is None
    assert len(requests) == 2
    assert "stage=speech event_id=E1 persona=player lang=en duration_ms=" in caplog.text
    assert "fallback=yes" in caplog.text
    assert speech.cache == {}  # a later request may still succeed


def test_a_429_is_retried_without_using_an_attempt(monkeypatch):
    from deepkick.agents import narrative

    monkeypatch.setattr(narrative, "_sleep", lambda s: None)
    speech, requests = fake_speech(429, 429, 200)
    assert speech.synthesize("Hello", "casual", "ar") == MP3 and len(requests) == 3


def test_speech_endpoint_serves_mp3_404s_unknown_overlays_and_503s_failures():
    speech, _ = fake_speech(200)
    failing, _ = fake_speech(503)
    for voice, ok in ((speech, True), (failing, False)):
        with TestClient(create_app(_orchestrator(), replay_events(REPLAY_PATH), speech=voice)) as api:
            _wait_until(lambda: api.get("/healthz").json()["status"] == "finished")
            overlay = api.get("/recap", params={"persona": "casual", "lang": "en"}).json()[0]
            response = api.get(f"/speech/{overlay['overlay_id']}", params={"persona": "casual", "lang": "en"})
            if ok:
                assert response.status_code == 200 and response.headers["content-type"] == "audio/mpeg"
                assert response.content == MP3
            else:
                assert response.status_code == 503  # silent; the text overlay is unaffected
            assert api.get("/speech/nope-casual-en", params={"persona": "casual", "lang": "en"}).status_code == 404
            # The id must belong to the persona and language asked for.
            assert api.get(f"/speech/{overlay['overlay_id']}",
                           params={"persona": "analyst", "lang": "en"}).status_code == 404
            assert api.get(f"/speech/{overlay['overlay_id']}",
                           params={"persona": "coach", "lang": "en"}).status_code == 422
            assert api.get(f"/speech/{overlay['overlay_id']}",
                           params={"persona": "casual", "lang": "fr"}).status_code == 422


def test_without_speech_settings_every_clip_is_a_503():
    with TestClient(create_app(_orchestrator(), replay_events(REPLAY_PATH))) as api:
        _wait_until(lambda: api.get("/healthz").json()["status"] == "finished")
        overlay = api.get("/recap", params={"persona": "player", "lang": "ar"}).json()[0]
        clip = api.get(f"/speech/{overlay['overlay_id']}", params={"persona": "player", "lang": "ar"})
        assert clip.status_code == 503
