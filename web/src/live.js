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
