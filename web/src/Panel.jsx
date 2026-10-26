import league from "../../data/league.json";

const PERSONAS = [["analyst", "Analyst"], ["casual", "Casual fan"], ["player", "Player focus"]];
const LANGUAGES = [["en", "English"], ["ar", "العربية"]];

/** The personalisation controls as compact selects: persona, language, favourite club and player. */
export default function Panel({ profile, onChange }) {
  const set = (field) => (event) => onChange({ ...profile, [field]: event.target.value });
  return (
    <div className="controls">
      <label>
        <span>Persona</span>
        <select value={profile.persona} onChange={set("persona")}>
          {PERSONAS.map(([id, label]) => <option key={id} value={id}>{label}</option>)}
        </select>
      </label>
      <label>
        <span>Language</span>
        <select value={profile.lang} onChange={set("lang")}>
          {LANGUAGES.map(([code, name]) => <option key={code} value={code}>{name}</option>)}
        </select>
      </label>
      <label>
        <span>Club</span>
        <select value={profile.club} onChange={set("club")}>
          <option value="">Any</option>
          {league.clubs.map((club) => <option key={club.id} value={club.id}>{club.name}</option>)}
        </select>
      </label>
      <label>
        <span>Player</span>
        <select value={profile.player} onChange={set("player")}>
          <option value="">Any</option>
          {league.clubs.map((club) => (
            <optgroup key={club.id} label={club.name}>
              {[...club.starters, ...club.bench].map((p) => (
                <option key={p.id} value={p.id}>{p.number}. {p.name}</option>
              ))}
            </optgroup>
          ))}
        </select>
      </label>
    </div>
  );
}
