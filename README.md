# Deepkick

**AI match intelligence for the Microsoft × Premier League "Inside the Game" hackathon.** Deepkick turns a stream of football events into explained, timed, personalised broadcast overlays, recaps and a match chat, with every number traced back to the data.

| Desktop, light | Desktop, dark | Phone |
|---|---|---|
| ![Match centre, light theme](docs/screenshots/desktop-light.png) | ![Match centre, dark theme](docs/screenshots/desktop-dark.png) | ![Match centre on a phone](docs/screenshots/mobile-dark.png) |

![The pitch at 5x replay: passes, interceptions and runs played out from the events](docs/pitch-motion.gif)

All clubs, players, stadiums and referees are fictional ([`data/league.json`](data/league.json)). The full design notes for every stage are in [`docs/REFERENCE.md`](docs/REFERENCE.md).

## What it does

- **Explained moments:** goals, big chances, pressure spells, momentum swings, sprints and milestones are detected from the stats, then a Foundry model says *why* each one matters.
- **Three personas:** every moment is written for an Analyst, a Casual fan and a Player-focused viewer. Switching persona changes the text already on screen.
- **Timed overlays:** a lower-third on an animated pitch, plus a feed of moment cards, each with a **How we know** list of the exact facts it cites.
- **Recaps and catch-up:** half-time and full-time recaps, and a spoiler-safe "Catch me up" for the last 10 minutes.
- **Ask the match:** a chat answered only from match tools. It declines rather than guesses.
- **Spoken commentary:** optional Azure AI Speech voice per persona.
- **Never blank:** every model call has a timeout and a template fallback, so an overlay always ships.

## Architecture

```mermaid
flowchart LR
  SIM["Simulator<br/>seeded match"] -->|events| EH[("Azure Event Hubs")]
  SIM -.->|replay file| ORC
  EH --> ORC
  subgraph ORC["Orchestrator (Microsoft Agent Framework)"]
    direction LR
    ST["Stats Agent"] --> MD["Moment detector"]
    MD --> NA["Narrative Agent"]
    NA --> PA["Persona Agent"]
    MD --> RA["Recap Agent"]
  end
  NA <-->|gpt-5-mini| FD[("Microsoft Foundry")]
  PA <--> FD
  RA <--> FD
  ORC --> API["FastAPI<br/>/ws /state /events /recap<br/>/catchup /timeline /chat /speech"]
  CH["Chat agent + match tools"] <--> FD
  API --> CH
  API --> SP[("Azure AI Speech")]
  API --> WEB["React broadcast view"]
```

One orchestrator reads the stream (Event Hubs Basic has one consumer group) and fans out to every viewer. Stats, moments, priorities and overlay timing are plain Python; only the text comes from the model.

## How each part works

### 1. Ingest: simulator

```mermaid
flowchart LR
  L["league.json"] --> S["simulate_match(seed)"] --> E["~1,500 events<br/>pass, shot, tackle, run…"]
  E --> F["replay file (.jsonl)"]
  E --> H["Event Hubs (--send)"]
```

Same seed, same match: seed 42 is **Ashvale 3-5 Calderbrook**, 1,497 events. The player on the ball is chosen near the ball from the team shape, so the pitch never has to teleport anyone. [`deepkick/simulator.py`](deepkick/simulator.py)

```bash
.venv/bin/python -m deepkick.simulator --seed 42 --out replays/ASH-CAL-42.jsonl
```

### 2. Interpret: Stats Agent and moment detector

```mermaid
flowchart LR
  E["event"] --> S["Stats Agent<br/>score, xG, possession,<br/>momentum, speeds"] --> D["Moment detector<br/>rules + cooldowns"] --> M["moment<br/>type, priority 1-5, facts"]
```

No model calls. Seed 42 yields 44 moments (8 goals, 12 sprint milestones, 6 momentum swings…). [`deepkick/stats.py`](deepkick/stats.py), [`deepkick/moments.py`](deepkick/moments.py)

### 3. Explain and personalise: Narrative and Persona Agents

```mermaid
sequenceDiagram
  participant O as Orchestrator worker
  participant N as Narrative Agent
  participant P as Persona Agent
  participant F as Foundry (gpt-5-mini)
  O->>N: moment + stats snapshot
  N->>F: facts only (30 s timeout)
  F-->>N: why it matters + evidence keys
  N->>N: check every number is in the facts
  alt invalid, timeout or error
    N->>N: one retry, then the stats template
  end
  N->>P: narrative
  P->>F: one call for all three personas
  F-->>P: analyst, casual, player
  P->>P: same checks per version, template for any that fails
  P-->>O: three versions, each with evidence and source
```

