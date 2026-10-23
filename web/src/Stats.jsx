import league from "../../data/league.json";

const SHORT = Object.fromEntries(league.clubs.map((club) => [club.id, club.short_name]));
// [label, value from one team's /state stats, unit]
const ROWS = [
  ["Possession", (t) => t.possession_pct, "%"],
  ["Shots", (t) => t.shots],
  ["Shots on target", (t) => t.shots_on_target],
  ["Expected goals", (t) => t.xg],
  ["Passes", (t) => t.passes],
  ["Pass accuracy", (t) => t.pass_accuracy_pct, "%"],
  ["Tackles won", (t) => t.tackles_won],
  ["Pressures won", (t) => t.pressures_won],
  ["Top shot speed", (t) => t.top_shot_speed_kmh, " km/h"],
];

/** Head-to-head team stats from /state, with a bar showing each side's share. */
export default function Stats({ state }) {
  if (!state?.teams) return <p className="empty">Stats appear at kick-off.</p>;
  const [home, away] = Object.keys(state.score);
  const minutes = state.momentum_window_minutes;
  const rows = [...ROWS, [`Momentum (last ${minutes} min)`, (t) => t.momentum.value]];
  return (
    <table className="stats">
      <caption className="sr-only">Team stats</caption>
      <thead>
        <tr><th scope="col" className="home">{SHORT[home] ?? home}</th><th scope="col">Stat</th>
          <th scope="col" className="away">{SHORT[away] ?? away}</th></tr>
      </thead>
      <tbody>
        {rows.map(([label, get, unit = ""]) => {
          const h = get(state.teams[home]) ?? 0;
          const a = get(state.teams[away]) ?? 0;
          const share = h + a ? (h / (h + a)) * 100 : 50;
          return (
            <tr key={label}>
              <td className="home">{h}{unit}</td>
              <th scope="row">
                {label}
                <span className="bar" style={{ "--share": `${share}%` }} aria-hidden="true" />
              </th>
              <td className="away">{a}{unit}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
