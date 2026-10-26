"""Speech step: spoken commentary for an overlay with Azure AI Speech (REST text to speech), or None on failure."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from typing import Any
from xml.sax.saxutils import escape

import httpx

from deepkick.agents import narrative
from deepkick.agents.narrative import RATE_LIMIT_BACKOFF_S, RETRY_AFTER_MAX_S, _retry_after
from deepkick.contracts import PERSONAS

log = logging.getLogger("deepkick.speech")

TIMEOUT_S = 10.0  # per attempt
ATTEMPTS = 2
OUTPUT_FORMAT = "audio-24khz-48kbitrate-mono-mp3"
# One en-GB neural voice per persona.
# Confirm against the region with `python -m deepkick.agents.speech --check-voices`.
VOICES = {"analyst": "en-GB-RyanNeural", "casual": "en-GB-SoniaNeural", "player": "en-GB-ThomasNeural"}
# Persona style through SSML prosody only; the player voice speaks at its default.
PROSODY = {"analyst": {"rate": "-5%"}, "casual": {"rate": "+10%", "pitch": "+5%"}, "player": {}}
_XML = {"'": "&apos;", '"': "&quot;"}  # escape() already handles &, < and >


def ssml(text: str, persona: str) -> str:
    """The SSML for `text` in the persona's voice and prosody, with every character of the text XML-escaped."""
    voice = VOICES[persona]
    locale = "-".join(voice.split("-")[:2])  # "en-GB" from "en-GB-RyanNeural"
    body = escape(text, _XML)
    if PROSODY[persona]:
        attrs = " ".join(f'{name}="{value}"' for name, value in PROSODY[persona].items())
        body = f"<prosody {attrs}>{body}</prosody>"
    return (f'<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="{locale}">'
            f'<voice name="{voice}">{body}</voice></speak>')


def spoken_text(payload: dict[str, Any]) -> str:
    """What the commentary says: the overlay's headline, then its detail."""
    headline = payload["headline"].strip()
    end = "" if headline[-1:] in (".", "!", "?", "…") else "."
    return f"{headline}{end} {payload['detail'].strip()}"


class Speech:
    """Azure AI Speech text-to-speech client with an (overlay_id, persona) cache of the MP3 clips."""

    def __init__(self, key: str, region: str, client: httpx.Client | None = None,
                 timeout_s: float = TIMEOUT_S) -> None:
        self.url = f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1"
        self.voices_url = f"https://{region}.tts.speech.microsoft.com/cognitiveservices/voices/list"
        self.headers = {"Ocp-Apim-Subscription-Key": key, "Content-Type": "application/ssml+xml",
                        "X-Microsoft-OutputFormat": OUTPUT_FORMAT, "User-Agent": "deepkick"}
        self.client, self.timeout_s = client or httpx.Client(), timeout_s
        # shortcut: two viewers asking for the same new clip at once both call the service; add a per-key lock
        # if many viewers share one deployment.
        self.cache: dict[tuple[str, str, str], bytes] = {}

    @classmethod
    def from_env(cls) -> Speech | None:
        """A Speech client from AZURE_SPEECH_KEY and AZURE_SPEECH_REGION, or None (and a warning) if either is unset."""
        key, region = os.environ.get("AZURE_SPEECH_KEY"), os.environ.get("AZURE_SPEECH_REGION")
        if not (key and region):
            log.warning("stage=speech AZURE_SPEECH_* settings missing; commentary will stay silent")
            return None
        return cls(key, region)

    def synthesize(self, text: str, persona: str, *, overlay_id: str | None = None,
                   event_id: str | None = None) -> bytes | None:
        """The MP3 for `text` in the persona's voice, or None after ATTEMPTS failures; never raises.

        Clips are cached by (overlay_id, persona) when an overlay_id is given. A 429 waits out
        RATE_LIMIT_BACKOFF_S (or Retry-After) without using up an attempt, as for model calls.
        """
        key = (overlay_id, persona) if overlay_id else None
        if key in self.cache:
            return self.cache[key]
        start, audio = time.monotonic(), None
        attempt, backoffs = 1, iter(RATE_LIMIT_BACKOFF_S)
        while attempt <= ATTEMPTS:
            try:
                response = self.client.post(self.url, headers=self.headers, timeout=self.timeout_s,
                                            content=ssml(text, persona).encode("utf-8"))
                response.raise_for_status()
                if not response.content:
                    raise ValueError("empty audio")
                audio = response.content
                break
            except Exception as exc:  # timeouts, HTTP errors and empty replies all get the retry, then silence
                rate_limited = isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429
                backoff = next(backoffs, None) if rate_limited else None
                if backoff is not None:
                    wait = min(RETRY_AFTER_MAX_S, _retry_after(exc) or backoff)
                    log.warning("stage=speech event_id=%s rate_limited=429 retry_in_s=%.1f", event_id, wait)
                    narrative._sleep(wait)
                    continue
                log.warning("stage=speech event_id=%s attempt=%d failed: %s: %s",
                            event_id, attempt, type(exc).__name__, exc)
            attempt += 1
        if audio is not None and key:
            self.cache[key] = audio
        log.info("stage=speech event_id=%s persona=%s duration_ms=%.0f fallback=%s", event_id, persona,
                 (time.monotonic() - start) * 1000, "no" if audio is not None else "yes")
        return audio

    def missing_voices(self) -> list[str]:
        """The VOICES that the region's voices/list does not offer (raises if the list cannot be fetched)."""
        response = self.client.get(self.voices_url, headers={"Ocp-Apim-Subscription-Key":
                                                             self.headers["Ocp-Apim-Subscription-Key"]},
                                   timeout=self.timeout_s)
        response.raise_for_status()
        offered = {voice["ShortName"] for voice in response.json()}
        return sorted(set(VOICES.values()) - offered)


def main(argv: list[str] | None = None) -> None:
    """CLI: check that every voice in VOICES exists in the region, or speak one line to an MP3 file."""
    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("--check-voices", action="store_true", help="confirm VOICES against the region's voices/list")
    parser.add_argument("--say", help="text to speak")
    parser.add_argument("--persona", choices=PERSONAS, default="casual")
    parser.add_argument("--out", default="commentary.mp3")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    from dotenv import load_dotenv

    load_dotenv()
    speech = Speech.from_env()
    if speech is None:
        sys.exit("AZURE_SPEECH_KEY and AZURE_SPEECH_REGION must be set in .env")
    if args.check_voices:
        missing = speech.missing_voices()
        print("all voices found" if not missing else f"missing voices: {', '.join(missing)}")
        sys.exit(1 if missing else 0)
    if args.say:
        audio = speech.synthesize(args.say, args.persona)
        if audio is None:
            sys.exit("synthesis failed; see the log")
        with open(args.out, "wb") as f:
            f.write(audio)
        print(f"wrote {len(audio)} bytes to {args.out}")


if __name__ == "__main__":
    main()