Only priority 2+ moments call the model (22 on seed 42); the rest use templates. A 429 waits 2 s, 4 s, 8 s (or Retry-After) without using up an attempt. [`deepkick/agents/narrative.py`](deepkick/agents/narrative.py), [`deepkick/agents/persona.py`](deepkick/agents/persona.py)

### 4. Render: orchestrator and overlay API

```mermaid
flowchart LR
  M["moments"] --> C["cap: 3 per match minute<br/>(goals never dropped)"] --> Q["queue"] --> W["3 workers<br/>Explain → Personalise"]
  W --> R["release in clock order"] --> WS["/ws per viewer<br/>(own persona)"]
```

Model calls run concurrently, but overlays are always released in match-clock order. Each viewer gets the backlog first, then live overlays in their own persona. Every handoff is logged (`stage, event_id, duration_ms, fallback`). [`deepkick/orchestrator.py`](deepkick/orchestrator.py), [`deepkick/api.py`](deepkick/api.py)

### 5. Broadcast view

```mermaid
flowchart LR
  WS["/ws overlays"] --> LT["lower-third<br/>one at a time"]
  WS --> FEED["Moments feed<br/>+ How we know"]
  EV["/events every 400 ms"] --> MO["motion engine<br/>passes, interceptions, runs, shots"] --> PITCH["animated pitch"]
  ST["/state"] --> SB["score strip, Stats tab"]
  TL["/timeline"] --> MC["momentum chart"]
```

React + Vite, light and dark themes, keyboard accessible, reduced-motion aware. Layout from 1280 px: **Ask the match | the match | Moments**. Players move at a sprinter's capped speed, the ball travels between them, and a queue catches up when the replay runs fast. [`web/src`](web/src)

### 6. Recap and catch-up

```mermaid
flowchart LR
  HT["half_time / full_time moment"] --> RA["Recap Agent<br/>goals list + team stats"] --> F[("Foundry")] --> CK["checks: 3-6 sentences,<br/>numbers grounded,<br/>every goal named"] --> R["/recap"]
  T["timeline up to now"] --> CU["/catchup<br/>last 10 min, no model"]
```

A recap that leaves out a goal falls back to its template. Catch-up never shows anything past the live clock. [`deepkick/agents/recap.py`](deepkick/agents/recap.py)

### 7. Ask the match

```mermaid
sequenceDiagram
  participant V as Viewer
  participant C as Chat agent
  participant F as Foundry
  participant T as Match tools
  V->>C: question (rate limited)
  loop up to 3 tool rounds, 45 s in total
    C->>F: conversation + tool list
    F-->>C: tool calls (player_stats, top_sprints, goals…)
    C->>T: run them
    T-->>C: results up to the live clock only
  end
  F-->>C: answer
  C->>C: every number must appear in the tool results
  C-->>V: answer + stat cards, or a polite decline
```

The tools read only events already played, so answers are spoiler-safe. Cards are built in Python from tool results, never by the model. [`deepkick/agents/chat.py`](deepkick/agents/chat.py), [`deepkick/agents/match_tools.py`](deepkick/agents/match_tools.py)

### 8. Spoken commentary

```mermaid
flowchart LR
  O["overlay"] --> S["/speech/{overlay_id}"] --> AZ[("Azure AI Speech<br/>en-GB voice per persona")] --> MP3["MP3, cached"]
```

Off by default; a 10 s timeout and one retry, then silence (the text overlay still shows). [`deepkick/agents/speech.py`](deepkick/agents/speech.py)

## Reliability rules

| Situation | What happens |
|---|---|
| Model timeout or error | One retry, then a template built from the stats; logged |
| Model invents a number | Rejected, retried once, then the template |
| 429 rate limit | Wait 2 s, 4 s, 8 s (or Retry-After, max 30 s); not counted as a failure |
| Speech fails | No audio; the overlay still shows |
| Chat can't ground an answer | Declines and shows the tool results as cards |
| Too many chat requests | 429 after 6 per minute per client or 30 per minute in total |

## Run it locally

```bash
python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env    # optional: FOUNDRY_*, AZURE_SPEECH_*, EVENTHUB_*; without them everything uses templates
(cd web && npm install)
```

