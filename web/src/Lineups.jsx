import league from "../../data/league.json";

const CLUBS = Object.fromEntries(league.clubs.map((club) => [club.id, club]));

/** Both squads from league.json: formation, manager, starting eleven and bench, home side first. */
export default function Lineups({ state }) {
  const ids = state?.score ? Object.keys(state.score) : league.clubs.map((club) => club.id);
  const squad = (players) => (
    <ol>
      {players.map((p) => (
        <li key={p.id}><span className="number">{p.number}</span><span>{p.name}</span><span className="position">{p.position}</span></li>
      ))}
    </ol>
  );
  return (
    <div className="lineups">
      {ids.map((id) => CLUBS[id]).filter(Boolean).map((club) => (
        <section key={club.id} style={{ "--primary": club.colours.primary, "--secondary": club.colours.secondary }}>
          <h3><span className="badge">{club.short_name}</span>{club.name}</h3>
          <p className="meta">{club.formation.name} · Manager {club.manager}</p>
          <h4>Starting XI</h4>
          {squad(club.starters)}
          <h4>Bench</h4>
          {squad(club.bench)}
        </section>
      ))}
    </div>
  );
}
