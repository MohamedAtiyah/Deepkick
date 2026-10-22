"""Orchestrator: one consumer reads the event stream, interprets it in line, and explains and personalises moments
on a Microsoft Agent Framework workflow in a worker pool, releasing overlays in match-clock order."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from collections import Counter, deque
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from agent_framework import Executor, WorkflowBuilder, WorkflowContext, handler

from deepkick.agents.narrative import NarrativeAgent, rate_limit_count, template_narrative
from deepkick.agents.persona import PersonaAgent, select_overlay, template_versions
from deepkick.contracts import PERSONAS
from deepkick.moments import MomentDetector
from deepkick.simulator import clock_seconds
from deepkick.stats import StatsAgent

log = logging.getLogger("deepkick.orchestrator")

WORKERS = 3  # 50,000 TPM fits about 25 calls a minute at ~2,000 tokens each
MODEL_MIN_PRIORITY = 2  # lower-priority moments (milestones, high presses) use templates, not the model
CAP_PER_MINUTE = 3  # overlays per minute of match clock
PROTECTED = frozenset({"goal", "half_time", "full_time"})  # never dropped by the cap


def cap_minute(moments: list[dict[str, Any]],
               cap: int = CAP_PER_MINUTE) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split one match minute's moments into (kept, dropped): at most `cap`, lowest priority dropped first.

    Goals, half-time and full-time are always kept, even past the cap. Ties keep the earlier moment.
    """
    others = sorted((m for m in moments if m["type"] not in PROTECTED), key=lambda m: -m["priority"])
    room = max(0, cap - sum(m["type"] in PROTECTED for m in moments))
    keep = {m["moment_id"] for m in moments if m["type"] in PROTECTED} | {m["moment_id"] for m in others[:room]}
    return [m for m in moments if m["moment_id"] in keep], [m for m in moments if m["moment_id"] not in keep]


class ExplainExecutor(Executor):
    """Workflow step 3: the Narrative Agent explains one moment."""

    def __init__(self, narrator: NarrativeAgent) -> None:
        super().__init__("explain")
        self.narrator = narrator

    @handler
    async def explain(self, job: dict, ctx: WorkflowContext[dict]) -> None:
        """Add the narrative to the job; the blocking model call runs in a thread so other moments keep going."""
        if not job["model"]:
            narrative = template_narrative(job["moment"], job["snapshot"])
        else:
            narrative = await asyncio.to_thread(self.narrator.explain, job["moment"], job["snapshot"])
        await ctx.send_message(job | {"narrative": narrative})


class PersonaliseExecutor(Executor):
    """Workflow step 5: the Persona Agent writes the three persona versions of one moment."""

    def __init__(self, personas: PersonaAgent) -> None:
        super().__init__("personalise")
        self.personas = personas

    @handler
    async def personalise(self, job: dict, ctx: WorkflowContext[Any, dict]) -> None:
        """Yield the job with its persona versions as the workflow's output."""
        if not job["model"]:
            versions = template_versions(job["moment"])
        else:
            versions = await asyncio.to_thread(self.personas.versions, job["moment"], job["narrative"])
        await ctx.yield_output(job | {"versions": versions})


