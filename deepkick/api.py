"""Overlay API: serves the orchestrator's shared match state over FastAPI (/state, /recap, /ws, /healthz)."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Literal

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect

from deepkick.agents.persona import select_overlay
from deepkick.contracts import LANGS, PERSONAS
from deepkick.orchestrator import Orchestrator, live_events, replay_events

log = logging.getLogger("deepkick.api")


def _log_failure(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception():
        log.error("stage=api orchestrator stopped: %r", task.exception())


def create_app(orchestrator: Orchestrator | None = None,
               events: AsyncIterator[dict[str, Any]] | None = None) -> FastAPI:
    """Build the API. With no arguments, startup runs an orchestrator on REPLAY_FILE if set, else on Event Hubs."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        from dotenv import load_dotenv

        load_dotenv()  # before the agents are built, so they see FOUNDRY_* and EVENTHUB_*
        replay = os.environ.get("REPLAY_FILE")
        app.state.mode = "injected" if events else "replay" if replay else "live"
        app.state.orchestrator = orchestrator or Orchestrator()
        stream = events or (replay_events(Path(replay), float(os.environ.get("REPLAY_SPEED", "30"))) if replay
                            else live_events())
        task = asyncio.create_task(app.state.orchestrator.run(stream))
        task.add_done_callback(_log_failure)
        yield
        task.cancel()

    app = FastAPI(title="Deepkick overlay API", lifespan=lifespan)

    @app.get("/state")
    def state() -> dict[str, Any]:
        """The current match state: score, clock, team and player stats."""
        return app.state.orchestrator.snapshot()

    @app.get("/recap")
    def recap(persona: Literal["analyst", "casual", "player"] | None = None, lang: Literal["en", "ar"] = "en",
              limit: int | None = Query(None, ge=1)) -> list[dict[str, Any]]:
        """Overlays released so far, newest first, for one persona (default: the run's) and language."""
        orchestrator = app.state.orchestrator
        profile = orchestrator.profile | {"lang": lang} | ({"persona": persona} if persona else {})
        return orchestrator.history(profile)[::-1][:limit]

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        """Whether the pipeline is running, where each stage gets its input, and the run counters."""
        orchestrator = app.state.orchestrator
        model = {True: "foundry", False: "templates"}
        return {"status": orchestrator.status,
                "stages": {"ingest": app.state.mode, "interpret": "in_process",
                           "explain": model[orchestrator.narrator.client is not None],
                           "personalise": model[orchestrator.personas.client is not None]},
                "summary": orchestrator.summary()}

    @app.websocket("/ws")
    async def overlays(websocket: WebSocket, persona: str = "casual", club: str | None = None,
                       player: str | None = None, lang: str = "en") -> None:
        """Send this viewer's overlays already released, then each new one as it is released."""
        if persona not in PERSONAS or lang not in LANGS:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        profile = {"persona": persona, "favourite_club": club, "favourite_player": player, "lang": lang}
        orchestrator, inbox = app.state.orchestrator, asyncio.Queue()
        def listener(moment: dict[str, Any], versions: dict[str, Any]) -> None:
            inbox.put_nowait((moment, versions))

        backlog = orchestrator.history(profile)
        orchestrator.listeners.append(listener)  # same step as the backlog read, so no overlay falls between
        try:
            for overlay in backlog:
                await websocket.send_json(overlay)
            while True:
                await websocket.send_json(select_overlay(*await inbox.get(), profile))
        except WebSocketDisconnect:
            pass
        finally:
            orchestrator.listeners.remove(listener)

    return app


app = create_app()
