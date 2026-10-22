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
- **Run speed:** every `player_run` carries `speed_kmh`, which never goes above 37.
  - **Real runs:** the top speed is roughly 12–36 km/h. It rises with the run's length and is slightly lower with the ball.
  - **5-minute tracking summaries:** these report their average speed over the window, about 3–7 km/h.
  - **Separate RNG:** each run's speed comes from its own `random.Random` seeded with `(seed, event_id)`, so the match's main random stream is untouched. Adding speeds changed no other field of the seed-42 replay; `tests/test_simulator.py` checks this.
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

- **Team stats:** goals, shots, shots on target, xG, passes and pass accuracy, average pass length and difficulty, hard passes (difficulty ≥ 0.7) and their completion rate, top shot speed, top run speed, tackles won, pressures won, and possession in seconds and as a percentage. Possession time goes to the team that held the ball between two events.
- **Player stats:** actions, passes, shots, goals, xG, top shot speed, top run speed, tackles, pressures and distance covered.
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
| `speed_milestone` | a shot at 120 km/h or faster: the ball's speed. Facts are `shot_speed_kmh`, `shot_distance_m` (distance from goal), `outcome` and the player's and team's top shot speeds | 1 |
| `sprint_milestone` | a player's run at 30 km/h or faster, or a team's new fastest run from 25 km/h. Facts are `run_speed_kmh`, `run_distance_m` and `team_top_run_speed_kmh` | 1 |
| `distance_milestone` | the match's leading distance coverer passes another 3 km (3, 6, 9 km): three a match, down from 48 when every player was flagged at 5, 8 and 10 km | 1 |
| `pass_milestone` | a player's 50th or 75th pass | 1 |

Each moment has `moment_id, match_id, event_id, type, match_clock, half, team, player, priority, facts`. `facts` copies numbers from the event and the Stats Agent snapshot and nothing else, so the Narrative Agent can only quote real match state. The only exceptions are labels: a distance milestone's `milestone_km` and a spell's or swing's `window_minutes`. A `momentum_swing` belongs to the team that took momentum, so its `player` is `null`. Each handoff logs `stage=moments event_id=… moments=… duration_ms=… fallback=no`.

```bash
# Print the moments of a replayed match as JSON lines; a count per moment type goes to stderr
.venv/bin/python -m deepkick.moments tests/fixtures/replay_ASH-CAL-42.jsonl
```

The summary line shows the mix at a glance. For seed 42 it is `35 moments: sprint_milestone=6, goal=5, speed_milestone=4, momentum_swing=4, pressure_spell=4, high_press_win=4, big_chance=3, distance_milestone=3, half_time=1, full_time=1`.

**Azure service:** none directly. On Day 2 the orchestrator reads Azure Event Hubs, calls `StatsAgent.update` and then `MomentDetector.detect` for each event, and sends the moments to the Narrative Agent.

## 3. Explain: Narrative Agent

[`deepkick/agents/narrative.py`](deepkick/agents/narrative.py) takes one moment from the moment detector and the current Stats Agent snapshot, and says why the moment matters. It returns:

| Field | Content |
|---|---|
| `headline` | at most 70 characters |
| `why_it_matters` | one or two sentences |
| `theme` | set in code from the moment type, not by the model: `goal`/`full_time` → `result`, `big_chance` → `chaos`, `momentum_swing` → `control`, `pressure_spell`/`high_press_win` → `pressure_shift`, `half_time` → `rhythm`, the three milestones → `milestone` |
| `evidence` | the stats it used, as `{"stat": …, "value": …}` |
| `fallback` | `true` if the template wrote it instead of the model |

