import { useEffect, useState } from "react";
import league from "../../data/league.json";
import { clockMs, momentKey } from "./live.js";
import { LABEL } from "./OverlayStack.jsx";

const CLUBS = Object.fromEntries(league.clubs.map((club) => [club.id, club]));
const FULL = 90; // minutes on the axis; it stretches if the clock runs past
const HALF = 45;

/** Polls /timeline every 2 seconds and whenever a new overlay arrives. */
function useTimeline(profile, overlayCount) {
  const [data, setData] = useState(null);
  useEffect(() => {
    let alive = true;
    const params = new URLSearchParams({ persona: profile.persona, lang: profile.lang });
    const load = () =>
      fetch(`/timeline?${params}`)
        .then((r) => (r.ok ? r.json() : null))
        .then((d) => alive && d && setData(d))
        .catch(() => {});
    load();
    const id = setInterval(load, 2000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [profile.persona, profile.lang, overlayCount]);
  return data;
}

/** Momentum above (home) and below (away) a centre line, moment markers, the half-time line and the clock. */
export default function Timeline({ state, profile, overlays, selected, onSelect }) {
  const data = useTimeline(profile, overlays.size);
  const [hover, setHover] = useState(null);
  if (!data || !state?.score) return null;

  const [home, away] = Object.keys(state.score).map((id) => CLUBS[id]);
  const now = clockMs(state.match_clock) / 60000;
  const span = Math.max(FULL, Math.ceil(now));
  const x = (minutes) => `${(minutes / span) * 100}%`;
  const colours = (club) => club && { "--primary": club.colours.primary, "--secondary": club.colours.secondary };

  // Markers in the same minute stack outward from the centre line instead of overlapping.
  const perMinute = {};
  const markers = data.moments.map((m) => {
    const stack = (perMinute[m.minute] = (perMinute[m.minute] ?? 0) + 1) - 1;
    const side = m.team && home && m.team !== home.id ? 1 : -1; // home above the line, away and match-wide below
    const key = momentKey(m);
    return { ...m, key, offset: side * stack * 14, headline: overlays.get(key)?.payload.headline ?? LABEL[m.type] };
  });

  return (
    <section className="timeline-card" aria-label="Match timeline">
      <div className="timeline-plot">
        <svg viewBox={`0 -100 ${span} 200`} preserveAspectRatio="none" aria-hidden="true">
          {data.minutes.map(({ minute, momentum }) => (
            <rect key={minute} className="bar" style={colours(momentum >= 0 ? home : away)}
                  x={minute - 1 + 0.08} width={0.84} y={momentum >= 0 ? -momentum : 0} height={Math.abs(momentum)} />
          ))}
          <line className="centre" x1={0} x2={span} y1={0} y2={0} />
          <line className="half" x1={HALF} x2={HALF} y1={-100} y2={100} />
          <line className="cursor" x1={now} x2={now} y1={-100} y2={100} />
        </svg>
        {home && <span className="side home" style={colours(home)}>{home.short_name}</span>}
        {away && <span className="side away" style={colours(away)}>{away.short_name}</span>}
        <span className="axis" style={{ left: x(HALF) }}>HT</span>
        {markers.map((m) => (
          <button key={m.key} style={{ left: x(clockMs(m.match_clock) / 60000), "--offset": `${m.offset}px`,
                                       ...colours(CLUBS[m.team]) }}
                  className={`marker ${m.type}${m.team ? "" : " neutral"}${selected?.key === m.key ? " on" : ""}`}
                  aria-label={`${m.match_clock} ${LABEL[m.type] ?? m.type}: ${m.headline}`}
                  aria-pressed={selected?.key === m.key}
                  onMouseEnter={() => setHover(m)} onMouseLeave={() => setHover(null)}
                  onFocus={() => setHover(m)} onBlur={() => setHover(null)}
                  onClick={() => onSelect(selected?.key === m.key ? null : m)} />
        ))}
        {hover && (
          <div className="tip" role="presentation" style={{ left: x(clockMs(hover.match_clock) / 60000) }}
               lang={profile.lang} dir={profile.lang === "ar" ? "rtl" : "ltr"}>
            <strong dir="ltr">{hover.match_clock}</strong> {hover.headline}
          </div>
        )}
      </div>
    </section>
  );
}
