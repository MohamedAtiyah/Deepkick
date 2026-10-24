import { useEffect, useRef, useState } from "react";
import { EvidenceList } from "./Feed.jsx";
import { Text } from "./OverlayStack.jsx";

const LENGTH = "Ask a question of 3 to 200 characters.";
const BADGE = { model: "AI-explained", declined: "Declined" };

/** "Ask about this match": one question at a time to POST /ask, answered from the live facts or declined. */
export default function Ask({ profile, about, onClearAbout }) {
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null); // {answer, evidence, source} or {error}
  const input = useRef(null);
  const dir = profile.lang === "ar" ? "rtl" : "ltr";

  useEffect(() => {
    if (about) input.current?.focus(); // "Ask about this" on a card: straight to typing
  }, [about]);

  const submit = async (event) => {
    event.preventDefault();
    const q = question.trim();
    if (q.length < 3 || q.length > 200) return setResult({ error: LENGTH });
    setBusy(true);
    setResult(null);
    try {
      const response = await fetch("/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: q, persona: profile.persona, lang: profile.lang,
                               overlay_id: about?.overlay_id ?? null }),
      });
      const body = await response.json().catch(() => ({}));
      if (response.ok) setResult(body);
      else if (response.status === 429) setResult({ error: body.detail ?? "Lots of questions. Try again soon." });
      else if (response.status === 422) setResult({ error: LENGTH });
      else if (response.status === 404) setResult({ error: "That moment can't be asked about yet." });
      else setResult({ error: "The match centre couldn't answer just now. Try again." });
    } catch {
      setResult({ error: "The match centre couldn't be reached. Try again." });
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="ask" aria-labelledby="ask-title">
      <h2 id="ask-title" className="section-title">Ask about this match</h2>
      {about && (
        <p className="ask-about">
          <span>About {about.match_clock}: <Text>{about.payload.headline}</Text></span>
          <button onClick={onClearAbout} aria-label="Ask about the whole match instead">✕</button>
        </p>
      )}
      <form onSubmit={submit}>
        <input ref={input} value={question} onChange={(e) => setQuestion(e.target.value)} disabled={busy}
               maxLength={200} placeholder="How many shots have Calderbrook had?" aria-label="Your question" />
        <button type="submit" disabled={busy}>{busy ? "Thinking…" : "Ask"}</button>
      </form>
      <div aria-live="polite">
        {result?.error && <p className="ask-error">{result.error}</p>}
        {result?.answer && (
          <div className="ask-answer">
            <span className={`source ${result.source}`}>{BADGE[result.source]}</span>
            <p lang={profile.lang} dir={dir}><Text>{result.answer}</Text></p>
            {result.evidence.length > 0 && <EvidenceList evidence={result.evidence} lang={profile.lang} dir={dir} />}
          </div>
        )}
      </div>
    </section>
  );
}
