"""Tests for the fictional league data file."""

import pytest

from deepkick.simulator import load_league

LEAGUE = load_league()
RATINGS = {"pace", "passing", "shooting", "defending", "stamina"}


@pytest.fixture(params=LEAGUE["clubs"], ids=lambda c: c["id"])
def club(request):
    return request.param


def test_eleven_starters_match_the_formation(club):
    slots = club["formation"]["slots"]
    assert len(slots) == 11
    assert len(club["starters"]) == 11
    assert sorted(p["position"] for p in club["starters"]) == sorted(slots)


def test_shirt_numbers_are_unique_and_in_range(club):
    numbers = [p["number"] for p in club["starters"] + club["bench"]]
    assert len(numbers) == len(set(numbers))
    assert all(1 <= n <= 99 for n in numbers)


def test_bench_has_keeper_defender_midfielder_and_two_forwards(club):
    assert len(club["bench"]) == 5
    assert [p["position"] for p in club["bench"]][:3] == ["GK", "CB", "CM"]
    assert all(p["position"] in {"ST", "RW", "LW"} for p in club["bench"][3:])


def test_player_fields_and_ratings(club):
    for p in club["starters"] + club["bench"]:
        assert {"id", "name", "number", "position", "foot", "ratings"} <= p.keys()
        assert p["foot"] in {"left", "right"}
        assert p["ratings"].keys() == RATINGS
        assert all(1 <= v <= 99 for v in p["ratings"].values())


def test_slots_are_on_the_pitch(club):
    for xy in club["formation"]["slots"].values():
        assert 0 <= xy["x"] <= 105 and 0 <= xy["y"] <= 68


def test_match_references_clubs_and_names_are_unique():
    ids = {c["id"] for c in LEAGUE["clubs"]}
    assert {LEAGUE["match"]["home"], LEAGUE["match"]["away"]} <= ids
    names = [p["name"] for c in LEAGUE["clubs"] for p in c["starters"] + c["bench"]]
    assert len(names) == len(set(names))
