import { useState, useSyncExternalStore } from "react";
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
import Ask from "./Ask.jsx";

const WIDE = "(min-width: 1024px)"; // the feed gets its own column; below this it lives in the Moments tab
const wide = () => matchMedia(WIDE).matches;
const onWideChange = (notify) => {
  const query = matchMedia(WIDE);
  query.addEventListener("change", notify);
  return () => query.removeEventListener("change", notify);
};

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
  const isWide = useSyncExternalStore(onWideChange, wide);
  // Off by default: browsers block audio until the viewer clicks, and this click is that permission.
  const [commentary, setCommentary] = useState(false);
  useCommentary(overlays, state?.match_clock, commentary);
  const [selected, setSelected] = useState(null); // a moment picked on the timeline: { key, coordinates, team }
  const [askAbout, setAskAbout] = useState(null); // the overlay an "Ask about this" button picked
  const feed = (
    <Feed overlays={overlays} matchClock={state?.match_clock} lang={profile.lang} selected={selected?.key}
          onAsk={setAskAbout} />
  );
  const ask = <Ask profile={profile} about={askAbout} onClearAbout={() => setAskAbout(null)} />;

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="wordmark">Deep<span>kick</span></span>
          <span className="tagline">Every moment explained, in your words.</span>
        </div>
        <Panel profile={profile} onChange={setProfile} />
        <CatchUp profile={profile} />
        <button className="toggle commentary" aria-pressed={commentary} onClick={() => setCommentary(!commentary)}>
          Commentary {commentary ? "on" : "off"}
        </button>
        <ThemeToggle />
      </header>
      <Scoreboard state={state} status={status} />
      <Timeline state={state} profile={profile} overlays={overlays} selected={selected} onSelect={setSelected} />
      <main className="layout">
        <div className="main-col">
          <section className="pitch-card" aria-label="Pitch">
            <Pitch state={state} highlight={selected} />
            <OverlayStack overlays={overlays} matchClock={state?.match_clock} lang={profile.lang} />
          </section>
          <Tabs state={state} profile={profile} overlays={overlays} feed={isWide ? null : <>{feed}{ask}</>}
                selected={selected} />
        </div>
        {isWide && (
          <aside className="feed-col" aria-label="Live feed">
            <div className="feed-stack">
              <div className="feed-scroll">
                <h2 className="section-title">Live feed</h2>
                {feed}
              </div>
              {ask}
            </div>
          </aside>
        )}
      </main>
    </div>
  );
}
