"""Translate step of the Personalise stage: overlay text into Arabic with Azure AI Translator; English passes."""

from __future__ import annotations

import html
import logging
import os
import re
import time
from collections import Counter
from typing import Any

import httpx

from deepkick.agents import narrative
from deepkick.agents.narrative import _NUMBER, ATTEMPTS, RATE_LIMIT_BACKOFF_S, RETRY_AFTER_MAX_S, _retry_after
from deepkick.contracts import PERSONAS
from deepkick.simulator import load_league

log = logging.getLogger("deepkick.translate")

TIMEOUT_S = 10.0  # per attempt; the Translator answers a short batch in well under a second
FIELDS = ("headline", "detail", "why")
_LEAGUE = load_league()
# Fictional names stay in English in every language: player full names and surnames, club names, the short club
# forms the templates use ("Calderbrook") and the club codes ("ASH 0-1 CAL"). Longest first, so a full name wins
# over its part. A possessive's "'s" stays outside the name, for the Translator to render as grammar.
_PLAYERS = [p["name"] for c in _LEAGUE["clubs"] for p in c["starters"] + c["bench"]]
_NAMES = sorted({*_PLAYERS, *(n.split()[-1] for n in _PLAYERS)}
                | {n for c in _LEAGUE["clubs"] for n in (c["name"], c["name"].split()[0], c["short_name"], c["id"])},
                key=len, reverse=True)
# Football words the general-purpose Translator gets wrong ("shots" came back as الحقن, injections, and "Full-time"
# as دوام كامل, a full-time job), given in Arabic up front and protected like names.
GLOSSARY = {"Full-time": "نهاية المباراة", "Half-time": "نهاية الشوط الأول", "shots": "تسديدات",
            "shot": "تسديدة", "Shots": "التسديدات", "Shot": "التسديدة", "scores": "يسجل"}
_TERM = re.compile(r"\b(" + "|".join(re.escape(t) for t in sorted(GLOSSARY, key=len, reverse=True)) + r")\b")
_NAME = re.compile(r"\b(" + "|".join(re.escape(n) for n in _NAMES) + r")\b")
_SPAN = re.compile(r"<span[^>]*>(.*?)</span>", re.S)
# The Translator can drop the space beside a protected name or number ("Calderbrook Athleticحتى", "17Calderbrook").
# Latin letters, Arabic letters and digits never touch each other in correct text, so a space goes back wherever
# two of them do.
_LETTER = r"A-Za-z\u0600-\u06FF"
# A span the Translator butted up against the next word gets its space back before the tags are removed.
_SPAN_THEN_LETTER = re.compile(rf"(</span>)(?=[{_LETTER}])")
_SCRIPTS_TOUCH = re.compile(rf"(?<=[A-Za-z])(?=[\u0600-\u06FF])|(?<=[\u0600-\u06FF])(?=[A-Za-z])"
                            rf"|(?<=[{_LETTER}])(?=\d)|(?<=\d)(?=[{_LETTER}])")
# Arabic-Indic digits and decimal point back to Western, so numbers stay checkable.
_WESTERN = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹٫", "01234567890123456789.")


def translate_url(endpoint: str) -> str:
    """The v3 translate URL for the global Translator endpoint or a resource's own cognitiveservices endpoint."""
    base = endpoint.rstrip("/")
    if base.endswith("/translate"):
        return base
    if "cognitiveservices.azure.com" in base and "/translator/text/v3.0" not in base:
        base += "/translator/text/v3.0"
    return base + "/translate"


def placeholder(i: int) -> str:
    """The i-th name placeholder: capital letters only, so neither the number guard nor the Translator touches it."""
    return f"ZQ{chr(65 + i // 26)}{chr(65 + i % 26)}"


