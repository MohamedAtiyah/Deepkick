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

GitHub Actions ([`.github/workflows/tests.yml`](.github/workflows/tests.yml)) runs the same tests on Python 3.11 for every push and pull request.

## 1. Ingest: match simulator

[`deepkick/simulator.py`](deepkick/simulator.py) plays the 90-minute fixture from `data/league.json` and emits about 1,500 events (`match_metadata`, `pass`, `shot`, `tackle`, `possession_change`, `pressure_event`, `player_run`) that follow the event contract in [`deepkick/contracts.py`](deepkick/contracts.py).

- **Seeded:** the same `--seed` always produces the same match, event ids and timestamps.
- **Slot-based:** the player who receives, presses or tackles is picked by how close their formation slot is to the ball (outfield slots shift up and down with play), so strikers shoot and centre-backs defend.
- **Pass difficulty:** every pass carries `difficulty` (0–1). It rises with pass length, a pass into the box, pressure on the passer and the number of opposition outfield players the pass goes past (line-breaking). Completion odds fall as difficulty rises.
- **Shot speed:** every shot carries `speed_kmh` (60–130). Longer shots are struck harder.
- **Distance covered:** every 5 minutes, and at the end of each half, each starter gets an off-ball `player_run` (`with_ball: false`) with the distance run since the last report. Outfield players cover about 9.5–10.5 km a match and keepers about 5 km, scaled by their stamina rating.
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

## 2. Interpret: Stats Agent and moment detector

### Stats Agent

[`deepkick/stats.py`](deepkick/stats.py) folds each event into running team and player stats. It is pure Python and makes no model calls. Its `snapshot()` is the match state that the Narrative Agent is allowed to quote, so every number in it is already rounded.

- **Team stats:** goals, shots, shots on target, xG, passes and pass accuracy, average pass length and difficulty, hard passes (difficulty ≥ 0.7) and their completion rate, top shot speed, tackles won, pressures won, and possession in seconds and as a percentage. Possession time goes to the team that held the ball between two events.
- **Player stats:** actions, passes, shots, goals, xG, top shot speed, tackles, pressures and distance covered.
- **Momentum:** each team's shots, xG, possession share and final-third entries (completed passes or carries from before x = 70 m to beyond it) over the last 5 minutes of match clock, plus a momentum `value`: the team's share (0–100) of `2 × shots + 10 × xG + final-third entries + 5 × possession share`. The two teams' values add up to 100.
- **Live context:** the current `pressure_index` (pressure on the team in possession, 0–1) and `possession_team`.
- **Failure recovery:** events are validated against the contract. A malformed event is skipped, and a redelivered event (Event Hubs delivers at least once) is ignored by `event_id`. Neither changes the state, and both are logged with `fallback=yes`.
- **Logging:** each handoff logs `stage=stats event_id=… duration_ms=… fallback=yes|no`.

```bash
# Replay a match through the Stats Agent and print the final snapshot
.venv/bin/python -m deepkick.stats tests/fixtures/replay_ASH-CAL-42.jsonl
```

### Moment detector

[`deepkick/moments.py`](deepkick/moments.py) looks at each event the Stats Agent accepted, plus the snapshot taken after it, and flags the moments worth an overlay. It is pure Python and makes no model calls.

| Moment | When | Priority |
|---|---|---|
| `goal` | any goal, labelled `opener`, `equaliser`, `go_ahead`, `extends_lead` or `pulls_back` | 3 |
| `full_time` | final whistle | 3 |
| `big_chance` | a missed shot with xG ≥ 0.2 | 2 |
| `pressure_spell` | a team's third shot within 5 minutes (at most once per 10 minutes per team) | 2 |
| `half_time` | half-time whistle | 2 |
| `momentum_swing` | a team's momentum value reaches 70 after the other team was last to reach 70 (from 5:00 on) | 2 |
| `high_press_win` | a successful press at least 75 m up the pitch | 1 |
| `speed_milestone` | a shot struck at 120 km/h or faster | 1 |
| `distance_milestone` | a player passes 5, 8 or 10 km covered | 1 |
| `pass_milestone` | a player's 50th or 75th pass | 1 |

Each moment has `moment_id, match_id, event_id, type, match_clock, half, team, player, priority, facts`. `facts` copies numbers from the event and the Stats Agent snapshot and nothing else, so the Narrative Agent can only quote real match state. The only exceptions are labels: a distance milestone's `milestone_km` and a spell's or swing's `window_minutes`. A `momentum_swing` belongs to the team that took momentum, so its `player` is `null`. Each handoff logs `stage=moments event_id=… moments=… duration_ms=… fallback=no`.

```bash
# Print the moments of a replayed match as JSON lines
.venv/bin/python -m deepkick.moments tests/fixtures/replay_ASH-CAL-42.jsonl
```

**Azure service:** none directly. On Day 2 the orchestrator reads Azure Event Hubs, calls `StatsAgent.update` and then `MomentDetector.detect` for each event, and sends the moments to the Narrative Agent.
