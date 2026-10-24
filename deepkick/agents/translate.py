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
# Fictional names are never translated: full player and club names, the short club forms the templates use
# ("Calderbrook") and the club codes in scores ("ASH 0-1 CAL"). Longest first, so a full name wins over its part.
_CODES = {c["id"] for c in _LEAGUE["clubs"]}
_NAMES = sorted({p["name"] for c in _LEAGUE["clubs"] for p in c["starters"] + c["bench"]}
                | {c["name"] for c in _LEAGUE["clubs"]} | {c["name"].split()[0] for c in _LEAGUE["clubs"]}
                | _CODES, key=len, reverse=True)
TRANSLITERATE_BATCH = 10  # the Translator's limit of texts per transliterate request
NAMES_RETRY_S = 60.0
# Football words the general-purpose Translator gets wrong ("shots" came back as الحقن, injections, and "Full-time"
# as دوام كامل, a full-time job), given in Arabic up front and protected like names.
GLOSSARY = {"Full-time": "نهاية المباراة", "Half-time": "نهاية الشوط الأول", "shots": "تسديدات",
            "shot": "تسديدة", "Shots": "التسديدات", "Shot": "التسديدة", "scores": "يسجل"}
_TERM = re.compile(r"\b(" + "|".join(re.escape(t) for t in sorted(GLOSSARY, key=len, reverse=True)) + r")\b")
_NAME = re.compile(r"\b(" + "|".join(re.escape(n) for n in _NAMES) + r")\b")
_SPAN = re.compile(r"<span[^>]*>(.*?)</span>", re.S)
_LATIN = re.compile(r"[A-Za-z]")
# The Translator can drop the space beside a protected name or number ("Calderbrook Athleticحتى", "17Calderbrook").
# Latin letters, Arabic letters and digits never touch each other in correct text, so a space goes back wherever
# two of them do.
_LETTER = r"A-Za-z\u0600-\u06FF"
# With names now in Arabic script the same drop is Arabic touching Arabic ("أولابيفي"), so a span that the
# Translator butted up against the next word gets its space back before the tags are removed.
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


def protect(text: str, names: dict[str, str] | None = None, glossary: bool = False) -> str:
    """The text as HTML, with every fictional name and every number wrapped so the Translator leaves them untouched.

    With `names`, a name is swapped for its spelling in the target script (the same spelling every time); with
    `glossary`, GLOSSARY terms go in already translated. Numbers are protected too: left alone, the Translator
    writes small counts as Arabic words ("1 shot" becomes "تسديدة واحدة"), which loses the digit the number check needs.
    """
    keep = lambda words: f'<span class="notranslate">{words}</span>'  # noqa: E731
    escaped = html.escape(text, quote=False)
    if glossary:
        escaped = _TERM.sub(lambda m: keep(GLOSSARY[m.group(0)]), escaped)
    named = _NAME.sub(lambda m: keep((names or {}).get(m.group(0), m.group(0))), escaped)
    return _NUMBER.sub(lambda m: keep(m.group(0)), named)


def unprotect(markup: str) -> str:
    """Translated HTML back to plain text, with Western digits and a space wherever Latin, Arabic and digits touch."""
    spaced = _SPAN_THEN_LETTER.sub(r"\1 ", markup)
    return _SCRIPTS_TOUCH.sub(" ", html.unescape(_SPAN.sub(r"\1", spaced)).translate(_WESTERN))


def same_numbers(english: str, translated: str) -> bool:
    """True if the translation carries exactly the numbers of the English text, none lost or altered."""
    return Counter(_NUMBER.findall(english)) == Counter(_NUMBER.findall(translated))


