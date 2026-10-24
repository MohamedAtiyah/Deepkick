const PERSONAS = [["analyst", "Analyst"], ["casual", "Casual fan"], ["player", "Player focus"]];
const LANGUAGES = [["en", "English"], ["ar", "العربية"]];

/** The personalisation controls as compact selects: persona and language. */
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
    </div>
  );
}
