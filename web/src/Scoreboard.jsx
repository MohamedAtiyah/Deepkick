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
  // A club's two colours as CSS variables, e.g. --primary/--secondary or --home-club/--home-club-2.
  const colours = (id, a, b) => ({ [a]: CLUBS[id]?.colours.primary, [b]: CLUBS[id]?.colours.secondary });
  const club = (id, side) => (
    <div className={`club ${side}`} style={colours(id, "--primary", "--secondary")}>
      <span className="badge">{CLUBS[id]?.short_name ?? id}</span>
      {/* The short form ("Calderbrook") fits beside the chat and the feed; the full name is on hover. */}
      <span className="name" title={state.teams[id]?.name}>{(state.teams[id]?.name ?? id).split(" ")[0]}</span>
    </div>
  );
  return (
    <div className="scoreboard" style={{ ...colours(home, "--home-club", "--home-club-2"),
                                         ...colours(away, "--away-club", "--away-club-2") }}>
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
