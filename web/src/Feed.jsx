import { useEffect, useId, useRef, useState } from "react";
import { clockMs } from "./live.js";
import { LABEL, Text } from "./OverlayStack.jsx";

export const SOURCE = { model: "AI-explained", template: "Stats template" };

/** A list of cited facts as label: value; values stay left-to-right in Arabic. */
export const EvidenceList = ({ evidence, lang, dir, ...props }) => (
  <dl className="evidence" lang={lang} dir={dir} {...props}>
    {evidence.map((e) => (
      <div key={e.label}><dt>{e.label}</dt><dd><bdi dir="ltr">{e.value}</bdi></dd></div>
    ))}
  </dl>
);

/** Who wrote the card, an "Ask about this" button, and a collapsible list of the facts it cites. */
function HowWeKnow({ payload, lang, dir, onAsk }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  return (
    <>
      <div className="how">
        <span className={`source ${payload.source}`}>{SOURCE[payload.source]}</span>
        {onAsk && <button onClick={onAsk}>Ask about this</button>}
        {payload.evidence.length > 0 && (
          <button aria-expanded={open} aria-controls={id} onClick={() => setOpen(!open)}>How we know</button>
        )}
      </div>
      <EvidenceList id={id} evidence={payload.evidence} lang={lang} dir={dir} hidden={!open} />
    </>
  );
}

/** Every overlay whose moment has come, newest first: full cards, or one line each when `compact`. */
export default function Feed({ overlays, matchClock, lang, compact = false, selected = null, onAsk = null }) {
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
  const dir = lang === "ar" ? "rtl" : "ltr";
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
              <span lang={lang} dir={dir}><Text>{o.payload.headline}</Text></span>
            </li>
          ))}
        </ol>
      ) : (
        items.map(([key, o]) => (
          <article key={key} className={`card ${o.type}${key === selected ? " selected" : ""}`}>
            <header>{chip(o)}<span className="minute">{o.match_clock}</span></header>
            <div lang={lang} dir={dir}>
              <h3><Text>{o.payload.headline}</Text></h3>
              <p><Text>{o.payload.detail}</Text></p>
              <p className="why"><Text>{o.payload.why}</Text></p>
            </div>
            <HowWeKnow payload={o.payload} lang={lang} dir={dir} onAsk={onAsk && (() => onAsk(o))} />
          </article>
        ))
      )}
    </div>
  );
}
