// Spoken commentary: new overlays play through one audio queue, in order, from /speech.
import { useEffect, useRef } from "react";
import { clockMs } from "./live.js";

// Real seconds, counted from when the overlay reached the feed. Match time would not do: at 30x replay every clip
// would be minutes of match time "late" and nothing would ever play.
export const STALE_MS = 15_000;

/** Drop clips queued more than STALE_MS ago; return the first fresh one (or undefined) and the rest of the queue. */
export function nextClip(queue, now, staleMs = STALE_MS) {
  const fresh = queue.filter((clip) => now - clip.queuedAt <= staleMs);
  return { clip: fresh[0], rest: fresh.slice(1) };
}

/** While `enabled`, speak each overlay that newly reaches the feed; stop at once when disabled. */
export function useCommentary(overlays, matchClock, enabled) {
  const latest = useRef(overlays); // the newest text for a moment, so a persona or language switch is respected
  latest.current = overlays;
  const heard = useRef(new Set()); // moments already queued or present when commentary was switched on
  const queue = useRef([]);
  const audio = useRef(null);
  const run = useRef(0); // bumped on every stop, so callbacks from an old clip do nothing

  const due = () => [...latest.current].filter(([, o]) => o.show_at_ms <= clockMs(matchClock)).map(([key]) => key);

  const playNext = () => {
    if (audio.current) return;
    const { clip, rest } = nextClip(queue.current, Date.now());
    queue.current = rest;
    const overlay = clip && latest.current.get(clip.key);
    if (!overlay) return;
    const id = run.current;
    const params = new URLSearchParams({ persona: overlay.persona, lang: overlay.lang });
    const sound = new Audio(`/speech/${encodeURIComponent(overlay.overlay_id)}?${params}`);
    const done = () => {
      if (id !== run.current) return;
      audio.current = null;
      playNext();
    };
    sound.onended = done;
    sound.onerror = done; // a 503 (synthesis failed) stays silent; the text overlay is unaffected
    audio.current = sound;
    sound.play().catch(done);
  };

  // Switching on: only moments that arrive from now on are spoken. Switching off: silence at once.
  useEffect(() => {
    if (enabled) {
      heard.current = new Set(due());
      return;
    }
    run.current += 1;
    audio.current?.pause();
    audio.current = null;
    queue.current = [];
  }, [enabled]);

  useEffect(() => {
    if (!enabled) return;
    for (const key of due()) {
      if (heard.current.has(key)) continue;
      heard.current.add(key);
      queue.current.push({ key, queuedAt: Date.now() });
    }
    queue.current.sort((a, b) => latest.current.get(a.key).show_at_ms - latest.current.get(b.key).show_at_ms);
    playNext();
  }, [overlays, matchClock, enabled]);

  useEffect(() => () => audio.current?.pause(), []); // unmount
}
