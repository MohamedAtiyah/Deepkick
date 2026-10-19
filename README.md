# Deepkick

AI match intelligence for the "Inside the Game" hackathon. Deepkick turns a stream of synthetic football events into explained, timed, personalised broadcast overlays and match recaps.

All clubs, players, stadiums and referees are fictional and live in [`data/league.json`](data/league.json).

## Setup

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # then fill in the values
.venv/bin/python -m pytest
```

## 1. Ingest: match simulator

[`deepkick/simulator.py`](deepkick/simulator.py) plays a full 90-minute match between two clubs from `data/league.json` and emits about 1,100 events (`match_metadata`, `pass`, `shot`, `tackle`, `possession_change`, `pressure_event`, `player_run`) that follow the event contract in [`deepkick/contracts.py`](deepkick/contracts.py).

- **Seeded:** the same `--home`, `--away` and `--seed` always produce the same match, event ids and timestamps.
- **Coordinates** are 0–100 on both axes, oriented so the acting team always attacks towards `x=100`.
- **`context`** on each event is the match state after that event (score keyed by club id, team in possession, pressure index 0–1).
- `match_metadata` events mark `kickoff` (with lineups, stadium, referee), `half_time`, `second_half` and `full_time`.

```bash
# Print the match as JSON lines
.venv/bin/python -m deepkick.simulator --home ASH --away CAL --seed 42

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