def protect(text: str, glossary: bool = False) -> tuple[str, list[str]]:
    """The text as HTML for the Translator, and the names it held, in order.

    Every fictional name becomes a placeholder in a notranslate span, so it comes back in English. Numbers are
    wrapped too: left alone, the Translator writes small counts as Arabic words ("1 shot" becomes "تسديدة واحدة"),
    which loses the digit the number check needs. With `glossary`, GLOSSARY terms go in already translated.
    """
    keep = lambda words: f'<span class="notranslate">{words}</span>'  # noqa: E731
    names: list[str] = []

    def hold(match: re.Match[str]) -> str:
        names.append(match.group(0))
        return keep(placeholder(len(names) - 1))

    escaped = html.escape(text, quote=False)
    if glossary:
        escaped = _TERM.sub(lambda m: keep(GLOSSARY[m.group(0)]), escaped)
    return _NUMBER.sub(lambda m: keep(m.group(0)), _NAME.sub(hold, escaped)), names


def unprotect(markup: str, names: list[str] = ()) -> str | None:
    """Translated HTML back to plain text with the English names swapped back in, or None if a placeholder is lost.

    Digits come back Western, and a space goes in wherever Latin, Arabic and digits touch.
    """
    spaced = _SPAN_THEN_LETTER.sub(r"\1 ", markup)
    text = _SCRIPTS_TOUCH.sub(" ", html.unescape(_SPAN.sub(r"\1", spaced)).translate(_WESTERN))
    for i, name in enumerate(names):
        if text.count(placeholder(i)) != 1:
            return None
        text = text.replace(placeholder(i), name)
    return text


def same_numbers(english: str, translated: str | None) -> bool:
    """True if there is a translation (every name came back) and it carries exactly the English text's numbers."""
    return translated is not None and Counter(_NUMBER.findall(english)) == Counter(_NUMBER.findall(translated))


class Translator:
    """Azure AI Translator client with a (text, language) cache; one HTTP call per batch of uncached strings."""

    def __init__(self, key: str, region: str, endpoint: str, client: httpx.Client | None = None,
                 timeout_s: float = TIMEOUT_S) -> None:
        self.url, self.timeout_s = translate_url(endpoint), timeout_s
        self.headers = {"Ocp-Apim-Subscription-Key": key, "Ocp-Apim-Subscription-Region": region}
        self.client = client or httpx.Client()
        self.cache: dict[tuple[str, str], str | None] = {}  # None: a name placeholder was lost

    @classmethod
    def from_env(cls) -> Translator | None:
        """A Translator from AZURE_TRANSLATOR_KEY, _REGION and _ENDPOINT, or None (and a warning) if any is unset."""
        key, region, endpoint = (os.environ.get(f"AZURE_TRANSLATOR_{k}") for k in ("KEY", "REGION", "ENDPOINT"))
        if not (key and region and endpoint):
            log.warning("stage=translate AZURE_TRANSLATOR_* settings missing; Arabic overlays will show English")
            return None
        return cls(key, region, endpoint)

    def translate(self, texts: list[str], lang: str) -> list[str | None]:
        """Translate texts into `lang` via the cache, None for a text whose names did not all come back; raise on
        any failure so the caller can fall back."""
        missing = list(dict.fromkeys(t for t in texts if (t, lang) not in self.cache))
        if missing:
            sent = [protect(t, glossary=lang == "ar") for t in missing]
            response = self.client.post(
                self.url, params={"api-version": "3.0", "to": lang, "textType": "html"}, headers=self.headers,
                json=[{"Text": markup} for markup, _ in sent], timeout=self.timeout_s)
            response.raise_for_status()
            results = response.json()
            if not isinstance(results, list) or len(results) != len(missing):
                raise ValueError(f"expected {len(missing)} translations, got {results!r:.200}")
            for text, (_, names), result in zip(missing, sent, results):
                self.cache[(text, lang)] = unprotect(result["translations"][0]["text"], names)
                if self.cache[(text, lang)] is None:
                    log.warning("stage=translate lang=%s fallback=yes: a name placeholder was lost in %r", lang, text)
        return [self.cache[(t, lang)] for t in texts]


