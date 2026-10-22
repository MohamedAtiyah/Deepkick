import { useEffect, useRef, useState } from "react";
import { clockMs, splitScores } from "./live.js";

/** Text with every score isolated left-to-right, so "1-0" never displays as "0-1" in Arabic. */
export const Text = ({ children }) =>
  splitScores(children).map(([part, isScore], i) => (isScore ? <bdi key={i} dir="ltr">{part}</bdi> : part));

const SLOTS = 2; // never more than two overlays on screen
const QUEUE = 4; // shortcut: a late viewer's backlog is trimmed to the newest few rather than replayed for minutes
const LABEL = { goal: "Goal", big_chance: "Big chance", half_time: "Half-time", full_time: "Full-time",
  momentum_swing: "Momentum", pressure_spell: "Pressure", high_press_win: "High press", speed_milestone: "Shot speed",
  sprint_milestone: "Sprint", distance_milestone: "Distance", pass_milestone: "Passing" };

export default function OverlayStack({ overlays, matchClock, lang }) {
  const handled = useRef(new Set()); // moments shown or dropped: never queued again, in any persona
  const [view, setView] = useState({ queue: [], onScreen: [] }); // queue: moment keys, oldest first

  // New moments join the queue; a moment already handled (now arriving in another persona) does not.
  useEffect(() => {
    setView((prev) => {
      const fresh = [...overlays.keys()].filter((key) => !handled.current.has(key) && !prev.queue.includes(key));
      if (!fresh.length) return prev;
      const queue = [...prev.queue, ...fresh].sort((a, b) => overlays.get(a).show_at_ms - overlays.get(b).show_at_ms);
      queue.slice(0, -QUEUE).forEach((key) => handled.current.add(key));
      return { ...prev, queue: queue.slice(-QUEUE) };
    });
  }, [overlays]);

  // Every quarter second: retire overlays past their duration, then fill free slots with ones whose moment has come.
  useEffect(() => {
    const tick = setInterval(() => {
      const now = Date.now();
      const nowMs = clockMs(matchClock);
      setView((prev) => {
        const live = prev.onScreen.filter((item) => item.until > now);
        const ready = prev.queue.filter((key) => overlays.get(key).show_at_ms <= nowMs).slice(0, SLOTS - live.length);
        if (!ready.length && live.length === prev.onScreen.length) return prev;
        ready.forEach((key) => handled.current.add(key));
        return {
          queue: prev.queue.filter((key) => !ready.includes(key)),
          onScreen: [...live, ...ready.map((key) => ({ key, until: now + overlays.get(key).duration_ms }))],
        };
      });
    }, 250);
    return () => clearInterval(tick);
  }, [overlays, matchClock]);

  return (
    <div className="overlays" lang={lang} dir={lang === "ar" ? "rtl" : "ltr"} aria-live="polite">
      {view.onScreen.map(({ key }) => {
        const overlay = overlays.get(key);
        return (
          <article key={key} className={`overlay ${overlay.type} p${overlay.priority}`}>
            <header>
              <span className="tag">{LABEL[overlay.type] ?? overlay.type}</span>
              <span className="minute">{overlay.match_clock}</span>
            </header>
            <h2><Text>{overlay.payload.headline}</Text></h2>
            <p><Text>{overlay.payload.detail}</Text></p>
            <p className="why"><Text>{overlay.payload.why}</Text></p>
          </article>
        );
      })}
    </div>
  );
}
