import { useEffect, useId, useRef, useState } from "react";
import { clockMs } from "./live.js";
import { LABEL } from "./OverlayStack.jsx";
import league from "../../data/league.json";

// A card's edge takes the club colours of the player it is about (match-wide moments keep the accent).
const CLUB_OF = Object.fromEntries(league.clubs.flatMap((club) =>
  [...club.starters, ...club.bench].map((p) => [p.name, club.colours])));
const edge = (player) => CLUB_OF[player] && { "--club": CLUB_OF[player].primary,
                                               "--club-2": CLUB_OF[player].secondary };

/** A list of cited facts as label: value. */
export const EvidenceList = ({ evidence, ...props }) => (
  <dl className="evidence" {...props}>
    {evidence.map((e) => (
      <div key={e.label}><dt>{e.label}</dt><dd>{e.value}</dd></div>
    ))}
  </dl>
);

/** A collapsible list of the facts the card cites. */
function HowWeKnow({ payload }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  return (
    <>
      <div className="how">
        {payload.evidence.length > 0 && (
          <button aria-expanded={open} aria-controls={id} onClick={() => setOpen(!open)}>How we know</button>
        )}
      </div>
      <EvidenceList id={id} evidence={payload.evidence} hidden={!open} />
    </>
  );
}

/** Every overlay whose moment has come, newest first, as full cards. */
export default function Feed({ overlays, matchClock, selected = null }) {
  const list = useRef(null);
  useEffect(() => {
    const card = list.current?.querySelector(".selected");
    if (!card) return;
    const behavior = matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth";
    const column = card.closest(".feed-scroll"); // desktop: scroll the feed column only, so the timeline stays put
    if (column) column.scrollTo({ top: card.offsetTop - 8, behavior });
    else card.scrollIntoView({ block: "center", behavior });
  }, [selected]);
  const now = clockMs(matchClock);
  const items = [...overlays].filter(([, o]) => o.show_at_ms <= now).sort(([, a], [, b]) => b.show_at_ms - a.show_at_ms);
  const chip = (o) => <span className={`chip ${o.type}`}>{LABEL[o.type] ?? o.type}</span>;

  return (
    <div ref={list} className="feed" aria-live="polite">
      {!items.length && <p className="empty">Moments appear here as the match unfolds.</p>}
      {items.map(([key, o]) => (
        <article key={key} className={`card ${o.type}${key === selected ? " selected" : ""}`}
                 style={edge(o.payload.player)}>
          <header>{chip(o)}<span className="minute">{o.match_clock}</span></header>
          <div>
            <h3>{o.payload.headline}</h3>
            <p>{o.payload.detail}</p>
            <p className="why">{o.payload.why}</p>
          </div>
          <HowWeKnow payload={o.payload} />
        </article>
      ))}
    </div>
  );
}
