import { useEffect, useState } from "react";

export default function Recap({ persona }) {
  const [items, setItems] = useState([]);
  useEffect(() => {
    let alive = true;
    const load = () =>
      fetch(`/recap?persona=${persona}`)
        .then((r) => (r.ok ? r.json() : []))
        .then((list) => alive && setItems(list))
        .catch(() => {});
    load();
    const id = setInterval(load, 5000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [persona]);

  return (
    <section className="recap">
      <h3>Recap</h3>
      {!items.length && <p className="empty">Nothing yet.</p>}
      <ol>
        {items.map((o) => (
          <li key={o.overlay_id} className={o.type}>
            <span className="minute">{o.match_clock}</span>
            <div>
              <strong>{o.payload.headline}</strong>
              <p>{o.payload.detail}</p>
            </div>
          </li>
        ))}
      </ol>
    </section>
  );
}
