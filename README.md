# Deepkick

AI match intelligence for the "Inside the Game" hackathon. Deepkick turns a stream of synthetic football events into explained, timed, personalised broadcast overlays and match recaps.

All clubs, players, stadiums and referees are invented and live in [`data/league.json`](data/league.json):

- **Clubs:** Ashvale Wanderers (ASH, 4-4-2) and Calderbrook Athletic (CAL, 4-3-3), each with name, short name, colours, home stadium, manager, 11 starters (one per formation slot) and 5 bench players (GK, CB, CM and two forwards).
- **Players:** id, name, unique shirt number, FIFA-style position code (`GK`, `RB`, `RCB`, `CDM`, `LW`, `RST`…), preferred foot, and ratings from 1 to 99 for pace, passing, shooting, defending and stamina.
- **Formation slots:** each position code has a default pitch coordinate in metres (x 0–105, y 0–68, attacking left to right).
- **Match:** the fixture (`home`, `away`), its stadium and its referee.

## Setup

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # then fill in the values
.venv/bin/python -m pytest
```

## 1. Ingest: match simulator

[`deepkick/simulator.py`](deepkick/simulator.py) plays the 90-minute fixture from `data/league.json` and emits about 1,100 events (`match_metadata`, `pass`, `shot`, `tackle`, `possession_change`, `pressure_event`, `player_run`) that follow the event contract in [`deepkick/contracts.py`](deepkick/contracts.py).

- **Seeded:** the same `--seed` always produces the same match, event ids and timestamps.
- **Slot-based:** the player who receives, presses or tackles is picked by how close their formation slot is to the ball (outfield slots shift up and down with play), so strikers shoot and centre-backs defend.
- **Coordinates** are metres on a 105 × 68 pitch, oriented so the acting team always attacks towards `x=105`, the same frame as the formation slots.
- **`context`** on each event is the match state after that event (score keyed by club id, team in possession, pressure index 0–1).
- `match_metadata` events mark `kickoff` (with formations, starters and their slots, bench, managers, colours, stadium, referee), `half_time`, `second_half` and `full_time`.

```bash
# Print the match as JSON lines
.venv/bin/python -m deepkick.simulator --seed 42

# Write a replay file
.venv/bin/python -m deepkick.simulator --seed 42 --out replays/ASH-CAL-42.jsonl

# Stream to Azure Event Hubs at 30x real time (90 minutes in 3)
.venv/bin/python -m deepkick.simulator --seed 42 --send --speed 30
```

**Azure service:** Azure Event Hubs (Basic tier). `--send` reads `EVENTHUB_CONNECTION_STRING` and `EVENTHUB_NAME` from `.env`. Events are sent with the match id as partition key so they stay in order. Without `--speed` they go in batches of 200; with it they go one at a time, paced to the match clock. A failed send is logged with the event id and the run exits with an error (the SDK has already retried).

**Tests:** the fixture [`tests/fixtures/replay_ASH-CAL-42.jsonl`](tests/fixtures/replay_ASH-CAL-42.jsonl) is the seed-42 match. If you change the simulator on purpose, regenerate it:

```bash
.venv/bin/python -m deepkick.simulator --seed 42 --out tests/fixtures/replay_ASH-CAL-42.jsonl
```
