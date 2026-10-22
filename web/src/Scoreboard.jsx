import league from "../../data/league.json";

const COLOURS = Object.fromEntries(league.clubs.map((club) => [club.id, club.colours]));
const PERIOD = { not_started: "Pre-match", kickoff: "1st half", half_time: "Half-time", second_half: "2nd half",
  full_time: "Full-time" };

export default function Scoreboard({ state }) {
  if (!state?.match_id) return <div className="scoreboard waiting">Waiting for kick-off…</div>;
  const [home, away] = Object.keys(state.score);
  const club = (id) => (
    <div className="club" style={{ "--primary": COLOURS[id]?.primary, "--secondary": COLOURS[id]?.secondary }}>
      <span className="badge">{id}</span>
      <span className="name">{state.teams[id]?.name ?? id}</span>
    </div>
  );
  return (
    <div className="scoreboard">
      {club(home)}
      <div className="score">
        {state.score[home]}<span>–</span>{state.score[away]}
      </div>
      {club(away)}
      <div className="clock">
        <span>{state.match_clock}</span>
        <small>{PERIOD[state.status] ?? `Half ${state.half}`}</small>
      </div>
    </div>
  );
}
