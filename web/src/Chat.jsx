import { useEffect, useRef, useState } from "react";
import league from "../../data/league.json";
import { Text } from "./OverlayStack.jsx";

const MAX_CHARS = 300; // the server's limit per message
const HISTORY = 6; // messages the server keeps
const [HOME, AWAY] = league.clubs;
const number9 = (club) => club.starters.find((p) => p.number === 9);
const striker = HOME.starters.find((p) => ["ST", "RST", "LST", "CF"].includes(p.position)) ?? number9(HOME);
const STARTERS = [
  "Who was the fastest sprinter?",
  `How did ${striker.name} play?`,
  `Compare the two number 9s, ${number9(HOME).name} and ${number9(AWAY).name}`,
  "Where was the game won?",
];
const TOOL_LABEL = { find_player: "find player", player_stats: "player stats", top_sprints: "top sprints",
  team_stats: "team stats", compare_players: "compare players", list_moments: "moments", goals: "goals",
  shots: "shots" };
const PROBLEM = {
  422: "Keep each message under 300 characters.",
  network: "The match chat couldn't be reached. Try again in a moment.",
  other: "The match chat couldn't answer just now. Try again in a moment.",
};

const value = (v, unit) => <><bdi dir="ltr">{v}</bdi>{unit && <small> {unit}</small>}</>;

/** A stat card built by the server from tool results: stat block, ranked bars, comparison or moment list. */
function Card({ card, dir }) {
  if (card.kind === "ranking") {
    const top = Math.max(...card.rows.map((r) => Number(r.value) || 0), 1);
    return (
      <figure className="stat-card ranking" dir={dir}>
        <figcaption>{card.title}</figcaption>
        <ol>
          {card.rows.map((r) => (
            <li key={r.label}>
              <span className="label">{r.label}</span>
              <span className="bar" style={{ "--share": `${(Number(r.value) / top) * 100}%` }} aria-hidden="true" />
              <span className="value">{value(r.value, r.unit)}</span>
            </li>
          ))}
        </ol>
      </figure>
    );
  }
  if (card.kind === "comparison") {
    return (
      <figure className="stat-card comparison" dir={dir}>
        <figcaption>{card.title}</figcaption>
        <table>
          <thead><tr><th scope="col"><span className="sr-only">Stat</span></th>
            {card.columns.map((c) => <th key={c} scope="col">{c}</th>)}</tr></thead>
          <tbody>
            {card.rows.map((r) => (
              <tr key={r.label}><th scope="row">{r.label}</th>
                {r.value.map((v, i) => <td key={i}>{value(v, r.unit)}</td>)}</tr>
            ))}
          </tbody>
        </table>
      </figure>
    );
  }
  return (
    <figure className={`stat-card ${card.kind}`} dir={dir}>
      <figcaption>{card.title}</figcaption>
      <dl>
        {card.rows.map((r, i) => (
          <div key={`${r.label}-${i}`}><dt>{r.label}</dt><dd>{value(r.value, r.unit)}</dd></div>
        ))}
      </dl>
    </figure>
  );
}

/** "Ask the match": a chat answered from spoiler-safe match tools, with the facts it used as cards. */
export default function Chat({ profile, clock, docked, open, onClose }) {
  const [messages, setMessages] = useState([]); // {role, content, lang?, cards?, tools?, source?, problem?}
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const input = useRef(null);
  const list = useRef(null);

  useEffect(() => {
    if (open) input.current?.focus({ preventScroll: true }); // the drawer or sheet just opened
  }, [open]);
  useEffect(() => {
    list.current?.scrollTo({ top: list.current.scrollHeight });
  }, [messages, busy]);

  const send = async (text) => {
    const question = text.trim();
    if (!question || busy) return;
    if (question.length > MAX_CHARS) {
      setMessages((m) => [...m, { role: "notice", content: PROBLEM[422] }]);
      return;
    }
    const conversation = [...messages.filter((m) => m.role !== "notice"), { role: "user", content: question }];
    setMessages((m) => [...m, { role: "user", content: question }]);
    setDraft("");
    setBusy(true);
    try {
      const response = await fetch("/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          persona: profile.persona, lang: profile.lang,
          // The server keeps 6 messages of at most 300 characters; longer answers are trimmed for context.
          messages: conversation.slice(-HISTORY).map((m) => ({ role: m.role, content: m.content.slice(0, MAX_CHARS) })),
        }),
      });
      const body = await response.json().catch(() => ({}));
      if (response.ok) {
        setMessages((m) => [...m, { role: "assistant", content: body.answer, lang: profile.lang, cards: body.cards,
                                     tools: body.tools_used, source: body.source, until: body.data_until_clock }]);
      } else {
        const notice = response.status === 429 ? body.detail : PROBLEM[response.status] ?? PROBLEM.other;
        setMessages((m) => [...m, { role: "notice", content: notice ?? PROBLEM.other }]);
      }
    } catch {
      setMessages((m) => [...m, { role: "notice", content: PROBLEM.network }]);
    } finally {
      setBusy(false);
      input.current?.focus({ preventScroll: true });
    }
  };

  const onKeyDown = (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault(); // Enter sends; Shift+Enter keeps its newline
      send(draft);
    }
  };

  return (
    <section className="chat" aria-labelledby="chat-title">
      <header className="chat-head">
        <div>
          <h2 id="chat-title">Ask the match</h2>
          <p className="chat-until">Data up to <bdi dir="ltr">{clock ?? "00:00"}</bdi></p>
        </div>
        {messages.length > 0 && (
          <button className="chat-clear" onClick={() => setMessages([])} disabled={busy}>Clear chat</button>
        )}
        {!docked && <button className="chat-close" onClick={onClose} aria-label="Close the chat">✕</button>}
      </header>

      <div className="chat-log" ref={list} aria-live="polite" aria-busy={busy}>
        {!messages.length && (
          <div className="chat-empty">
            <p>Ask anything about this match. Answers come only from the match data, up to the live clock.</p>
            <div className="starters">
              {STARTERS.map((s) => <button key={s} onClick={() => send(s)} disabled={busy}>{s}</button>)}
            </div>
          </div>
        )}
        {messages.map((m, i) => (
          <div key={i} className={`bubble ${m.role}${m.source === "declined" ? " declined" : ""}`}>
            <p lang={m.lang} dir={m.lang === "ar" ? "rtl" : "ltr"}><Text>{m.content}</Text></p>
            {m.tools?.length > 0 && (
              <p className="used">Used: {m.tools.map((t) => <span key={t}>{TOOL_LABEL[t] ?? t}</span>)}</p>
            )}
            {m.cards?.map((card, j) => <Card key={j} card={card} dir={m.lang === "ar" ? "rtl" : "ltr"} />)}
          </div>
        ))}
        {busy && (
          <div className="bubble assistant typing" aria-label="The match chat is answering">
            <span /><span /><span />
          </div>
        )}
      </div>

      <form className="chat-input" onSubmit={(e) => { e.preventDefault(); send(draft); }}>
        <textarea ref={input} value={draft} onChange={(e) => setDraft(e.target.value)} onKeyDown={onKeyDown}
                  disabled={busy} maxLength={MAX_CHARS} rows={2} aria-label="Your question"
                  placeholder="Ask about a player, a team or a moment" />
        <button type="submit" disabled={busy || !draft.trim()}>{busy ? "…" : "Send"}</button>
      </form>
    </section>
  );
}
