// Run with `npm test` (Node's built-in test runner; no extra dependency).
import assert from "node:assert/strict";
import test from "node:test";
import league from "../../data/league.json" with { type: "json" };
import { LENGTH, MIN_GAP, WIDTH, layout, role, separate, teamShift, toScreen } from "./pitchLayout.js";

const [home, away] = league.clubs;
const byId = (points) => Object.fromEntries(points.map((p) => [p.id, p]));
const slot = (club, player) => toScreen(club.id, home.id, ...Object.values(club.formation.slots[player.position]));

test("roles come from position codes", () => {
  assert.deepEqual(["GK", "RCB", "LB", "CDM", "LM", "RW", "ST", "LST"].map(role),
    ["GK", "defender", "defender", "midfielder", "midfielder", "forward", "forward", "forward"]);
});

test("with no ball every player sits on their formation slot, away side mirrored", () => {
  const points = byId(layout([home, away], home.id, null));
  const gk = away.starters.find((p) => p.position === "GK");
  assert.deepEqual([points[gk.id].x, points[gk.id].y], [LENGTH - 5, WIDTH - 34]);
  for (const club of [home, away]) {
    for (const p of club.starters) {
      const [x, y] = slot(club, p);
      assert.ok(Math.hypot(points[p.id].x - x, points[p.id].y - y) < MIN_GAP, p.id); // at most a small nudge
    }
  }
});

test("the player on the ball is at the event coordinates, in the right team's frame", () => {
  const holder = away.starters.find((p) => p.position === "ST");
  const points = byId(layout([home, away], home.id, { team: away.id, player_id: holder.id, x: 90, y: 20 }));
  assert.deepEqual([points[holder.id].x, points[holder.id].y], [LENGTH - 90, WIDTH - 20]);
  assert.equal(points[holder.id].onBall, true);
});

test("the shared shift is capped and scaled by role; keepers move at most 2 m", () => {
  assert.deepEqual(teamShift(LENGTH, WIDTH), { x: 6, y: 3 });
  assert.deepEqual(teamShift(LENGTH / 2, WIDTH / 2), { x: 0, y: 0 });
  const ball = { team: home.id, player_id: "nobody", x: LENGTH, y: WIDTH }; // far corner: the biggest shift
  const points = byId(layout([home, away], home.id, ball, 0)); // no nudging, to measure the shift itself
  const cap = Math.hypot(6, 3); // the largest shared shift: 6 m along, 3 m across
  const limit = { GK: Math.hypot(2, 2), defender: 0.5 * cap, midfielder: 0.75 * cap, forward: cap };
  for (const club of [home, away]) {
    for (const p of club.starters) {
      const [x, y] = slot(club, p);
      const moved = Math.hypot(points[p.id].x - x, points[p.id].y - y);
      assert.ok(moved <= limit[role(p.position)] + 1e-9, `${p.id} moved ${moved.toFixed(2)} m`);
    }
  }
  const moved = (position) => {
    const p = home.starters.find((s) => s.position === position);
    const [x] = slot(home, p);
    return byId(layout([home], home.id, ball, 0))[p.id].x - x;
  };
  assert.ok(moved("GK") <= 2 && moved("RCB") < moved("RCM") && moved("RCM") < moved("RST"));
});

test("markers that land on top of each other are nudged at least MIN_GAP apart; the ball holder stays put", () => {
  const points = separate([
    { id: "a", x: 50, y: 30, onBall: true }, { id: "b", x: 50, y: 30, onBall: false },
    { id: "c", x: 51, y: 30.5, onBall: false },
  ]);
  assert.deepEqual([points[0].x, points[0].y], [50, 30]);
  for (let i = 0; i < points.length; i += 1) {
    for (let j = i + 1; j < points.length; j += 1) {
      assert.ok(Math.hypot(points[i].x - points[j].x, points[i].y - points[j].y) >= MIN_GAP - 0.2);
    }
  }
});

test("a real seed-42 style frame keeps every pair of markers apart and on the pitch", () => {
  const points = layout([home, away], home.id, { team: home.id, player_id: home.starters[9].id, x: 75, y: 42 });
  for (const p of points) assert.ok(p.x >= 0 && p.x <= LENGTH && p.y >= 0 && p.y <= WIDTH);
  let closest = Infinity;
  for (let i = 0; i < points.length; i += 1)
    for (let j = i + 1; j < points.length; j += 1)
      closest = Math.min(closest, Math.hypot(points[i].x - points[j].x, points[i].y - points[j].y));
  assert.ok(closest >= MIN_GAP - 0.2, `closest pair ${closest.toFixed(2)} m`);
});
