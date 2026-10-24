"""Overlay API: the orchestrator's shared match state over FastAPI: REST endpoints plus the /ws overlay socket."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, AsyncIterator, Literal

from fastapi import FastAPI, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StringConstraints

from deepkick.agents.chat import ChatAgent
from deepkick.agents.persona import select_overlay
from deepkick.agents.speech import Speech, spoken_text
from deepkick.contracts import LANGS, PERSONAS
from deepkick.orchestrator import Orchestrator, live_events, replay_events
from deepkick.ratelimit import RateLimiter

log = logging.getLogger("deepkick.api")


def _log_failure(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception():
        log.error("stage=api orchestrator stopped: %r", task.exception())


HISTORY = 6  # messages of the conversation the server keeps


class ChatMessage(BaseModel):
    """One message of the conversation the client sends: the viewer's or a previous answer, at most 300 characters."""

    role: Literal["user", "assistant"]
    content: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]


class ChatRequest(BaseModel):
    """A chat turn: the conversation so far (the last message is the viewer's question), persona and language."""

    messages: Annotated[list[ChatMessage], Field(min_length=1)]
    persona: Literal["analyst", "casual", "player"] = "casual"
    lang: Literal["en", "ar"] = "en"


def create_app(orchestrator: Orchestrator | None = None, events: AsyncIterator[dict[str, Any]] | None = None,
               speech: Speech | None = None, chat: ChatAgent | None = None,
               limiter: RateLimiter | None = None) -> FastAPI:
    """Build the API. With no arguments, startup runs an orchestrator on REPLAY_FILE if set, else on Event Hubs."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        from dotenv import load_dotenv

        load_dotenv()  # before the agents are built, so they see FOUNDRY_* and EVENTHUB_*
        replay = os.environ.get("REPLAY_FILE")
        app.state.mode = "injected" if events else "replay" if replay else "live"
        app.state.orchestrator = orchestrator or Orchestrator()
        app.state.speech = speech or Speech.from_env()
        app.state.chat = chat or ChatAgent()
        app.state.limiter = limiter or RateLimiter(per_client=6, total=30)
        app.state.limiter = limiter or RateLimiter()
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

    @app.get("/timeline")
    def timeline(persona: Literal["analyst", "casual", "player"] = "casual", lang: Literal["en", "ar"] = "en",
                 club: str | None = None, player: str | None = None) -> dict[str, Any]:
        """Per-minute momentum (home minus away) and the released moments, up to the current clock only."""
        return app.state.orchestrator.timeline({"persona": persona, "lang": lang, "favourite_club": club,
                                                "favourite_player": player})

    @app.get("/recap")
    def recap(persona: Literal["analyst", "casual", "player"] | None = None,
              lang: Literal["en", "ar"] = "en") -> list[dict[str, Any]]:
        """The half-time and full-time recaps written so far, newest first, for one persona and language."""
        orchestrator = app.state.orchestrator
        return orchestrator.recaps(persona or orchestrator.profile["persona"], lang)

    @app.get("/catchup")
    def catchup(persona: Literal["analyst", "casual", "player"] = "casual", lang: Literal["en", "ar"] = "en",
                minutes: int = Query(10, ge=1, le=120)) -> dict[str, Any]:
        """The score change, moment headlines and momentum of the last `minutes` of match clock, up to now only."""
        return app.state.orchestrator.catch_up(persona, lang, minutes)

    @app.get("/speech/{overlay_id}", responses={200: {"content": {"audio/mpeg": {}}}})
    def speech_clip(overlay_id: str, persona: Literal["analyst", "casual", "player"],
                    lang: Literal["en", "ar"]) -> Response:
        """The overlay's headline and detail spoken as MP3: 404 for an unknown overlay, 503 if synthesis failed."""
        overlay = app.state.orchestrator.released_overlay(overlay_id, persona, lang)
        if overlay is None:
            raise HTTPException(404, f"no released overlay {overlay_id!r}")
        voice = app.state.speech
        audio = voice and voice.synthesize(spoken_text(overlay["payload"]), persona, lang, overlay_id=overlay_id,
                                           event_id=overlay_id.rsplit("-", 3)[0])  # id: <event>-<type>-<persona>-<lang>
        if audio is None:
            raise HTTPException(503, "commentary is unavailable for this overlay")  # the text overlay still ships
        return Response(audio, media_type="audio/mpeg")

    @app.post("/chat")
    def chat_turn(body: ChatRequest, request: Request) -> Any:
        """Answer the last message from the match tools only (up to the live clock); 429 when over the rate limit."""
        if body.messages[-1].role != "user":
            raise HTTPException(422, "the last message must be the viewer's question")
        # shortcut: X-Forwarded-For can be spoofed to dodge the per-client limit; the global limit still caps the
        # model calls. Behind Azure Container Apps, trust only the ingress's own hop once it is deployed.
        client = (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
                  or (request.client.host if request.client else "unknown"))
        if not app.state.limiter.allow(client):
            return JSONResponse({"detail": "Lots of questions right now. Give it a minute and ask again."}, 429)
        orchestrator = app.state.orchestrator
        history = [m.model_dump() for m in body.messages[-HISTORY:]]
        return app.state.chat.reply(history, body.persona, body.lang, orchestrator.match_data(),
                                    orchestrator.translator)

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
