"""Tests for the chat's match tools on the seed-42 replay: every tool, the 20:00 cutoff and bad input."""

import pytest

from deepkick.agents.match_tools import (MatchData, compare_players, find_player, goals, list_moments, player_stats,
                                         run_tool, shots, team_stats, top_sprints)
from deepkick.simulator import clock_seconds


@pytest.fixture(scope="module")
def full(replay, detected):
    return MatchData(replay, [m for m, _ in detected])


@pytest.fixture(scope="module")
def at_20(replay, detected):
    return MatchData(replay, [m for m, _ in detected], until="20:00")


def test_find_player_full_surname_partial_case_and_accents(full):
    assert find_player(full, "Rafe Ironsby")["player"]["id"] == "CAL09"
    assert find_player(full, "ironsby")["player"]["id"] == "CAL09"  # surname, any case
    assert find_player(full, "  IRONSB ")["player"]["id"] == "CAL09"  # partial
    assert find_player(full, "Ìrönsby")["player"]["id"] == "CAL09"  # accents ignored
    assert find_player(full, "Rafe")["player"]["name"] == "Rafe Ironsby"  # first name


def test_find_player_ambiguous_and_unknown(full):
    ambiguous = find_player(full, "ar")
    assert "player" not in ambiguous and len(ambiguous["candidates"]) > 1
    assert all("ar" in c["name"].lower() for c in ambiguous["candidates"])
    assert "error" in find_player(full, "Lionel Messi") and "error" in find_player(full, "  ")


def test_player_stats_counts_real_runs_not_tracking_summaries(full):
    stats = player_stats(full, "CAL09")
    assert stats["player"]["name"] == "Rafe Ironsby" and stats["goals"] == 2 and stats["shots"] == 6
    assert stats["passes_completed"] <= stats["passes_attempted"]
    assert stats["runs"] == 3 and stats["top_speed_kmh"] == 20.2  # a summary's 9 km/h average is not a run
    assert stats["distance_m"] > 9000 and 0 < stats["minutes_involved"] <= 90
    assert {"match_clock": "13:52", "type": "goal"} in stats["key_moments"]
    assert "error" in player_stats(full, "XYZ99")


def test_top_sprints_fastest_first_and_by_team(full):
    sprints = top_sprints(full, n=3)["sprints"]
    assert [s["speed_kmh"] for s in sprints] == sorted((s["speed_kmh"] for s in sprints), reverse=True)
    assert sprints[0] == {"player": "Arlo Thistlewood", "team": "ASH", "speed_kmh": 33.0, "distance_m": 22.4,
                          "match_clock": "61:10"}
    assert {s["team"] for s in top_sprints(full, n=5, team="Calderbrook")["sprints"]} == {"CAL"}
    assert len(top_sprints(full, n=50)["sprints"]) == 10 and "error" in top_sprints(full, team="Nowhere FC")


def test_team_stats_by_code_or_name(full):
    cal = team_stats(full, "calderbrook")
    assert cal["team"] == "CAL" and (cal["goals"], cal["conceded"]) == (5, 3)
    assert cal["shots"] == 14 and 0 <= cal["possession_pct"] <= 100 and cal == team_stats(full, "CAL")
    assert "error" in team_stats(full, "Real Madrid")


def test_compare_players_side_by_side(full):
    both = compare_players(full, "CAL09", "ASH09")
    assert both["a"]["player"]["number"] == both["b"]["player"]["number"] == 9


def test_list_moments_newest_first_and_by_type(full):
    every = list_moments(full)
    clocks = [m["match_clock"] for m in every["moments"]]
    assert clocks == sorted(clocks, reverse=True) and every["count"] >= len(clocks)
    assert {m["type"] for m in list_moments(full, "goal")["moments"]} == {"goal"}
    assert "types_so_far" in list_moments(full, "own_goal")


def test_goals_in_order_with_scorer_minute_and_score(full):
    scored = goals(full)
    assert scored["score"] == {"ASH": 3, "CAL": 5} and len(scored["goals"]) == 8
    assert scored["goals"][0] == {"match_clock": "13:52", "minute": 14, "team": "CAL", "scorer": "Rafe Ironsby",
                                  "score_after": {"ASH": 0, "CAL": 1}}


def test_shots_totals_and_list(full):
    ash = shots(full, "ASH")
    assert (ash["total"], ash["goals"], ash["xg"]) == (18, 3, 1.81) and {s["team"] for s in ash["shots"]} == {"ASH"}
    assert shots(full)["total"] == 32


def test_the_20_minute_cutoff_hides_everything_later(at_20, full):
    assert clock_seconds({"match_clock": at_20.clock}) <= 20 * 60
    assert all(clock_seconds(e) <= 20 * 60 for e in at_20.events)
    assert goals(at_20)["score"] == {"ASH": 0, "CAL": 2} and len(goals(at_20)["goals"]) == 2
    assert all(s["match_clock"] <= "20:00" for s in top_sprints(at_20, n=10)["sprints"])
    # Caius Mossington scores at 19:51, 21:56 and 32:30: only the first has happened by 20:00.
    assert player_stats(at_20, "CAL07")["goals"] == 1 and player_stats(full, "CAL07")["goals"] == 3
    assert team_stats(at_20, "CAL")["goals"] == 2


def test_run_tool_turns_bad_calls_into_error_results(full):
    assert "error" in run_tool(full, "weather", {})
    assert "error" in run_tool(full, "player_stats", {"name": "Ironsby"})  # wrong argument name
    assert run_tool(full, "goals", {})["score"] == {"ASH": 3, "CAL": 5}
