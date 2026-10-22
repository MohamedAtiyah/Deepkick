import league from "../../data/league.json";

const PERSONAS = [["analyst", "Analyst"], ["casual", "Casual fan"], ["player", "Player focus"]];
const LANGUAGES = [["en", "English"], ["ar", "العربية"]];

export default function Panel({ profile, onChange }) {
  const set = (field) => (event) => onChange({ ...profile, [field]: event.target.value });
  return (
    <section className="panel">
      <h3>Your broadcast</h3>
      <div className="personas" role="radiogroup" aria-label="Persona">
        {PERSONAS.map(([id, label]) => (
          <button key={id} role="radio" aria-checked={profile.persona === id}
                  className={profile.persona === id ? "on" : ""} onClick={() => onChange({ ...profile, persona: id })}>
            {label}
          </button>
        ))}
      </div>
      <label>
        Favourite club
        <select value={profile.club} onChange={set("club")}>
          <option value="">None</option>
          {league.clubs.map((club) => <option key={club.id} value={club.id}>{club.name}</option>)}
        </select>
      </label>
      <label>
        Favourite player
        <select value={profile.player} onChange={set("player")}>
          <option value="">None</option>
          {league.clubs.map((club) => (
            <optgroup key={club.id} label={club.name}>
              {[...club.starters, ...club.bench].map((p) => (
                <option key={p.id} value={p.id}>{p.number}. {p.name}</option>
              ))}
            </optgroup>
          ))}
        </select>
      </label>
      <label>
        Language
        <select value={profile.lang} onChange={set("lang")}>
          {LANGUAGES.map(([code, name]) => <option key={code} value={code}>{name}</option>)}
        </select>
      </label>
    </section>
  );
}
