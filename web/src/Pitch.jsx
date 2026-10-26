// A full pitch to real proportions on the data's 105 x 68 metre system, with all 22 starters and the ball, animated
// from the event sequence. The SVG's units are metres; markers are placed by converting the same metres to
// percentages of the same box, written straight to the DOM each animation frame (no React render per frame).
import { useEffect, useRef } from "react";
import league from "../../data/league.json";
import { LENGTH, WIDTH, layout, toScreen } from "./pitchLayout.js";
import { approach, ballAt, ballMs, catchUp, toSteps } from "./motion.js";
import { clockMs } from "./live.js";

const VIEW = { x: -3, y: -1.5, w: LENGTH + 6, h: WIDTH + 3 }; // room outside the lines for the goals
const pct = (x, y) => ({ left: `${((x - VIEW.x) / VIEW.w) * 100}%`, top: `${((y - VIEW.y) / VIEW.h) * 100}%` });
const CLUBS = Object.fromEntries(league.clubs.map((club) => [club.id, club]));

const MID = WIDTH / 2;
const BOX = { depth: 16.5, width: 40.3 };
const SIX = { depth: 5.5, width: 18.3 };
const GOAL = { width: 7.32, depth: 2 };
const SPOT = 11;
const ARC = 9.15; // centre circle and penalty arc radius
const CORNER = 1;
const STRIPES = 12;

// The part of the 9.15 m circle around a penalty spot that lies outside the penalty area.
function dArc(side) {
  const spotX = side === "left" ? SPOT : LENGTH - SPOT;
  const edgeX = side === "left" ? BOX.depth : LENGTH - BOX.depth;
  const half = Math.sqrt(ARC ** 2 - (edgeX - spotX) ** 2);
  return `M ${edgeX} ${MID - half} A ${ARC} ${ARC} 0 0 ${side === "left" ? 1 : 0} ${edgeX} ${MID + half}`;
}

function Markings() {
  const end = (side) => {
    const goalLine = side === "left" ? 0 : LENGTH;
    const inward = side === "left" ? 1 : -1;
    const rect = (depth, width) => (
      <rect x={side === "left" ? 0 : LENGTH - depth} y={MID - width / 2} width={depth} height={width} />
    );
    return (
      <g key={side}>
        {rect(BOX.depth, BOX.width)}
        {rect(SIX.depth, SIX.width)}
        <circle className="spot" cx={goalLine + inward * SPOT} cy={MID} r={0.3} />
        <path d={dArc(side)} />
        <rect className="goal" x={side === "left" ? -GOAL.depth : LENGTH} y={MID - GOAL.width / 2}
              width={GOAL.depth} height={GOAL.width} />
      </g>
    );
  };
  const corners = [[0, 0, 1, 1], [LENGTH, 0, -1, 1], [0, WIDTH, 1, -1], [LENGTH, WIDTH, -1, -1]];
  return (
    <svg viewBox={`${VIEW.x} ${VIEW.y} ${VIEW.w} ${VIEW.h}`} aria-hidden="true">
      {Array.from({ length: STRIPES }, (_, i) => (
        <rect key={i} className={i % 2 ? "stripe dark" : "stripe"} x={(i * LENGTH) / STRIPES} y={0}
              width={LENGTH / STRIPES} height={WIDTH} />
      ))}
      <g className="lines">
        <rect x={0} y={0} width={LENGTH} height={WIDTH} />
        <line x1={LENGTH / 2} y1={0} x2={LENGTH / 2} y2={WIDTH} />
        <circle cx={LENGTH / 2} cy={MID} r={ARC} />
        <circle className="spot" cx={LENGTH / 2} cy={MID} r={0.3} />
        {end("left")}
        {end("right")}
        {corners.map(([x, y, dx, dy]) => (
          <path key={`${x}-${y}`}
                d={`M ${x + dx * CORNER} ${y} A ${CORNER} ${CORNER} 0 0 ${dx * dy > 0 ? 1 : 0} ${x} ${y + dy * CORNER}`} />
        ))}
      </g>
    </svg>
  );
}

