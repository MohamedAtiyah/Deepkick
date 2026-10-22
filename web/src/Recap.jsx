import { useEffect, useState } from "react";
import { Text } from "./OverlayStack.jsx";

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
      <h3>Recap</h3>
      {!items.length && <p className="empty">Nothing yet.</p>}
      <ol lang={lang} dir={lang === "ar" ? "rtl" : "ltr"}>
        {items.map((o) => (
          <li key={o.overlay_id} className={o.type}>
            <span className="minute">{o.match_clock}</span>
            <div>
              <strong><Text>{o.payload.headline}</Text></strong>
              <p><Text>{o.payload.detail}</Text></p>
            </div>
          </li>
        ))}
      </ol>
    </section>
  );
}
