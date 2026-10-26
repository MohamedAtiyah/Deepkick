import { useEffect, useState, useSyncExternalStore } from "react";
import { momentKey, useMatchState, useOverlaySocket } from "./live.js";
import { THEMES, useTheme } from "./theme.js";
import { useCommentary } from "./commentary.js";
import Scoreboard from "./Scoreboard.jsx";
import Pitch from "./Pitch.jsx";
import OverlayStack from "./OverlayStack.jsx";
import Feed from "./Feed.jsx";
import Panel from "./Panel.jsx";
import Tabs from "./Tabs.jsx";
import CatchUp from "./CatchUp.jsx";
import Timeline from "./Timeline.jsx";
import Chat from "./Chat.jsx";

const WIDE = "(min-width: 1024px)"; // the feed gets its own column; below this it follows the tabs
const DOCKED = "(min-width: 1280px)"; // the chat gets its own column; below this it opens as a drawer or a sheet

/** Whether a media query matches, kept in step with the window. */
function useMedia(query) {
  return useSyncExternalStore((notify) => {
    const list = matchMedia(query);
    list.addEventListener("change", notify);
    return () => list.removeEventListener("change", notify);
  }, () => matchMedia(query).matches);
}

function ThemeToggle() {
  const [theme, setTheme] = useTheme();
  return (
    <div className="segmented" role="radiogroup" aria-label="Theme">
      {THEMES.map((t) => (
        <button key={t} role="radio" aria-checked={theme === t} onClick={() => setTheme(t)}>
          {t[0].toUpperCase() + t.slice(1)}
        </button>
      ))}
    </div>
  );
}

export default function App() {
  const [profile, setProfile] = useState({ persona: "casual", lang: "en" });
  // Latest overlay per moment: a persona switch replaces the text of what is already on screen.
  const [overlays, setOverlays] = useState(() => new Map());
  const state = useMatchState();
  const status = useOverlaySocket(profile, (overlay) =>
    setOverlays((previous) => new Map(previous).set(momentKey(overlay), overlay)),
  );
  const isWide = useMedia(WIDE);
  const docked = useMedia(DOCKED);
  const [chatOpen, setChatOpen] = useState(false);
  useEffect(() => {
    if (!chatOpen) return undefined;
    const close = (event) => event.key === "Escape" && setChatOpen(false);
    addEventListener("keydown", close);
    return () => removeEventListener("keydown", close);
  }, [chatOpen]);
  // Off by default: browsers block audio until the viewer clicks, and this click is that permission.
  const [commentary, setCommentary] = useState(false);
  useCommentary(overlays, state?.match_clock, commentary);
  const [selected, setSelected] = useState(null); // a moment picked on the timeline: { key, coordinates, team }
  const feed = (
    <Feed overlays={overlays} matchClock={state?.match_clock} lang={profile.lang} selected={selected?.key} />
  );

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="wordmark">Deep<span>kick</span></span>
          <span className="tagline">Every moment explained, in your words.</span>
        </div>
        <Panel profile={profile} onChange={setProfile} />
        <button className="toggle ask-open" onClick={() => setChatOpen(true)} aria-expanded={chatOpen}
                aria-controls="chat-dock">Ask the match</button>
        <CatchUp profile={profile} />
        <button className="toggle commentary" aria-pressed={commentary} onClick={() => setCommentary(!commentary)}>
          Commentary {commentary ? "on" : "off"}
        </button>
        <ThemeToggle />
      </header>
      <main className="layout">
        {/* One chat for every width: a column from 1280px, a drawer or a full-screen sheet below (CSS places it). */}
        <aside id="chat-dock" className={`chat-dock${chatOpen ? " open" : ""}`} aria-label="Ask the match"
               inert={!docked && !chatOpen}>
          <Chat profile={profile} clock={state?.match_clock} docked={docked} open={chatOpen}
                onClose={() => setChatOpen(false)} />
        </aside>
        {!docked && chatOpen && <div className="chat-backdrop" onClick={() => setChatOpen(false)} />}
        <div className="main-col">
          <Scoreboard state={state} status={status} />
          <Timeline state={state} profile={profile} overlays={overlays} selected={selected} onSelect={setSelected} />
          <section className="pitch-card" aria-label="Pitch">
            <Pitch state={state} highlight={selected} />
            <OverlayStack overlays={overlays} matchClock={state?.match_clock} lang={profile.lang} />
          </section>
          <Tabs state={state} profile={profile} />
          {!isWide && (
            <section className="feed-section" aria-label="Moments">
              <h2 className="section-title">Moments</h2>
              {feed}
            </section>
          )}
        </div>
        {isWide && (
          <aside className="feed-col" aria-label="Moments">
            <div className="feed-stack">
              <div className="feed-scroll">
                <h2 className="section-title">Moments</h2>
                {feed}
              </div>
            </div>
          </aside>
        )}
      </main>
      {!docked && !chatOpen && (
        <button className="ask-fab" onClick={() => setChatOpen(true)} aria-controls="chat-dock">Ask</button>
      )}
    </div>
  );
}