// A marker never snaps: it moves at most a sprinter's 9 m/s of *match* time, so on screen that is 9 m/s live and
// faster on a sped-up replay (the replay rate is measured from the events), capped so it still reads as running.
const SPRINT_MS = 9;
const SCREEN_SPEED = { min: 9, max: 60 }; // metres per real second
const CLAIM_M = 1.8; // a player this close to a loose ball takes it
const WAIT_MS = 1200; // the longest a step waits for its player to reach the ball
const TOUCH_MS = 300; // a player who has just taken the ball keeps it this long before passing, shooting or running
const AHEAD_M = 0.9; // a carried ball sits this far ahead of the carrier's feet
const POLL_MS = 400;
const FLIGHT_SCALE_MAX = 2; // catching up never plays a ball's flight more than twice as fast
const KEEP_ON_SKIP = 3; // steps kept when the queue is too far behind: the rest of the backlog is dropped
const FLIGHT = new Set(["pass", "intercept", "loose", "shot"]); // steps where the ball travels on its own

/** The motion engine: events from /events become steps, played in order on the markers every animation frame. */
function usePitchMotion(home, clubs, refs) {
  useEffect(() => {
    const ids = clubs.flatMap((club) => club.starters.map((p) => p.id));
    const teamOf = Object.fromEntries(clubs.flatMap((club) => club.starters.map((p) => [p.id, club.id])));
    const shape = (ball) => Object.fromEntries(
      layout(clubs, home, { team: home, x: ball[0], y: ball[1] }).map((p) => [p.id, [p.x, p.y]]));
    const m = {
      pos: shape([LENGTH / 2, WIDTH / 2]), dir: {}, ball: [LENGTH / 2, WIDTH / 2], carrier: null, claim: null,
      queue: [], step: null, buffer: [], next: null, stopped: false, heldSince: 0,
      rate: 1, seen: null, // match seconds per real second, and the last (match clock, arrival) pair it came from
    };
    const reduced = matchMedia("(prefers-reduced-motion: reduce)");

    const draw = () => {
      for (const id of ids) {
        const el = refs.current.players.get(id);
        if (!el) continue;
        const [x, y] = m.pos[id];
        el.style.left = `${((x - VIEW.x) / VIEW.w) * 100}%`;
        el.style.top = `${((y - VIEW.y) / VIEW.h) * 100}%`;
        el.classList.toggle("on-ball", id === m.carrier);
      }
      const ball = refs.current.ball;
      if (ball) Object.assign(ball.style, pct(...m.ball));
    };

    const pulse = (side) => {
      const net = refs.current.nets[side];
      if (!net) return;
      net.classList.remove("pulse");
      void net.offsetWidth; // restart the animation
      net.classList.add("pulse");
    };

    // Each step starts from where things are now, so nothing jumps: the ball tweens from its current spot.
    const begin = (step) => {
      const s = { ...step, t: 0, waited: 0, origin: [...m.ball], start: step.player ? [...m.pos[step.player]] : null };
      const far = (to) => Math.hypot(m.ball[0] - to[0], m.ball[1] - to[1]) > 1;
      if (step.kind === "kickoff" && far([LENGTH / 2, WIDTH / 2])) {
        // The ball goes back to the centre spot: it travels there rather than appearing.
        Object.assign(s, { flight: true, to: [LENGTH / 2, WIDTH / 2], ms: ballMs(m.ball, [LENGTH / 2, WIDTH / 2]) });
        Object.assign(m, { carrier: null, claim: null });
      } else if (step.kind === "take" && far(step.to)) {
        Object.assign(s, { flight: true, ms: ballMs(m.ball, step.to) }); // a save, a restart: the ball travels there
        m.carrier = null;
      } else if (step.kind === "run" && step.withBall) {
        // He carries it only if he is on it; otherwise he runs and picks it up when he reaches it.
        const [x, y] = m.pos[step.player];
        if (m.carrier === step.player || Math.hypot(x - m.ball[0], y - m.ball[1]) <= CLAIM_M) m.carrier = step.player;
        else m.claim = step.player;
      }
      return s;
    };

    const finish = (s) => {
      if (s.kind === "pass") m.claim = s.receiver;
      else if (s.kind === "intercept") m.claim = s.by;
      else if (s.kind === "take") m.claim = s.player;
      else if (s.kind === "shot" && s.goal) pulse(s.side);
      if (FLIGHT.has(s.kind) || s.flight) m.ball = [...s.to];
    };

    // Who must be on the ball before a step can start (the passer, shooter or carrier).
    const actor = (s) => (["pass", "shot"].includes(s.kind) ? s.passer ?? s.player
      : s.kind === "intercept" || s.kind === "loose" ? s.passer : s.kind === "run" && s.withBall ? s.player : null);

    let last = performance.now();
    const frame = (now) => {
      if (m.stopped) return;
      const dt = Math.min(100, now - last);
      last = now;
      const remaining = (m.step ? Math.max(0, m.step.ms * (1 - m.step.t)) : 0) + m.queue.reduce((n, s) => n + s.ms, 0);
      let { scale, skip } = catchUp(remaining);
      if (reduced.matches) {
        // Reduced motion: every step lands at once and markers snap, with no tweening.
        scale = Infinity;
        for (const s of [...(m.step ? [m.step] : []), ...m.queue]) { begin(s); finish(s); }
        Object.assign(m, { step: null, queue: [] });
      } else if (skip) {
        // Too far behind the live clock: drop the stale backlog and play on from the latest steps. The ball travels
        // on from where it is rather than jumping, so the pitch catches up without anything teleporting.
        Object.assign(m, { step: null, claim: null, queue: m.queue.slice(-KEEP_ON_SKIP) });
      }
      if (!m.step && m.queue.length) m.step = begin(m.queue.shift());
      const s = m.step;
      const speed = Math.min(SCREEN_SPEED.max, Math.max(SCREEN_SPEED.min, SPRINT_MS * m.rate));
      // Catching up speeds players up too, but never past the screen cap: the dropped backlog absorbs the rest.
      const pace = Math.min(SCREEN_SPEED.max, speed * scale);
      const targets = shape(m.ball);
      if (s) {
        const who = actor(s);
        const gap = who ? Math.hypot(m.pos[who][0] - m.ball[0], m.pos[who][1] - m.ball[1]) : 0;
        const away = who && m.carrier !== who && gap > CLAIM_M;
        const touching = who && m.carrier === who && (now - m.heldSince) * scale < TOUCH_MS; // a first touch
        if ((away || touching) && s.waited < WAIT_MS) {
          s.waited += dt * scale; // the player is still getting to the ball
          targets[who] = m.ball;
        } else {
          if (s.t === 0 && (FLIGHT.has(s.kind) || s.flight)) {
            // The ball leaves the feet now, even if its player only reached it while this step waited.
            m.carrier = null;
            if (m.claim === who) m.claim = null;
          }
          const tempo = FLIGHT.has(s.kind) || s.flight ? Math.min(scale, FLIGHT_SCALE_MAX) : scale;
          s.t = s.ms ? Math.min(1, s.t + (dt * tempo) / s.ms) : 1;
          if (FLIGHT.has(s.kind) || s.flight) m.ball = ballAt(s.origin, s.to, s.t);
          if (s.kind === "pass") targets[s.receiver] = s.to;
          if (s.kind === "intercept") targets[s.by] = s.to;
          if (s.kind === "take" || s.kind === "duel") targets[s.player] = s.to;
          if (s.kind === "run") {
            // Along the run at its own speed, but never faster than a sprint at the current playback speed.
            const [fx, fy] = s.start;
            const along = [fx + (s.to[0] - fx) * s.t, fy + (s.to[1] - fy) * s.t];
            m.pos[s.player] = approach(m.pos[s.player], along, (1.5 * pace * dt) / 1000);
            m.dir[s.player] = [s.to[0] - fx, s.to[1] - fy];
          }
          if (s.t >= 1) { finish(s); m.step = null; }
        }
      }
      if (m.claim) targets[m.claim] = m.ball;
      if (m.carrier) targets[m.carrier] = m.pos[m.carrier]; // a carrier only moves on a run
      const step = reduced.matches ? Infinity : (pace * dt) / 1000;
      for (const id of ids) {
        if (s && s.kind === "run" && id === s.player && m.step) continue;
        const before = m.pos[id];
        m.pos[id] = approach(before, targets[id] ?? before, step);
        const moved = [m.pos[id][0] - before[0], m.pos[id][1] - before[1]];
        if (Math.hypot(...moved) > 0.01) m.dir[id] = moved;
      }
      const flying = Boolean(m.step && (FLIGHT.has(m.step.kind) || m.step.flight) && m.step.t > 0);
      if (m.claim && !flying && Math.hypot(m.pos[m.claim][0] - m.ball[0], m.pos[m.claim][1] - m.ball[1]) <= CLAIM_M) {
        m.carrier = m.claim;
        m.claim = null;
        m.heldSince = now;
      }
      if (m.carrier && !flying) {
        // The ball sits just ahead of the carrier's feet, the way he is moving (or facing the goal he attacks).
        const [dx, dy] = m.dir[m.carrier] ?? [teamOf[m.carrier] === home ? 1 : -1, 0];
        const d = Math.hypot(dx, dy) || 1;
        m.ball = [m.pos[m.carrier][0] + (dx / d) * AHEAD_M, m.pos[m.carrier][1] + (dy / d) * AHEAD_M];
      }
      draw();
      requestAnimationFrame(frame);
    };
    requestAnimationFrame(frame);

    // Events arrive in order; a pass waits in `buffer` until the event after it shows how it ended.
    // The replay rate: how much match clock passes per real second, smoothed over the polls.
    const measureRate = (events) => {
      const last = events.at(-1);
      if (!last) return;
      const [clock, at] = [clockMs(last.match_clock) / 1000, performance.now() / 1000];
      if (m.seen && at > m.seen[1] && clock >= m.seen[0]) {
        m.rate = 0.7 * m.rate + 0.3 * Math.min(60, Math.max(1, (clock - m.seen[0]) / (at - m.seen[1])));
      }
      m.seen = [clock, at];
    };
    const take = (events, instantly) => {
      measureRate(events);
      const { steps, pending } = toSteps([...m.buffer, ...events], home);
      m.buffer = pending;
      if (instantly) for (const s of steps) { begin(s); finish(s); }
      else m.queue.push(...steps);
    };
    const poll = async () => {
      try {
        const query = m.next === null ? "" : `?after=${m.next}`;
        const body = await fetch(`/events${query}`).then((r) => (r.ok ? r.json() : null));
        if (body && !m.stopped) {
          if (m.next !== null && body.next < m.next) {
            Object.assign(m, { next: null, buffer: [], queue: [], step: null }); // the replay restarted
          }
          else {
            take(body.events, m.next === null); // a late joiner starts from the latest state, not a long replay
            m.next = body.next;
          }
        }
      } catch {
        // the next poll tries again
      }
      if (!m.stopped) setTimeout(poll, POLL_MS);
    };
    poll();
    return () => { m.stopped = true; };
  }, [home, clubs, refs]);
}