def translate_batch(texts: list[str], translator: Translator | None, lang: str,
                    event_id: str | None) -> dict[str, str | None] | None:
    """{english: translated, or None if a name was lost} for one batch, or None once the Translator fails after
    ATTEMPTS tries; never raises.

    A 429 waits out RATE_LIMIT_BACKOFF_S (or Retry-After) without using up an attempt, as for model calls.
    """
    attempt, backoffs = 1, iter(RATE_LIMIT_BACKOFF_S)
    while translator and attempt <= ATTEMPTS:
        try:
            return dict(zip(texts, translator.translate(texts, lang)))
        except Exception as exc:  # timeouts, HTTP errors and malformed replies all get the retry, then English
            rate_limited = isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429
            backoff = next(backoffs, None) if rate_limited else None
            if backoff is not None:
                wait = min(RETRY_AFTER_MAX_S, _retry_after(exc) or backoff)
                log.warning("stage=translate event_id=%s rate_limited=429 retry_in_s=%.1f", event_id, wait)
                narrative._sleep(wait)
                continue
            log.warning("stage=translate event_id=%s attempt=%d failed: %s: %s",
                        event_id, attempt, type(exc).__name__, exc)
        attempt += 1
    return None


def translate_recap(versions: dict[str, dict[str, Any]], translator: Translator | None, lang: str,
                    event_id: str | None) -> dict[str, dict[str, Any]]:
    """The three persona recaps in `lang` in one batch; a recap whose numbers changed keeps its English text."""
    start = time.monotonic()
    translated = translate_batch([versions[p]["text"] for p in PERSONAS], translator, lang, event_id)
    out = {}
    for p in PERSONAS:
        english = versions[p]["text"]
        kept = translated is not None and same_numbers(english, translated[english])
        out[p] = {"text": translated[english] if kept else english, "fallback": not kept}
    log.info("stage=translate event_id=%s recap=yes duration_ms=%.0f fallback=%s", event_id,
             (time.monotonic() - start) * 1000, "yes" if any(v["fallback"] for v in out.values()) else "no")
    return out


def translate_versions(versions: dict[str, Any], translator: Translator | None, lang: str,
                       event_id: str | None) -> dict[str, dict[str, Any]]:
    """The three persona versions in `lang`. Each is wholly translated, or the English text with `fallback: true`.

    Evidence labels go in the same batch and translate with their version; evidence values are never touched.
    A version falls back if the Translator fails after ATTEMPTS tries or if any number was lost or altered.
    A 429 waits out RATE_LIMIT_BACKOFF_S (or Retry-After) without using up an attempt, as for model calls.
    "en" passes through with no call. Never raises.
    """
    english = {p: {k: versions[p][k] for k in FIELDS} for p in PERSONAS}
    evidence = {p: versions[p].get("evidence", []) for p in PERSONAS}
    if lang == "en":
        return {p: english[p] | {"evidence": evidence[p], "fallback": False} for p in PERSONAS}
    start = time.monotonic()
    labels = [e["label"] for p in PERSONAS for e in evidence[p]]
    translated = translate_batch([english[p][k] for p in PERSONAS for k in FIELDS] + labels, translator, lang,
                                 event_id)
    out = {}
    for p in PERSONAS:
        kept = translated is not None and all(same_numbers(t, translated[t]) for t in
                                              [english[p][k] for k in FIELDS] + [e["label"] for e in evidence[p]])
        out[p] = ({k: translated[english[p][k]] for k in FIELDS} if kept else dict(english[p])) | {
            "evidence": [e | {"label": translated[e["label"]]} for e in evidence[p]] if kept else evidence[p],
            "fallback": not kept}
        if translated is not None and not kept:
            log.warning("stage=translate event_id=%s persona=%s fallback=yes: a name or number was lost or altered",
                        event_id, p)
    log.info("stage=translate event_id=%s duration_ms=%.0f fallback=%s", event_id, (time.monotonic() - start) * 1000,
             "yes" if any(v["fallback"] for v in out.values()) else "no")
    return out