class Orchestrator:
    """Runs stages 2-5 over one shared match state: one Stats Agent snapshot plus the overlay history."""

    def __init__(self, narrator: NarrativeAgent | None = None, personas: PersonaAgent | None = None,
                 profile: dict[str, Any] | None = None, workers: int = WORKERS, cap: int = CAP_PER_MINUTE,
                 emit: Callable[[dict[str, Any]], None] = lambda overlay: None) -> None:
        self.stats, self.detector = StatsAgent(), MomentDetector()
        self.narrator, self.personas = narrator or NarrativeAgent(), personas or PersonaAgent()
        self.profile = profile or {"persona": "analyst", "favourite_club": None, "favourite_player": None, "lang": "en"}
        self.workers, self.cap, self.emit = workers, cap, emit
        self.counts: Counter[str] = Counter()
        self.status = "not_started"  # then running, finished or failed
        # Called with (moment, versions) for each released moment, so every viewer can render their own overlay.
        self.listeners: list[Callable[[dict[str, Any], dict[str, Any]], None]] = []
        self._released: list[tuple[dict[str, Any], dict[str, Any]]] = []
        self._start, self._rate_limits_before = time.monotonic(), rate_limit_count()
        self._minute: int | None = None
        self._held: list[tuple[dict[str, Any], dict[str, Any]]] = []  # (moment, snapshot) of the open minute
        self._order: deque[str] = deque()  # moment ids in match-clock order, waiting to be released
        self._done: dict[str, tuple[dict[str, Any], dict[str, Any]] | None] = {}
        self._queue: asyncio.Queue[tuple[dict[str, Any], dict[str, Any]]] | None = None

    def snapshot(self) -> dict[str, Any]:
        """The current match state, as the Stats Agent sees it (for /state)."""
        return self.stats.snapshot()

    def history(self, profile: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Every overlay released so far for a viewer profile (default: the run's), in match-clock order."""
        return [select_overlay(m, v, profile or self.profile) for m, v in self._released]

    def summary(self) -> dict[str, Any]:
        """The run's counters so far (for /healthz and the end-of-run log)."""
        return {"events": self.counts["events"], "moments": self.counts["moments"],
                "emitted": self.counts["emitted"], "dropped": self.counts["dropped"],
                "model_explained": self.counts["model_explained"], "templated": self.counts["templated"],
                "fallbacks": self.counts["fallbacks"], "rate_limits": rate_limit_count() - self._rate_limits_before,
                "seconds": round(time.monotonic() - self._start, 2)}

    def ingest(self, event: dict[str, Any]) -> None:
        """Interpret one event in line (stats, then moments); model work is only queued, never awaited here."""
        self.counts["events"] += 1
        if not self.stats.update(event):
            return
        minute = clock_seconds(event) // 60
        if self._minute is not None and minute != self._minute:
            self.close_minute()
        self._minute = minute
        snapshot = self.stats.snapshot()
        for moment in self.detector.detect(event, snapshot):
            self.counts["moments"] += 1
            self._held.append((moment, snapshot))

    def close_minute(self) -> None:
        """Apply the per-minute cap to the held moments and queue the kept ones for model work."""
        snapshots = {m["moment_id"]: s for m, s in self._held}
        kept, dropped = cap_minute([m for m, _ in self._held], self.cap)
        self._held = []
        for moment in dropped:
            self.counts["dropped"] += 1
            log.info("stage=orchestrator moment_id=%s priority=%d dropped by the cap of %d per minute",
                     moment["moment_id"], moment["priority"], self.cap)
        for moment in kept:
            self._order.append(moment["moment_id"])
            self._queue.put_nowait((moment, snapshots[moment["moment_id"]]))

    async def _worker(self) -> None:
        """Take queued moments and run each through its own Explain -> Personalise workflow."""
        while True:
            moment, snapshot = await self._queue.get()
            start, record, fallback = time.monotonic(), None, True
            model = moment["priority"] >= MODEL_MIN_PRIORITY
            try:
                explain, personalise = ExplainExecutor(self.narrator), PersonaliseExecutor(self.personas)
                workflow = WorkflowBuilder(start_executor=explain).add_edge(explain, personalise).build()
                job = (await workflow.run({"moment": moment, "snapshot": snapshot, "model": model})).get_outputs()[0]
                fallback = model and (job["narrative"]["fallback"]
                                      or any(job["versions"][p]["fallback"] for p in PERSONAS))
                self.counts["model_explained"] += model and not job["narrative"]["fallback"]
                select_overlay(moment, job["versions"], self.profile)  # fail here, not at release, if it is broken
                record = (moment, job["versions"])
            except Exception:  # one broken moment must not stall the release of the others
                log.exception("stage=orchestrator moment_id=%s failed; no overlay", moment["moment_id"])
            self.counts["fallbacks"] += fallback
            self.counts["templated"] += not model
            log.info("stage=orchestrator moment_id=%s duration_ms=%.0f fallback=%s model=%s", moment["moment_id"],
                     (time.monotonic() - start) * 1000, "yes" if fallback else "no", "yes" if model else "no")
            self._done[moment["moment_id"]] = record
            self._release()
            self._queue.task_done()

    def _release(self) -> None:
        """Emit finished moments in match-clock order, holding any that finished before an earlier moment."""
        while self._order and self._order[0] in self._done:
            record = self._done.pop(self._order.popleft())
            if record is not None:
                self._released.append(record)
                self.counts["emitted"] += 1
                self.emit(select_overlay(*record, self.profile))
                for listener in self.listeners:
                    listener(*record)

    async def run(self, events: AsyncIterator[dict[str, Any]]) -> dict[str, Any]:
        """Consume the stream to its end, wait for the model work to finish, log and return the run summary."""
        self._start, self._rate_limits_before, self.status = time.monotonic(), rate_limit_count(), "running"
        self._queue = asyncio.Queue()
        workers = [asyncio.create_task(self._worker()) for _ in range(self.workers)]
        try:
            async for event in events:
                self.ingest(event)
            self.close_minute()
            await self._queue.join()
        except BaseException:
            self.status = "failed"
            raise
        finally:
            for worker in workers:
                worker.cancel()
        self.status = "finished"
        summary = self.summary()
        log.info("stage=orchestrator summary events=%d moments=%d overlays_emitted=%d overlays_dropped=%d "
                 "model_explained=%d templated=%d fallbacks=%d rate_limits=%d wall_s=%.2f", *summary.values())
        return summary


async def replay_events(path: Path, speed: float = 0.0) -> AsyncIterator[dict[str, Any]]:
    """Yield a replay file's events, paced at `speed` x real time (0 = as fast as possible)."""
    previous = None
    with path.open() as f:
        for line in f:
            event = json.loads(line)
            gap = clock_seconds(event) - clock_seconds(previous) if previous and speed > 0 else 0
            await asyncio.sleep(max(0, gap) / speed if speed > 0 else 0)  # sleep(0) still lets the workers run
            previous = event
            yield event


async def live_events() -> AsyncIterator[dict[str, Any]]:
    """Yield events from Azure Event Hubs (the one consumer on the $Default group) until full-time."""
    from azure.eventhub.aio import EventHubConsumerClient

    conn, hub = os.environ.get("EVENTHUB_CONNECTION_STRING"), os.environ.get("EVENTHUB_NAME")
    if not conn or not hub:
        raise RuntimeError("live mode needs EVENTHUB_CONNECTION_STRING and EVENTHUB_NAME in .env")
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def on_event(partition_context: Any, event: Any) -> None:
        try:
            inbox.put_nowait(json.loads(event.body_as_str()))
        except ValueError:
            log.warning("stage=orchestrator skipped an Event Hubs message that is not JSON")

    client = EventHubConsumerClient.from_connection_string(conn, consumer_group="$Default", eventhub_name=hub)
    receiving = asyncio.create_task(client.receive(on_event=on_event, starting_position="@latest"))
    try:
        while True:
            event = await inbox.get()
            yield event
            if event.get("event_type") == "match_metadata" and event.get("payload", {}).get("status") == "full_time":
                return
    finally:
        await client.close()
        receiving.cancel()


def main(argv: list[str] | None = None) -> None:
    """CLI: run the pipeline on a replay file or live from Event Hubs and print overlays as JSON lines."""
    from dotenv import load_dotenv

    parser = argparse.ArgumentParser(description=main.__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--replay", type=Path, help="JSONL file written by `python -m deepkick.simulator --out`")
    source.add_argument("--live", action="store_true", help="read Azure Event Hubs (reads .env)")
    parser.add_argument("--speed", type=float, default=0.0, help="with --replay: multiple of real time (0 = no pacing)")
    parser.add_argument("--persona", choices=PERSONAS, default="analyst")
    parser.add_argument("--workers", type=int, default=WORKERS, help="concurrent model workers")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("deepkick.orchestrator").setLevel(logging.INFO)
    load_dotenv()

    profile = {"persona": args.persona, "favourite_club": None, "favourite_player": None, "lang": "en"}
    orchestrator = Orchestrator(profile=profile, workers=args.workers,
                                emit=lambda overlay: print(json.dumps(overlay), flush=True))
    events = live_events() if args.live else replay_events(args.replay, args.speed)
    try:
        summary = asyncio.run(orchestrator.run(events))
    except RuntimeError as exc:
        parser.error(str(exc))
    print(json.dumps({"summary": summary}), file=sys.stderr)


if __name__ == "__main__":
    main()
