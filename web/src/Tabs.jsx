import { useEffect, useRef, useState } from "react";
import Feed from "./Feed.jsx";
import Stats from "./Stats.jsx";
import Lineups from "./Lineups.jsx";
import Recap from "./Recap.jsx";

const TABS = [["moments", "Moments"], ["stats", "Stats"], ["lineups", "Lineups"], ["recap", "Recap"]];

/** Moments, Stats, Lineups and Recap under the pitch. `feed` is the full feed when it has no column of its own. */
export default function Tabs({ state, profile, overlays, feed, selected }) {
  const [active, setActive] = useState("moments");
  const hasFeed = Boolean(feed);
  useEffect(() => {
    if (selected && hasFeed) setActive("moments"); // a moment picked on the timeline: show its card
  }, [selected, hasFeed]);
  const buttons = useRef({});

  // Arrow keys, Home and End move between tabs, as in the WAI-ARIA tabs pattern.
  const onKeyDown = (event) => {
    const i = TABS.findIndex(([id]) => id === active);
    const next = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: TABS.length - 1 }[event.key];
    if (next === undefined) return;
    event.preventDefault();
    const [id] = TABS[(next + TABS.length) % TABS.length];
    setActive(id);
    buttons.current[id].focus();
  };

  return (
    <section className="tabs">
      <div className="tablist" role="tablist" aria-label="Match centre" onKeyDown={onKeyDown}>
        {TABS.map(([id, label]) => (
          <button key={id} ref={(el) => (buttons.current[id] = el)} role="tab" id={`tab-${id}`}
                  aria-selected={active === id} aria-controls={`panel-${id}`} tabIndex={active === id ? 0 : -1}
                  onClick={() => setActive(id)}>
            {label}
          </button>
        ))}
      </div>
      <div className="tabpanel" role="tabpanel" id={`panel-${active}`} aria-labelledby={`tab-${active}`} tabIndex={0}>
        {active === "moments" &&
          (feed ?? <Feed overlays={overlays} matchClock={state?.match_clock} lang={profile.lang} compact
                         selected={selected?.key} />)}
        {active === "stats" && <Stats state={state} />}
        {active === "lineups" && <Lineups state={state} />}
        {active === "recap" && <Recap persona={profile.persona} lang={profile.lang} />}
      </div>
    </section>
  );
}