export default function Pitch({ state, highlight }) {
  const ids = state?.score ? Object.keys(state.score) : league.clubs.map((club) => club.id);
  const home = ids[0];
  const clubs = useStable(ids.map((id) => CLUBS[id]).filter(Boolean));
  const refs = useRef({ players: new Map(), ball: null, nets: {} });
  usePitchMotion(home, clubs, refs);
  // The moment picked on the timeline, at its event's coordinates (in the acting team's frame, like the ball).
  const at = highlight?.coordinates;
  const spot = at && toScreen(highlight.team, home, at.x, at.y);
  const start = Object.fromEntries(layout(clubs, home, null).map((p) => [p.id, pct(p.x, p.y)]));

  return (
    <div className="pitch-area">
      <div className="pitch">
        <Markings />
        <span className="net left" ref={(el) => { refs.current.nets.left = el; }} style={pct(-1, MID)} />
        <span className="net right" ref={(el) => { refs.current.nets.right = el; }} style={pct(LENGTH + 1, MID)} />
        {clubs.flatMap((club) => club.starters.map((p) => (
          <span key={p.id} className="player" title={p.name} style={{ ...start[p.id], "--primary": club.colours.primary,
                                                                      "--secondary": club.colours.secondary }}
                ref={(el) => (el ? refs.current.players.set(p.id, el) : refs.current.players.delete(p.id))}>
            {p.number ?? p.position}
          </span>
        )))}
        <span className="ball" ref={(el) => { refs.current.ball = el; }} style={pct(LENGTH / 2, MID)} />
        {spot && <span className="spot-highlight" style={pct(...spot)} />}
      </div>
    </div>
  );
}

/** The same array object while its contents are the same, so the engine is not restarted on every /state poll. */
function useStable(list) {
  const ref = useRef(list);
  if (list.length !== ref.current.length || list.some((v, i) => v !== ref.current[i])) ref.current = list;
  return ref.current;
}