- **Model input:** the moment (type, clock, team, player name and position, facts) and a trimmed snapshot (score, clock, pressure index, team stats with momentum, and the moment's player). Event and player ids are left out.
- **No invented numbers:** the prompt tells the model to cite only numbers in that input, copied exactly. The reply is then checked. Every number in the headline, `why_it_matters` and `evidence` must appear in the input (the match clock counts too). Evidence can't be empty. Each item's `stat` must be a key of the moment's facts (`xg`, or `score.CAL` for a nested fact) and its `value` must be that fact's value. A reply that fails, or has the wrong shape or length, is rejected. A `theme` sent by the model is ignored.
- **Wording:** the prompt asks for plain broadcast English with no raw field names ("possession", "shots on target", "expected goals"), position codes spelled out, a headline of at most 10 words and a `why_it_matters` of at most 30 words, and normal punctuation (hyphens in scores like 0-2, apostrophes). It also tells the model to describe only what the facts show, with no claims about fatigue, confidence, form or intent. A shot speed is the ball's speed and a shot distance is the distance from goal; neither may be described as a player running.
- **Position codes:** after a reply passes the checks, any standalone position code the league uses (from `data/league.json`, such as `RCB` or `ST`) left in the headline or `why_it_matters` is replaced with words ("right centre-back", "striker"). Club codes such as `ASH` and `CAL` are left alone.
- **What is checked:** the validator guards the numbers and the evidence, plus the reply's shape and length. The wording rules above are limited by the prompt only, so a reply can still use a raw term or make an unsupported claim without any number in it.
- **Model call:** Chat Completions with JSON output, `reasoning_effort="low"` and `max_completion_tokens=1500` (reasoning tokens count against that cap).
- **Failure recovery:** each call has a 30-second timeout and one retry, so the worst case is about a minute before the template is used. Reasoning models such as gpt-5-mini answer a one-word probe in about 3 s but need much longer than 4 s on a real moment. After a timeout, an API error or a rejected reply on both tries, the agent returns a template narrative built straight from the moment facts, with `fallback: true`. It never raises. Every moment type has a template, and the tests run every template through the same number check as the model.
- **Logging:** at startup the agent logs the base URL path and the deployment once (`stage=narrative foundry base_path=… deployment=…`), never the key or host. Each handoff logs `stage=narrative event_id=… duration_ms=… fallback=yes|no`. Each failed attempt is logged with its reason.

```bash
# Explain the first 5 moments of priority 2 or more (template only if FOUNDRY_* is unset)
.venv/bin/python -m deepkick.agents.narrative tests/fixtures/replay_ASH-CAL-42.jsonl --limit 5
```

**Azure service:** a Microsoft Foundry reasoning model deployment (gpt-5-mini; `reasoning_effort` is sent on every call, which non-reasoning models such as gpt-4o-mini reject), called with the [`openai`](https://pypi.org/project/openai/) SDK on the resource's `/openai/v1` endpoint with an API key, as Microsoft's [Foundry SDK guide](https://learn.microsoft.com/en-us/azure/foundry/how-to/develop/sdk-overview) recommends for Chat Completions. Set `FOUNDRY_ENDPOINT`, `FOUNDRY_MODEL_DEPLOYMENT` and `FOUNDRY_API_KEY` in `.env`. `FOUNDRY_ENDPOINT` can be a bare host (`https://<resource>.openai.azure.com`), a project endpoint (`https://<resource>.services.ai.azure.com/api/projects/<name>`) or a full target URI copied from the portal. The agent keeps only the scheme, host and optional `/api/projects/<name>`, then adds `/openai/v1/`, so a pasted `/openai/.../chat/completions?api-version=…` or `/responses` path can't be doubled. If a setting is missing or the endpoint isn't an http(s) URL, every narrative uses the template.

**Tests:** [`tests/test_narrative.py`](tests/test_narrative.py) replaces the model client with a fake. It covers a good reply, invented numbers in text and in evidence, a timeout, endpoint normalisation (bare host, project endpoint and target URIs), player names in the template text, malformed JSON and bad fields, a retry that recovers, missing settings, and the template for every moment type.

## 4. Render: orchestrator (overlay API and broadcast view next)

[`deepkick/orchestrator.py`](deepkick/orchestrator.py) runs stages 2–5 over one shared match state with [Microsoft Agent Framework](https://pypi.org/project/agent-framework-core/) (`agent-framework-core` only; the `agent-framework` meta package pulls about 150 dependencies).

- **One consumer:** Event Hubs is on the Basic tier with one consumer group, so a single orchestrator reads the stream (`$Default`) and fans out in process.
- **Interpret in line:** each event goes through `StatsAgent.update` and `MomentDetector.detect` right away. Both are pure Python and take well under a millisecond, so ingest never waits for a model.
- **Overlay cap:** a match minute's moments are held until the clock moves past that minute, then cut to at most 3, dropping the lowest moment priority first (ties keep the earlier moment). Goals, half-time and full-time are never dropped. Each drop is logged with its moment id and priority. The cap runs before any model call, so a dropped moment costs nothing.
- **Explain and Personalise off the hot path:**
  - **Workers:** kept moments go on a queue served by 4 workers.
  - **Per-moment workflow:** each worker runs that moment through its own Agent Framework workflow: `ExplainExecutor` (Narrative Agent), then `PersonaliseExecutor` (Persona Agent). The blocking model calls run in threads.
  - **Why it's needed:** a narrative call takes 6–10 s and a persona call 6–12 s, and a seed-42 match has 35 moments, so running them one after another would take about 10 minutes.
  - **Timeouts and errors:** a timeout or error in one moment uses that moment's template fallback, and the other workers carry on.
- **Clock order:** finished overlays are buffered and released in match-clock (`show_at_ms`) order, even when a later moment finishes first.
- **Half-time and full-time:** these pass through as normal overlays until the recap agent exists.
- **Shared state:** `Orchestrator.snapshot()` returns the Stats Agent snapshot and `Orchestrator.history()` the released overlays. These are ready for the FastAPI `/state` and `/recap` endpoints, which aren't built yet.
- **Logging:** each moment's handoff logs `stage=orchestrator event_id=… duration_ms=… fallback=yes|no`. The run ends with `summary events=… moments=… overlays_emitted=… overlays_dropped=… fallbacks=… wall_s=…`, which is also printed as JSON on stderr.

```bash
# Replay a match file: needs no Azure at all (without FOUNDRY_* in .env, narratives and personas use templates)
.venv/bin/python -m deepkick.orchestrator --replay tests/fixtures/replay_ASH-CAL-42.jsonl --persona analyst

# Replay paced at 30x real time
.venv/bin/python -m deepkick.orchestrator --replay tests/fixtures/replay_ASH-CAL-42.jsonl --speed 30

# Live: read Azure Event Hubs from the latest event until full-time (start this, then the simulator with --send)
.venv/bin/python -m deepkick.orchestrator --live
```

Overlays print to stdout as JSON lines.

**Azure services by stage:**
- **Ingest (live mode):** Azure Event Hubs, using `EVENTHUB_CONNECTION_STRING` and `EVENTHUB_NAME`.
- **Interpret:** none.
- **Explain and Personalise:** the Microsoft Foundry model deployment (`FOUNDRY_*`).
- **Orchestration:** Microsoft Agent Framework, which runs in process.

**Next:** the FastAPI WebSocket (`/ws`) that streams these overlays, `/state` and `/recap`, and the React broadcast view that draws each overlay at its `show_at_ms` for its `duration_ms`.

**Tests:** [`tests/test_orchestrator.py`](tests/test_orchestrator.py) uses the seed-42 replay and a fake model client with controllable delays, and needs no network. It covers:
- overlays in match-clock order when work finishes out of order;
- one moment's model timeout not stalling the others;
- the cap holding on the replay and in isolation;
- goals, half-time and full-time never dropped;
- the same overlays for the same seed with 1 or 4 workers;
- handoff and summary logging;
- the replay CLI running with no Azure settings.

## 5. Personalise: Persona Agent

[`deepkick/agents/persona.py`](deepkick/agents/persona.py) takes the Narrative Agent's output for a moment (headline, `why_it_matters`, theme, evidence) and the moment's facts. It rewrites them for three viewers, then turns the right version into an overlay for each viewer profile.

- **One call, three versions:** one model call per moment returns `analyst`, `casual` and `player` versions, each with `headline`, `detail` and `why`. The result is cached by moment id, so switching persona never calls the model again.
  - `analyst`: precise, with the key numbers from the evidence.
  - `casual`: plain words, no jargon, at most one or two numbers (a prompt rule; not counted by code).
  - `player`: centred on the player in the moment, with their full name in the headline (or on the team when there is no player).
- **Checks, one version at a time:** each version must have non-empty fields and a headline of at most 70 characters. Every number in it must appear in the moment facts or the narrative's evidence; the narrative's text isn't a source, and the match clock counts. The player headline must contain the player's name. Position codes are spelled out afterwards, as in the Narrative Agent.
- **Prompt-only rules:** the word limits, how many numbers the casual version uses, plain wording, normal punctuation and four content rules are not checked by code. The content rules are: describe only what the facts show; make no claims about fatigue, confidence, intent, form or effort; never say one event caused another unless the facts say so; and never describe a shot speed (the ball's speed) or shot distance (from goal) as a player running.
- **Failure recovery:** it reuses the Narrative Agent's client setup, endpoint handling, 30-second timeout and single retry.
  - **One version fails:** only that version is replaced by its template, and the other two keep the model text. Each version is wholly model-written or wholly template, never mixed. Retrying for one version would cost a whole new call.
  - **Whole reply unusable:** if the reply isn't JSON with all three versions, every version fails, or the call times out or errors, the call is retried once. After that, all three versions come from templates.
  - **Fallback flag:** each version carries its own `fallback` flag, and the agent never raises.
- **Templates:** they're built from the moment facts alone, never from the narrative text, which may be model-written.
  - **Casual:** each moment type names its key fact in plain words. A goal names the scorer and the score ("Rafe Ironsby scores, Calderbrook lead 1-0"), and a speed milestone gives the speed ("fires a shot at 129.6 km/h"). There is no content-free filler.
  - **Analyst and player:** these use the Narrative Agent's facts-only template text. The analyst detail lists the key numbers in plain words, with the score (both sides) and the player's goal tally first. Fact keys the template has no wording for are left out rather than printed raw.
- **Viewer selection (plain Python, no model):** `select_overlay(moment, versions, profile)` picks the profile's persona and returns an overlay that follows the Overlay contract. It is checked by `validate_overlay` in [`deepkick/contracts.py`](deepkick/contracts.py), which checks fields, types, `persona` and `lang`.
  - **Priority:** 1–5 by moment type (goal 5, full-time 4, big chance, momentum swing, pressure spell and half-time 3, high press win and speed, sprint or pass milestones 2, distance milestones 1). It goes up by 1 when the moment involves the viewer's favourite club or player, capped at 5; a half-time or full-time moment involves both clubs.
  - **Other club:** a moment of priority 2 or lower for the viewer's other club is shown as priority 1. Goals keep priority 5 for every profile and are never dropped.
  - **Timing:** `show_at_ms` comes from the match clock, and `duration_ms` is 8000 for goals and 6000 for everything else.
  - **Language:** `lang` stays `en` until the translation stage.
- **Logging:** each handoff logs `stage=persona event_id=… duration_ms=… fallback=yes|no sources=analyst:model|template,casual:…,player:… cached=yes|no`. Each templated version (`persona=… fallback=yes: <reason>`) and each failed attempt is logged with its reason.

```bash
# Print the first 8 overlays for an analyst who follows ASH and player ASH08
.venv/bin/python -m deepkick.agents.persona tests/fixtures/replay_ASH-CAL-42.jsonl --persona analyst --club ASH --player ASH08 --limit 8
```

**Azure service:** the same Microsoft Foundry model deployment as the Narrative Agent (`FOUNDRY_*` in `.env`). The CLI calls the model twice per new moment: once for the narrative and once for the three persona versions. Without `FOUNDRY_*`, both stages use their templates.

**Tests:** [`tests/test_persona.py`](tests/test_persona.py) uses a fake model client and the seed-42 replay. It covers three personas from one cached call, the favourite club and player boost and its cap, goals never dropped for any profile, timeout fallback, invented numbers and broken versions being rejected, one failed version templated alone, a templated version never mixing in model text, every casual template naming its key fact without filler, a retry when every version fails or the reply is broken, plain-word key facts with the full score, the prompt rules, templates passing the same checks for every moment, overlay timing, and the CLI. [`tests/test_contracts.py`](tests/test_contracts.py) covers valid and invalid overlays.