class Translator:
    """Azure AI Translator client with a (text, language) cache; one HTTP call per batch of uncached strings."""

    def __init__(self, key: str, region: str, endpoint: str, client: httpx.Client | None = None,
                 timeout_s: float = TIMEOUT_S) -> None:
        self.url, self.timeout_s = translate_url(endpoint), timeout_s
        self.transliterate_url = self.url[: -len("translate")] + "transliterate"
        self.names: dict[str, dict[str, str]] = {}  # lang -> {English name: name in that language's script}
        self._names_retry_at = 0.0  # after a failed transliteration, wait before trying again
        self.headers = {"Ocp-Apim-Subscription-Key": key, "Ocp-Apim-Subscription-Region": region}
        self.client = client or httpx.Client()
        self.cache: dict[tuple[str, str], str] = {}

    @classmethod
    def from_env(cls) -> Translator | None:
        """A Translator from AZURE_TRANSLATOR_KEY, _REGION and _ENDPOINT, or None (and a warning) if any is unset."""
        key, region, endpoint = (os.environ.get(f"AZURE_TRANSLATOR_{k}") for k in ("KEY", "REGION", "ENDPOINT"))
        if not (key and region and endpoint):
            log.warning("stage=translate AZURE_TRANSLATOR_* settings missing; Arabic overlays will show English")
            return None
        return cls(key, region, endpoint)

    def names_for(self, lang: str) -> dict[str, str]:
        """Every league name in `lang`'s script, transliterated once and reused so a name is always spelled the same.

        Club codes stay as they are (scores read "ASH 0-1 CAL"). If transliteration fails, names stay in Latin
        script, as before, and it is tried again after NAMES_RETRY_S rather than on every translation.
        """
        if lang != "ar":
            return {}
        if lang not in self.names:
            if time.monotonic() < self._names_retry_at:
                return {}
            names = [n for n in _NAMES if n not in _CODES]
            try:
                spelled = self._transliterate(names, lang)
                # The service hands some full names back unchanged ("Fenn Grimsdyke"); word by word it spells them.
                stuck = [n for n, s in spelled.items() if _LATIN.search(s)]
                words = self._transliterate(sorted({w for n in stuck for w in n.split()}), lang)
                spelled |= {n: " ".join(words.get(w, w) for w in n.split()) for n in stuck}
                self.names[lang] = spelled
                latin = sorted(n for n, s in spelled.items() if _LATIN.search(s))
                log.info("stage=translate transliterated=%d names lang=%s", len(spelled), lang)
                if latin:
                    log.warning("stage=translate %d names stay partly in Latin script: %s", len(latin), latin)
            except Exception as exc:  # names in Latin script are still correct, just less natural to read
                self._names_retry_at = time.monotonic() + NAMES_RETRY_S
                log.warning("stage=translate transliteration failed, names stay in Latin script: %s: %s",
                            type(exc).__name__, exc)
                return {}
        return self.names[lang]

    def _transliterate(self, texts: list[str], lang: str) -> dict[str, str]:
        """{text: the text in `lang`'s script} from /transliterate, in batches; raises on any failure."""
        out: dict[str, str] = {}
        for i in range(0, len(texts), TRANSLITERATE_BATCH):
            batch = texts[i:i + TRANSLITERATE_BATCH]
            response = self.client.post(
                self.transliterate_url, headers=self.headers, timeout=self.timeout_s, json=[{"Text": t} for t in batch],
                params={"api-version": "3.0", "language": lang, "fromScript": "Latn", "toScript": "Arab"})
            response.raise_for_status()
            out |= {t: r["text"] for t, r in zip(batch, response.json(), strict=True)}
        return out

    def translate(self, texts: list[str], lang: str) -> list[str]:
        """Translate texts into `lang` via the cache; raise on any failure so the caller can fall back."""
        missing = list(dict.fromkeys(t for t in texts if (t, lang) not in self.cache))
        if missing:
            names = self.names_for(lang)
            response = self.client.post(
                self.url, params={"api-version": "3.0", "to": lang, "textType": "html"}, headers=self.headers,
                json=[{"Text": protect(t, names, glossary=lang == "ar")} for t in missing], timeout=self.timeout_s)
            response.raise_for_status()
            results = response.json()
            if not isinstance(results, list) or len(results) != len(missing):
                raise ValueError(f"expected {len(missing)} translations, got {results!r:.200}")
            for text, result in zip(missing, results):
                self.cache[(text, lang)] = unprotect(result["translations"][0]["text"])
        return [self.cache[(t, lang)] for t in texts]


def translate_batch(texts: list[str], translator: Translator | None, lang: str,
                    event_id: str | None) -> dict[str, str] | None:
    """{english: translated} for one batch, or None once the Translator fails after ATTEMPTS tries; never raises.

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
        kept = translated is not None and all(same_numbers(english[p][k], translated[english[p][k]]) for k in FIELDS)
        out[p] = ({k: translated[english[p][k]] for k in FIELDS} if kept else dict(english[p])) | {
            "evidence": [e | {"label": translated[e["label"]]} for e in evidence[p]] if kept else evidence[p],
            "fallback": not kept}
        if translated is not None and not kept:
            log.warning("stage=translate event_id=%s persona=%s fallback=yes: a number was lost or altered",
                        event_id, p)
    log.info("stage=translate event_id=%s duration_ms=%.0f fallback=%s", event_id, (time.monotonic() - start) * 1000,
             "yes" if any(v["fallback"] for v in out.values()) else "no")
    return out
