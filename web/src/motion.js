// Pitch motion rules: turn the event sequence into ball and player movements, in metres on screen (the home side
// attacks to the right). Pure functions, so how passes, interceptions, runs and shots play out is testable.
import { LENGTH, WIDTH, toScreen } from "./pitchLayout.js";

const GOAL_HALF = 3.66; // half the goal's width
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const dist = ([x1, y1], [x2, y2]) => Math.hypot(x2 - x1, y2 - y1);
const where = (event, home, xy = event.coordinates) => toScreen(event.team, home, xy.x, xy.y);

/** How long the ball takes over a distance: ease-out over about 300-800 ms. */
export const ballMs = (from, to) => Math.round(clamp(300 + dist(from, to) * 14, 300, 800));

/** A 5-minute tracking summary is a distance report, not a run on the pitch: it ends where it starts. */
export const isSummary = (e) =>
  e.event_type === "player_run" && e.payload.end.x === e.coordinates.x && e.payload.end.y === e.coordinates.y;

/** What a pass does, from the pass and the event after it (null while that event has not arrived yet).
 *
 * A completed pass (or, without the flag, one followed by a team-mate's event) travels to the team-mate.
 * Otherwise, when an opponent acts next (possession_change, tackle or pressure), the ball heads for the pass's
 * target and the opponent cuts it out where that event happened, then carries it from there.
 */
export function derivePass(pass, next, home) {
  const from = where(pass, home);
  const target = where(pass, home, pass.payload.end);
  const byOpponent = next && next.player && next.team !== pass.team;
  const completed = pass.payload.completed ?? (next ? next.team === pass.team : true);
  if (completed) {
    const receiver = pass.payload.recipient?.id ?? next?.player?.id;
    return { kind: "pass", passer: pass.player.id, receiver, from, to: target, ms: ballMs(from, target) };
  }
  if (byOpponent && ["possession_change", "tackle", "pressure_event"].includes(next.event_type)) {
    const cut = where(next, home);
    return { kind: "intercept", passer: pass.player.id, by: next.player.id, team: next.team, from, toward: target,
             to: cut, ms: ballMs(from, cut), consumes: next.event_type === "possession_change" };
  }
  return { kind: "loose", passer: pass.player.id, from, to: target, ms: ballMs(from, target) };
}

/** Where a shot ends: the goalmouth for goals and saves, wide of a post when off target, partway when blocked. */
export function shotEnd(shot, home) {
  const nudge = (Number(shot.event_id.slice(-2)) % 7) - 3; // a stable spread, so replays look the same
  const { outcome } = shot.payload;
  const [x, y] = [shot.coordinates.x, shot.coordinates.y];
  let end;
  if (outcome === "goal") end = { x: LENGTH, y: WIDTH / 2 + nudge * 0.8 };
  else if (outcome === "saved") end = { x: LENGTH - 0.6, y: WIDTH / 2 + nudge * 0.5 };
  else if (outcome === "off_target") {
    end = { x: LENGTH, y: WIDTH / 2 + Math.sign(nudge || 1) * (GOAL_HALF + 1.5 + Math.abs(nudge)) };
  }
  else end = { x: x + (LENGTH - x) * 0.5, y: y + (WIDTH / 2 - y) * 0.5 }; // blocked on the way
  return where(shot, home, end);
}

/** Turn events into animation steps, in order. A pass that needs its next event to show how it ended (a failed
 *  pass, or one without the completed flag) is held back in `pending` until that event arrives; a completed pass
 *  with its recipient plays at once. */
export function toSteps(events, home) {
  const steps = [];
  for (let i = 0; i < events.length; i += 1) {
    const e = events[i];
    const next = events[i + 1] ?? null;
    const p = e.payload;
    if (e.event_type === "match_metadata") {
      if (p.status === "kickoff" || p.status === "second_half") steps.push({ kind: "kickoff", ms: 0 });
    } else if (e.event_type === "pass") {
      const known = p.completed === true && p.recipient;
      if (!next && !known) return { steps, pending: events.slice(i) };
      const step = derivePass(e, next, home);
      steps.push(step);
      if (step.consumes) i += 1; // the interception's possession_change is this step's ending
    } else if (e.event_type === "possession_change") {
      const at = p.reason === "kickoff" || p.reason === "goal" ? [LENGTH / 2, WIDTH / 2] : where(e, home);
      steps.push({ kind: "take", player: e.player.id, team: e.team, to: at, ms: 0 });
    } else if (e.event_type === "player_run" && !isSummary(e)) {
      const from = where(e, home), to = where(e, home, p.end);
      const ms = Math.round(clamp((p.distance_m / Math.max(p.speed_kmh / 3.6, 1)) * 1000, 400, 1800));
      steps.push({ kind: "run", player: e.player.id, withBall: p.with_ball, from, to, ms });
    } else if (e.event_type === "shot") {
      const from = where(e, home), to = shotEnd(e, home);
      steps.push({ kind: "shot", player: e.player.id, from, to, goal: p.outcome === "goal",
                   side: e.team === home ? "right" : "left",
                   ms: Math.round(clamp(250 + dist(from, to) * 9, 300, 700)) });
    } else if (e.event_type === "tackle" || e.event_type === "pressure_event") {
      steps.push({ kind: "duel", player: e.player.id, to: where(e, home), ms: 250 });
    }
  }
  return { steps, pending: [] };
}

/** The ball's position on a tween at progress t (0-1): ease-out along the line, lifted on a slight arc. */
export function ballAt(from, to, t, lift = 0.08) {
  const e = 1 - (1 - t) ** 3;
  const [x, y] = [from[0] + (to[0] - from[0]) * e, from[1] + (to[1] - from[1]) * e];
  return [x, y - Math.sin(Math.PI * t) * dist(from, to) * lift]; // drawn a little "up" the screen mid-flight
}

/** Move a point toward a target by at most `maxStep` metres, never past it. */
export function approach([x, y], [tx, ty], maxStep) {
  const d = Math.hypot(tx - x, ty - y);
  if (d <= maxStep || d === 0) return [tx, ty];
  return [x + ((tx - x) / d) * maxStep, y + ((ty - y) / d) * maxStep];
}

/** How fast to play the queue: normal under 2 s of animation behind, up to 4x faster after, and skip past 8 s. */
export function catchUp(pendingMs) {
  if (pendingMs > 8000) return { scale: 4, skip: true };
  return { scale: pendingMs > 2000 ? Math.min(4, pendingMs / 1000) : 1, skip: false };
}
