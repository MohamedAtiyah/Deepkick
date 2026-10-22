import { useState } from "react";
import { momentKey, useMatchState, useOverlaySocket } from "./live.js";
import Scoreboard from "./Scoreboard.jsx";
import Pitch from "./Pitch.jsx";
import OverlayStack from "./OverlayStack.jsx";
import Panel from "./Panel.jsx";
import Recap from "./Recap.jsx";

const STATUS_LABEL = { live: "Live", connecting: "Connecting…", reconnecting: "Reconnecting…" };

export default function App() {
  const [profile, setProfile] = useState({ persona: "casual", club: "", player: "", lang: "en" });
  // Latest overlay per moment: a persona switch replaces the text of what is already on screen.
  const [overlays, setOverlays] = useState(() => new Map());
  const state = useMatchState();
  const status = useOverlaySocket(profile, (overlay) =>
    setOverlays((previous) => new Map(previous).set(momentKey(overlay), overlay)),
  );

  return (
    <div className="app">
      <main className="frame">
        <Scoreboard state={state} />
        <Pitch state={state} />
        <OverlayStack overlays={overlays} matchClock={state?.match_clock} lang={profile.lang} />
      </main>
      <aside className="side">
        <div className={`connection ${status}`} role="status">{STATUS_LABEL[status]}</div>
        <Panel profile={profile} onChange={setProfile} />
        <Recap persona={profile.persona} />
      </aside>
    </div>
  );
}
