import { useRef, useState } from "react";
import league from "../../data/league.json";
import { LABEL, Text } from "./OverlayStack.jsx";

const MINUTES = 10;
const CLUBS = Object.fromEntries(league.clubs.map((club) => [club.id, club]));
const score = (s) => {
  const [home, away] = Object.keys(s);
  return home ? `${home} ${s[home]}–${s[away]} ${away}` : "0–0";
};

/** "Catch me up": the last ten match minutes from /catchup, in a modal dialog. Spoiler-safe: nothing after now. */
export default function CatchUp({ profile }) {
  const dialog = useRef(null);
  const [data, setData] = useState(null);
  const [failed, setFailed] = useState(false);

  const open = () => {
    setData(null);
    setFailed(false);
    dialog.current.showModal();
    const params = new URLSearchParams({ persona: profile.persona, lang: profile.lang, minutes: MINUTES });
    fetch(`/catchup?${params}`)
      .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
      .then(setData)
      .catch(() => setFailed(true));
  };
  const dir = profile.lang === "ar" ? "rtl" : "ltr";
  const leader = data?.momentum && CLUBS[data.momentum.team];

  return (
    <>
      <button className="toggle catch-up" onClick={open}>Catch me up</button>
      <dialog ref={dialog} className="catch-up-panel" aria-labelledby="catch-up-title">
        <header>
          <h2 id="catch-up-title">Catch me up</h2>
          <button onClick={() => dialog.current.close()} aria-label="Close">✕</button>
        </header>
        {failed && <p className="empty">The match centre could not load the catch-up. Try again in a moment.</p>}
        {!data && !failed && <p className="empty">Loading…</p>}
        {data && (
          <>
            <p className="window">The last {data.minutes} minutes: {data.from} to {data.at}</p>
            <dl className="summary">
              <div>
                <dt>Score</dt>
                <dd dir="ltr">
                  {data.score.goals ? `${score(data.score.then)} → ${score(data.score.now)}` : score(data.score.now)}
                  <small> ({data.score.goals || "no"} goal{data.score.goals === 1 ? "" : "s"})</small>
                </dd>
              </div>
              {leader && (
                <div>
                  <dt>Momentum</dt>
                  <dd>With {leader.name} ({data.momentum.value}%)</dd>
                </div>
              )}
            </dl>
            {!data.moments.length && <p className="empty">Nothing flagged in that spell.</p>}
            <ol className="timeline">
              {data.moments.map((m) => (
                <li key={m.moment_id}>
                  <span className="minute">{m.match_clock}</span>
                  <span className={`chip ${m.type}`}>{LABEL[m.type] ?? m.type}</span>
                  <span lang={profile.lang} dir={dir}><Text>{m.headline}</Text></span>
                </li>
              ))}
            </ol>
          </>
        )}
      </dialog>
    </>
  );
}
