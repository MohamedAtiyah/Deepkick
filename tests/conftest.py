"""Shared pytest fixtures: the fixed-seed replay file."""

import json
from pathlib import Path

import pytest

REPLAY_PATH = Path(__file__).parent / "fixtures" / "replay_ASH-CAL-42.jsonl"


@pytest.fixture(scope="session")
def replay() -> list[dict]:
    """Events of ASH v CAL, seed 42, as written by `python -m deepkick.simulator --seed 42 --out ...`."""
    return [json.loads(line) for line in REPLAY_PATH.read_text().splitlines()]
