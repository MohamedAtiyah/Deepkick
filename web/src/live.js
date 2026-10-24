// Hooks for the backend: /state is polled, /ws is a socket that reconnects on its own.
import { useEffect, useRef, useState } from "react";

export function clockMs(matchClock) {
  const [minutes, seconds] = (matchClock || "0:0").split(":").map(Number);
  return (minutes * 60 + seconds) * 1000;
}

// overlay_id is "<moment_id>-<persona>-<lang>"; the moment is what stays the same across personas.
export const momentKey = (overlay) => overlay.overlay_id.split("-").slice(0, -2).join("-");

export function useMatchState(everyMs = 1000) {
  const [state, setState] = useState(null);
  useEffect(() => {
    let alive = true;
    const poll = () =>
      fetch("/state")
        .then((r) => (r.ok ? r.json() : null))
        .then((s) => alive && s && setState(s))
        .catch(() => {});
    poll();
    const id = setInterval(poll, everyMs);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [everyMs]);
  return state;
}

export function useOverlaySocket(profile, onOverlay) {
  const [status, setStatus] = useState("connecting");
  const handler = useRef(onOverlay);
  handler.current = onOverlay;

  useEffect(() => {
    const params = new URLSearchParams({ persona: profile.persona, lang: profile.lang });
    if (profile.club) params.set("club", profile.club);
    if (profile.player) params.set("player", profile.player);
    const url = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws?${params}`;
    let socket, retry, attempt = 0, stopped = false;

    const connect = () => {
      setStatus(attempt ? "reconnecting" : "connecting");
      socket = new WebSocket(url);
      socket.onopen = () => {
        attempt = 0;
        setStatus("live");
      };
      socket.onmessage = (event) => handler.current(JSON.parse(event.data));
      socket.onclose = () => {
        if (stopped) return;
        attempt += 1;
        setStatus("reconnecting");
        retry = setTimeout(connect, Math.min(8000, 500 * 2 ** attempt));
      };
    };
    connect();
    return () => {
      stopped = true;
      clearTimeout(retry);
      socket.close();
    };
  }, [profile.persona, profile.club, profile.player, profile.lang]);

  return status;
}

const STOPPED = new Set(["not_started", "half_time", "full_time"]);
const pad = (n) => String(n).padStart(2, "0");

// The match clock between /state polls: ticks at the rate the last two polls showed, so a 30x replay counts up
// smoothly instead of jumping 30 seconds a poll. It stops at half-time and full-time and never runs backwards.
export function useSmoothClock(state) {
  const sample = useRef(null); // { ms, at, rate } with rate in match ms per real ms
  const shown = useRef(0);
  const [, setTick] = useState(0);

  useEffect(() => {
    if (!state?.match_clock) return;
    const ms = clockMs(state.match_clock);
    const at = performance.now();
    const previous = sample.current;
    let rate = previous && ms > previous.ms ? (ms - previous.ms) / (at - previous.at) : previous?.rate ?? 0;
    if (STOPPED.has(state.status)) rate = 0;
    if (ms < shown.current - 60_000) shown.current = 0; // a new match started
    sample.current = { ms, at, rate };
  }, [state]);

  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), 100);
    return () => clearInterval(id);
  }, []);

  const s = sample.current;
  if (!s) return state?.match_clock ?? "00:00";
  // Run on for at most 1.5 s past a poll, so a stalled stream doesn't race ahead.
  const estimate = s.ms + s.rate * Math.min(performance.now() - s.at, 1500);
  shown.current = Math.max(shown.current, Math.min(estimate, s.ms + 60_000));
  const seconds = Math.floor(shown.current / 1000);
  return `${pad(Math.floor(seconds / 60))}:${pad(seconds % 60)}`;
}

// In right-to-left text a score like "1-0" after an Arabic word displays reversed ("0-1"): the hyphen does not join
// Arabic-context numbers into one run the way "." does. Split scores out so the view can isolate them LTR.
const SCORE = /(\d+(?:\.\d+)?(?:\s?[-–]\s?\d+(?:\.\d+)?)+)/;

/** The text as [part, isScore] pairs, scores (hyphenated number groups) marked so they can render left-to-right. */
export function splitScores(text) {
  return text.split(SCORE).filter(Boolean).map((part) => [part, SCORE.test(part) && /^\d/.test(part)]);
}

// At replay speed each lower-third holds the screen for many match minutes, so the queue falls behind the clock.
const STALE_MS = 60_000; // waited over a match minute: the feed already has it, so skip it on the pitch
const ALWAYS_SHOWN = 5; // goals: shown late rather than never

/** Split the lower-third queue at match time `nowMs` into what to show now (up to `free`, highest priority first),
 *  what has waited too long to show at all, and what keeps waiting. `arrivedMs` maps a key to the match time it
 *  reached the browser: an overlay that arrives late (the pipeline takes real seconds) still gets its turn. */
export function dueOverlays(queue, overlays, nowMs, free, arrivedMs = new Map()) {
  const stale = queue.filter((key) => {
    const o = overlays.get(key);
    return Math.max(o.show_at_ms, arrivedMs.get(key) ?? 0) < nowMs - STALE_MS && o.priority < ALWAYS_SHOWN;
  });
  const show = queue.filter((key) => !stale.includes(key) && overlays.get(key).show_at_ms <= nowMs)
    .sort((a, b) => overlays.get(b).priority - overlays.get(a).priority).slice(0, free);
  return { show, stale, keep: queue.filter((key) => !stale.includes(key) && !show.includes(key)) };
}
