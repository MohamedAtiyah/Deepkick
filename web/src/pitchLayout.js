// Where to draw each player, in metres on the data's 105 x 68 pitch, with the home side attacking to the right.
// Pure functions, so the rules (slots, a small shared shift, keepers on their line, no overlaps) are testable.

export const LENGTH = 105;
export const WIDTH = 68;
const MAX_SHIFT = { x: 6, y: 3 }; // the most a whole outfield shape slides toward the ball
const GK_MAX = 2; // keepers stay on their line
const SHIFT_RATE = { x: 0.2, y: 0.15 }; // metres of shift per metre the ball is from the centre spot
const ROLE_WEIGHT = { defender: 0.5, midfielder: 0.75, forward: 1 };
export const MIN_GAP = 3; // markers closer than this are nudged apart

const clamp = (value, limit) => Math.max(-limit, Math.min(limit, value));

/** "GK", "defender", "midfielder" or "forward" from a position code such as RCB, CDM or LST. */
export function role(position) {
  if (position === "GK") return "GK";
  if (position.endsWith("B")) return "defender";
  if (position.endsWith("M")) return "midfielder";
  return "forward";
}

/** A point in a team's attacking frame (towards x=105) seen on screen, where the home side attacks to the right. */
export function toScreen(team, home, x, y) {
  return team === home ? [x, y] : [LENGTH - x, WIDTH - y];
}

/** The small shift both teams share toward the ball: proportional to its distance from the centre spot, capped. */
export function teamShift(ballX, ballY) {
  return {
    x: clamp((ballX - LENGTH / 2) * SHIFT_RATE.x, MAX_SHIFT.x),
    y: clamp((ballY - WIDTH / 2) * SHIFT_RATE.y, MAX_SHIFT.y),
  };
}

/** Every starter of both clubs at their formation slot, shifted as a unit toward the ball; the holder is on the ball. */
export function layout(clubs, home, ball, gap = MIN_GAP) {
  const ballXY = ball ? toScreen(ball.team, home, ball.x, ball.y) : null;
  const shift = ballXY ? teamShift(...ballXY) : { x: 0, y: 0 };
  const points = clubs.flatMap((club) =>
    club.starters.map((player) => {
      const slot = club.formation.slots[player.position];
      const [sx, sy] = toScreen(club.id, home, slot.x, slot.y);
      const kind = role(player.position);
      const onBall = Boolean(ball) && player.id === ball.player_id;
      let [x, y] = [sx, sy];
      if (onBall) [x, y] = ballXY;
      else if (kind === "GK") [x, y] = [sx + clamp(shift.x, GK_MAX), sy + clamp(shift.y, GK_MAX)];
      else [x, y] = [sx + shift.x * ROLE_WEIGHT[kind], sy + shift.y * ROLE_WEIGHT[kind]];
      return { ...player, club, x, y, onBall };
    }),
  );
  return separate(points, gap);
}

/** Nudge markers that land within `gap` metres apart; the player on the ball never moves. Stays on the pitch. */
export function separate(points, gap = MIN_GAP, passes = 4) {
  const out = points.map((p) => ({ ...p }));
  for (let pass = 0; pass < passes; pass += 1) {
    for (let i = 0; i < out.length; i += 1) {
      for (let j = i + 1; j < out.length; j += 1) {
        const a = out[i], b = out[j];
        let dx = b.x - a.x, dy = b.y - a.y;
        const dist = Math.hypot(dx, dy);
        if (dist >= gap) continue;
        if (dist === 0) [dx, dy] = [0, 1]; // stacked exactly: split them vertically
        const push = (gap - (dist || 0)) / (dist || 1);
        const [shareA, shareB] = a.onBall ? [0, 1] : b.onBall ? [1, 0] : [0.5, 0.5];
        a.x -= dx * push * shareA; a.y -= dy * push * shareA;
        b.x += dx * push * shareB; b.y += dy * push * shareB;
      }
    }
  }
  for (const p of out) {
    p.x = Math.max(0, Math.min(LENGTH, p.x));
    p.y = Math.max(0, Math.min(WIDTH, p.y));
  }
  return out;
}
