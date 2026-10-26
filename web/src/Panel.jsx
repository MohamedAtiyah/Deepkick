const PERSONAS = [["analyst", "Analyst"], ["casual", "Casual fan"], ["player", "Player focus"]];

/** The personalisation control: the persona, as a compact select. */
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
    </div>
  );
}
