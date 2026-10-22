# Deepkick

AI match intelligence for the Microsoft × Premier League "Inside the Game" hackathon.
Solo build. Submissions close **Tue 27 Oct 2026**. Public repo: https://github.com/MohamedAtiyah/Deepkick

Deepkick turns a stream of synthetic football events into explained, timed, personalised broadcast overlays and match recaps.

## Pipeline

1. **Ingest**: `simulator.py` plays a match and sends events to Azure Event Hubs.
2. **Interpret**: Stats Agent keeps running stats; moment detector flags key moments. Pure Python, no model calls.
3. **Explain**: Narrative Agent (Microsoft Foundry model) says why a flagged moment matters.
4. **Render**: overlay objects streamed over a FastAPI WebSocket, drawn by the React broadcast view.
5. **Personalise**: Persona Agent rewrites for Analyst, Casual fan or Player focus, filtered by favourite club and player; Azure AI Translator for language.

Microsoft Agent Framework orchestrates stages 2–5 over one shared match state.

## Stack

- Python 3.11+, FastAPI, pytest
- Azure Event Hubs (Basic tier: one consumer group, so one orchestrator reads the stream and fans out)
- Microsoft Foundry: gpt-5-mini, Global Standard deployment at 50,000 tokens per minute; tracing and evaluation
- Microsoft Agent Framework
- Azure AI Translator: English, Spanish, Arabic
- React + Vite frontend
- Deploy: Azure Container Apps (backend), Azure Static Web Apps (frontend)

## Hard rules

- **Synthetic data only.** Fictional clubs, players, stadiums and referees from `data/league.json`. Never use real club names, player names, logos or league branding anywhere: code, data, UI or screenshots.
- **Secrets stay in `.env`**, which is git-ignored. Never commit keys or connection strings. Keep `.env.example` with empty values up to date.
- **Every model call has a timeout and a fallback.** On failure or timeout, return a template line built from the stats so an overlay still ships. Log the failure. A 429 (rate limit) is not a failure: it is retried with backoff (2 s, 4 s, 8 s, or the Retry-After header) without using up a normal attempt.
- **Narratives cite only numbers present in the match state** passed to the model. The model never invents stats.
- **The simulator takes a seed.** The same seed must produce the same match, so demos and tests repeat.

## Required for judging (always in scope, not optional extras)

Judges score code quality, documentation, testing and failure recovery, so these count as part of every task:

- A one-line docstring on each module and public function.
- Type hints on public functions.
- pytest coverage for every stats, moment and contract function. Use a fixed-seed replay file as the fixture.
- Log each agent handoff (stage, event id, duration, fallback used or not).
- When a stage is finished, update its section in `README.md`: what it does, how to run it, which Azure service it uses.

## Layout

```
data/league.json          fictional clubs and squads
deepkick/simulator.py     match simulator and Event Hubs producer
deepkick/stats.py         Stats Agent
deepkick/moments.py       moment detector
deepkick/agents/          narrative, persona, recap agents
deepkick/orchestrator.py  Agent Framework workflow
deepkick/api.py           FastAPI: /ws overlays, /state, /recap
web/                      React broadcast view
tests/
```

## Contracts

Event (one JSON object per message):
`event_id, match_id, timestamp, match_clock, half, event_type, team, player{id,name,position}, coordinates{x,y}, payload{...}, context{score, possession_team, pressure_index}`

`event_type` is one of: `match_metadata, pass, shot, tackle, possession_change, pressure_event, player_run`.

Overlay (one JSON object per WebSocket message):
`overlay_id, type, match_clock, show_at_ms, duration_ms, priority, persona, lang, payload{player, headline, detail, why}`

Change a contract only together with its producer, its consumers and its tests.

## Plan

- Day 1 (Tue 20 Oct): league data, simulator, Event Hubs, Stats Agent, moment detector, tests.
- Day 2 (Wed 21 Oct): Foundry project, narrative, persona, recap agents, translation, orchestration with fallbacks, overlay API, tracing.
- Day 3 (Thu 22 Oct): React broadcast view, personalisation panel, Azure deploy, README, demo video, submit.
- Fri 23 – Mon 26 Oct: buffer. If behind, cut Arabic, then half-time recap, then the Foundry eval. Never cut the persona switch or the timed overlays.