```bash
# Terminal 1: the API, replaying seed 42 at 30x (90 minutes in 3)
REPLAY_FILE=tests/fixtures/replay_ASH-CAL-42.jsonl REPLAY_SPEED=30 .venv/bin/uvicorn deepkick.api:app --port 8000
```

```bash
# Terminal 2: the broadcast view on http://localhost:5173
cd web && npm run dev
```

## Deploy a live demo to Azure

One Azure Container App runs everything: the [`Dockerfile`](Dockerfile) builds the site and serves it from the API, so the page, the REST endpoints and the WebSocket share one URL. Azure builds the image, so Docker isn't needed locally.

1. **Install the Azure CLI and sign in** (once):

   ```bash
   brew install azure-cli
   ```

   ```bash
   az login && az extension add --name containerapp --upgrade && az provider register -n Microsoft.App && az provider register -n Microsoft.OperationalInsights
   ```

2. **Build and deploy** from the repo root (pick a region near you, for example `uaenorth`):

   ```bash
   az containerapp up --name deepkick --resource-group deepkick-rg --location uaenorth --source . --ingress external --target-port 8000
   ```

3. **Add the keys as secrets.** This reads them from your `.env` into the shell, so they are never typed or committed:

   ```bash
   set -a; source .env; set +a; az containerapp secret set -n deepkick -g deepkick-rg --secrets foundry-key="$FOUNDRY_API_KEY" speech-key="$AZURE_SPEECH_KEY"
   ```

   ```bash
   az containerapp update -n deepkick -g deepkick-rg --min-replicas 1 --max-replicas 1 --set-env-vars FOUNDRY_ENDPOINT="$FOUNDRY_ENDPOINT" FOUNDRY_MODEL_DEPLOYMENT="$FOUNDRY_MODEL_DEPLOYMENT" FOUNDRY_API_KEY=secretref:foundry-key AZURE_SPEECH_REGION="$AZURE_SPEECH_REGION" AZURE_SPEECH_KEY=secretref:speech-key
   ```

   Keep exactly one replica: the match state lives in memory, and one orchestrator serves every viewer.

4. **Get the URL** and put it at the top of this README and in the GitHub repo's *About → Website*:

   ```bash
   az containerapp show -n deepkick -g deepkick-rg --query properties.configuration.ingress.fqdn -o tsv
   ```

5. **Replay the match again** for a new visitor or for judging (the replay plays once, 3 minutes at 30x; set `REPLAY_SPEED=10` for 9 minutes):

   ```bash
   az containerapp revision restart -n deepkick -g deepkick-rg --revision $(az containerapp show -n deepkick -g deepkick-rg --query properties.latestRevisionName -o tsv)
   ```

6. **Stop paying** after judging:

   ```bash
   az group delete -n deepkick-rg --yes
   ```

The public chat spends Foundry tokens, so it is rate limited (6 questions per minute per visitor, 30 in total).

## Tests

```bash
.venv/bin/python -m pytest -q                 # 339 tests, ~30 s, no network: Azure settings are blanked for the session
cd web && npm test && npm run build           # 23 tests: pitch layout, motion, overlay timing, theme
```

Everything is tested against a fixed-seed replay ([`tests/fixtures/replay_ASH-CAL-42.jsonl`](tests/fixtures/replay_ASH-CAL-42.jsonl)) with fake model clients. GitHub Actions runs the Python suite on every push. [`scripts/chat_eval.py`](scripts/chat_eval.py) runs 12 fixed questions against the real deployment: last run 11 of 12 grounded, median 5.9 s (the declined one was a prompt injection).

## Project layout

```
data/league.json          fictional clubs and squads
deepkick/simulator.py     match simulator and Event Hubs producer
deepkick/stats.py         Stats Agent
deepkick/moments.py       moment detector
deepkick/agents/          narrative, persona, recap, chat (+ match tools), speech
deepkick/orchestrator.py  Agent Framework workflow
deepkick/api.py           FastAPI: overlays, state, events, recap, catch-up, timeline, chat, speech, the built site
web/                      React broadcast view
tests/                    pytest suite and the seed-42 replay
docs/REFERENCE.md         full design notes for every stage
```

## Azure services

| Service | Used for |
|---|---|
| Microsoft Foundry (gpt-5-mini) | narratives, personas, recaps, match chat |
| Microsoft Agent Framework | the Explain → Personalise workflow per moment |
| Azure Event Hubs | live event stream (replay files work without it) |
| Azure AI Speech | spoken commentary |
| Azure Container Apps | the live demo |
