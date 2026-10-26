import { useEffect, useId, useRef, useState } from "react";
import { clockMs } from "./live.js";
import { LABEL, Text, Untranslated, dirOf } from "./OverlayStack.jsx";
import league from "../../data/league.json";

// A card's edge takes the club colours of the player it is about (match-wide moments keep the accent).
const CLUB_OF = Object.fromEntries(league.clubs.flatMap((club) =>
  [...club.starters, ...club.bench].map((p) => [p.name, club.colours])));
const edge = (player) => CLUB_OF[player] && { "--club": CLUB_OF[player].primary,
                                               "--club-2": CLUB_OF[player].secondary };

/** A list of cited facts as label: value; values stay left-to-right in Arabic. */
export const EvidenceList = ({ evidence, lang, dir, ...props }) => (
  <dl className="evidence" lang={lang} dir={dir} {...props}>
    {evidence.map((e) => (
      <div key={e.label}><dt>{e.label}</dt><dd><bdi dir="ltr">{e.value}</bdi></dd></div>
    ))}
  </dl>
);

/** A collapsible list of the facts the card cites. */
function HowWeKnow({ payload, lang, dir }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  return (
    <>
      <div className="how">
        {payload.evidence.length > 0 && (
          <button aria-expanded={open} aria-controls={id} onClick={() => setOpen(!open)}>How we know</button>
        )}
      </div>
      <EvidenceList id={id} evidence={payload.evidence} lang={lang} dir={dir} hidden={!open} />
    </>
  );
}

/** Every overlay whose moment has come, newest first: full cards, or one line each when `compact`. */
export default function Feed({ overlays, matchClock, lang, compact = false, selected = null }) {
  const list = useRef(null);
  useEffect(() => {
    const card = list.current?.querySelector(".selected");
    if (!card || compact) return; // the compact list sits beside the full feed: only the feed scrolls
    const behavior = matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth";
    const column = card.closest(".feed-scroll"); // desktop: scroll the feed column only, so the timeline stays put
    if (column) column.scrollTo({ top: card.offsetTop - 8, behavior });
    else card.scrollIntoView({ block: "center", behavior });
  }, [selected]);
  const now = clockMs(matchClock);
  const items = [...overlays].filter(([, o]) => o.show_at_ms <= now).sort(([, a], [, b]) => b.show_at_ms - a.show_at_ms);
  const chip = (o) => <span className={`chip ${o.type}`}>{LABEL[o.type] ?? o.type}</span>;

  return (
    <div ref={list} className={compact ? "feed compact" : "feed"} aria-live={compact ? undefined : "polite"}>
      {!items.length && <p className="empty">Moments appear here as the match unfolds.</p>}
      {compact ? (
        <ol className="timeline">
          {items.map(([key, o]) => (
            <li key={key} className={key === selected ? "selected" : undefined}>
              <span className="minute">{o.match_clock}</span>
              {chip(o)}
              <span lang={o.lang} dir={dirOf(o.lang)}><Text>{o.payload.headline}</Text></span>
            </li>
          ))}
        </ol>
      ) : (
        items.map(([key, o]) => (
          <article key={key} className={`card ${o.type}${key === selected ? " selected" : ""}`}
                   style={edge(o.payload.player)}>
            <header>{chip(o)}<span className="minute">{o.match_clock}</span></header>
            <div lang={o.lang} dir={dirOf(o.lang)}>
              <h3><Text>{o.payload.headline}</Text></h3>
              <p><Text>{o.payload.detail}</Text></p>
              <p className="why"><Text>{o.payload.why}</Text></p>
            </div>
            <Untranslated wanted={lang} got={o.lang} />
            <HowWeKnow payload={o.payload} lang={o.lang} dir={dirOf(o.lang)} />
          </article>
        ))
      )}
    </div>
  );
}
