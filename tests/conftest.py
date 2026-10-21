"""Shared pytest fixtures and helpers: the fixed-seed replay file, its moments and a fake Foundry chat client."""

import json
from pathlib import Path
from types import SimpleNamespace

import httpx2
import openai
import pytest

REPLAY_PATH = Path(__file__).parent / "fixtures" / "replay_ASH-CAL-42.jsonl"


@pytest.fixture(scope="session")
def replay() -> list[dict]:
    """Events of ASH v CAL, seed 42, as written by `python -m deepkick.simulator --seed 42 --out ...`."""
    return [json.loads(line) for line in REPLAY_PATH.read_text().splitlines()]


@pytest.fixture(scope="session")
def detected(replay) -> list[tuple[dict, dict]]:
    """(moment, snapshot) pairs for every moment in the replay."""
    from deepkick.moments import MomentDetector
    from deepkick.stats import StatsAgent

    stats, detector, out = StatsAgent(), MomentDetector(), []
    for event in replay:
        if stats.update(event):
            snap = stats.snapshot()
            out += [(m, snap) for m in detector.detect(event, snap)]
    return out


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


def timeout_error() -> openai.APITimeoutError:
    """The exception the OpenAI SDK raises when a request times out."""
    return openai.APITimeoutError(request=httpx2.Request("POST", "https://example.test/openai/v1/chat/completions"))
