import league from "../../data/league.json";
import { useSmoothClock } from "./live.js";

const CLUBS = Object.fromEntries(league.clubs.map((club) => [club.id, club]));
const PERIOD = { not_started: "Pre-match", kickoff: "1st half", half_time: "Half-time", second_half: "2nd half",
  full_time: "Full-time" };
const STATUS_LABEL = { live: "Live", connecting: "Connecting…", reconnecting: "Reconnecting…" };

/** The score strip: both clubs in their colours, score, smooth match clock, period and the feed's connection. */
export default function Scoreboard({ state, status }) {
  const clock = useSmoothClock(state);
  const connection = <div className={`connection ${status}`} role="status">{STATUS_LABEL[status]}</div>;
  if (!state?.match_id) return <div className="scoreboard waiting">{connection}<span>Waiting for kick-off…</span></div>;

  const [home, away] = Object.keys(state.score);
  const club = (id, side) => (
    <div className={`club ${side}`} style={{ "--primary": CLUBS[id]?.colours.primary, "--secondary": CLUBS[id]?.colours.secondary }}>
      <span className="badge">{CLUBS[id]?.short_name ?? id}</span>
      <span className="name">{state.teams[id]?.name ?? id}</span>
    </div>
  );
  return (
    <div className="scoreboard">
      {connection}
      {club(home, "home")}
      <div className="score" aria-label={`${state.score[home]} to ${state.score[away]}`}>
        {state.score[home]}<span aria-hidden="true">–</span>{state.score[away]}
      </div>
      {club(away, "away")}
      <div className="clock">
        <span>{clock}</span>
        <small>{PERIOD[state.status] ?? `Half ${state.half}`}</small>
      </div>
    </div>
  );
}
