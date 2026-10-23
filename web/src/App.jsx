import { useState, useSyncExternalStore } from "react";
import { momentKey, useMatchState, useOverlaySocket } from "./live.js";
import { THEMES, useTheme } from "./theme.js";
import Scoreboard from "./Scoreboard.jsx";
import Pitch from "./Pitch.jsx";
import OverlayStack from "./OverlayStack.jsx";
import Feed from "./Feed.jsx";
import Panel from "./Panel.jsx";
import Tabs from "./Tabs.jsx";

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
  const [profile, setProfile] = useState({ persona: "casual", club: "", player: "", lang: "en" });
  // Latest overlay per moment: a persona switch replaces the text of what is already on screen.
  const [overlays, setOverlays] = useState(() => new Map());
  const state = useMatchState();
  const status = useOverlaySocket(profile, (overlay) =>
    setOverlays((previous) => new Map(previous).set(momentKey(overlay), overlay)),
  );
  const isWide = useSyncExternalStore(onWideChange, wide);
  const feed = <Feed overlays={overlays} matchClock={state?.match_clock} lang={profile.lang} />;

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="wordmark">Deep<span>kick</span></span>
          <span className="tagline">Every moment explained, in your words.</span>
        </div>
        <Panel profile={profile} onChange={setProfile} />
        <ThemeToggle />
      </header>
      <Scoreboard state={state} status={status} />
      <main className="layout">
        <div className="main-col">
          <section className="pitch-card" aria-label="Pitch">
            <Pitch state={state} />
            <OverlayStack overlays={overlays} matchClock={state?.match_clock} lang={profile.lang} />
          </section>
          <Tabs state={state} profile={profile} overlays={overlays} feed={isWide ? null : feed} />
        </div>
        {isWide && (
          <aside className="feed-col" aria-label="Live feed">
            <div className="feed-scroll">
              <h2 className="section-title">Live feed</h2>
              {feed}
            </div>
          </aside>
        )}
      </main>
    </div>
  );
}
