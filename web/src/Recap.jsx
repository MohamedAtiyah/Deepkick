import { useEffect, useState } from "react";
import { Text, Untranslated, dirOf } from "./OverlayStack.jsx";

/** The half-time and full-time recaps for the current persona and language, refreshed every 5 seconds. */
export default function Recap({ persona, lang }) {
  const [items, setItems] = useState([]);
  useEffect(() => {
    let alive = true;
    const load = () =>
      fetch(`/recap?persona=${persona}&lang=${lang}`)
        .then((r) => (r.ok ? r.json() : []))
        .then((list) => alive && setItems(list))
        .catch(() => {});
    load();
    const id = setInterval(load, 5000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [persona, lang]);

  return (
    <section className="recap">
      {!items.length && <p className="empty">The half-time recap appears at the break.</p>}
      {items.map((r) => (
        <article key={r.recap_id} className="recap-item">
          <header>
            <span className="chip">{r.type === "half_time" ? "Half-time" : "Full-time"}</span>
            <span className="minute">{r.match_clock}</span>
          </header>
          <p lang={r.lang} dir={dirOf(r.lang)}><Text>{r.text}</Text></p>
          <Untranslated wanted={lang} got={r.lang} />
        </article>
      ))}
    </section>
  );
}
