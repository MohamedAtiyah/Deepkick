// Run with `npm test`.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { approach, ballAt, ballMs, catchUp, derivePass, isSummary, shotEnd, toSteps } from "./motion.js";

const HOME = "ASH";
const ev = (event_type, team, player, x, y, payload = {}, event_id = "E-00010") =>
  ({ event_id, event_type, team, player: player && { id: player }, coordinates: { x, y }, payload });
const pass = (completed, payload = {}) =>
  ev("pass", "ASH", "ASH04", 40, 30, { end: { x: 60, y: 20 }, completed, recipient: { id: "ASH07" }, ...payload });

test("a completed pass travels to the team-mate at the pass's end", () => {
  const step = derivePass(pass(true), ev("player_run", "ASH", "ASH07", 60, 20, {}), HOME);
  assert.deepEqual(step, { kind: "pass", passer: "ASH04", receiver: "ASH07", from: [40, 30], to: [60, 20],
                           ms: ballMs([40, 30], [60, 20]) });
});

test("without the completed flag, a team-mate acting next means the ball reached them", () => {
  const next = ev("pass", "ASH", "ASH09", 60, 20, { end: { x: 70, y: 25 } });
  const step = derivePass(pass(undefined, { recipient: undefined }), next, HOME);
  assert.deepEqual([step.kind, step.receiver], ["pass", "ASH09"]);
});

test("an incomplete pass is cut out by the opponent where the possession_change happens", () => {
  // The away side's event is in its own frame: (45, 48) there is (60, 20) on screen.
  const won = ev("possession_change", "CAL", "CAL06", 45, 48, { reason: "interception" });
  const step = derivePass(pass(false), won, HOME);
  assert.deepEqual([step.kind, step.by, step.toward, step.to, step.consumes],
                   ["intercept", "CAL06", [60, 20], [60, 20], true]);
});

test("an opponent's tackle or pressure after a failed pass is the cut-out point too", () => {
  for (const kind of ["tackle", "pressure_event"]) {
    const step = derivePass(pass(false), ev(kind, "CAL", "CAL05", 50, 40, {}), HOME);
    assert.deepEqual([step.kind, step.by, step.to, step.consumes], ["intercept", "CAL05", [55, 28], false]);
  }
});

test("a failed pass with no opponent known yet runs loose toward its target", () => {
  assert.equal(derivePass(pass(false), ev("shot", "ASH", "ASH09", 90, 30, { outcome: "saved" }), HOME).kind, "loose");
});

test("a completed pass with its recipient plays at once; it does not wait for the next event", () => {
  const { steps, pending } = toSteps([pass(true)], HOME);
  assert.deepEqual([steps.map((s) => s.kind), pending], [["pass"], []]);
});

test("toSteps holds a failed pass back until its next event arrives, and the interception swallows it", () => {
  const first = toSteps([ev("match_metadata", "ASH", null, 0, 0, { status: "kickoff" }), pass(false)], HOME);
  assert.deepEqual(first.steps.map((s) => s.kind), ["kickoff"]);
  assert.equal(first.pending.length, 1);
  const won = ev("possession_change", "CAL", "CAL06", 45, 48, { reason: "interception" });
  const run = ev("player_run", "CAL", "CAL06", 45, 48, { end: { x: 55, y: 50 }, distance_m: 10.2, speed_kmh: 24,
                                                       with_ball: true });
  const rest = toSteps([...first.pending, won, run], HOME);
  assert.deepEqual(rest.steps.map((s) => s.kind), ["intercept", "run"]);
  assert.deepEqual(rest.steps[1].to, [50, 18]); // the away side's run, mirrored onto the screen
  assert.equal(rest.steps[1].ms, Math.round((10.2 / (24 / 3.6)) * 1000)); // scaled by the run's speed
});

test("tracking summaries are not runs, and goals restart from the centre spot", () => {
  const summary = ev("player_run", "ASH", "ASH02", 30, 60, { end: { x: 30, y: 60 }, distance_m: 450, speed_kmh: 5 });
  assert.equal(isSummary(summary), true);
  const goal = ev("possession_change", "CAL", "CAL09", 52.5, 34, { reason: "goal" });
  const { steps } = toSteps([summary, goal], HOME);
  assert.deepEqual(steps, [{ kind: "take", player: "CAL09", team: "CAL", to: [52.5, 34], ms: 0 }]);
});

test("shots fly to the goalmouth on the right side; goals mark the net, blocks stop short", () => {
  const shot = (team, outcome) => ev("shot", team, "X", 90, 30, { outcome }, "E-00123");
  const home = toSteps([shot("ASH", "goal")], HOME).steps[0];
  assert.deepEqual([home.goal, home.side], [true, "right"]);
  assert.ok(Math.abs(home.to[1] - 34) <= 3.66 && home.to[0] === 105);
  const away = toSteps([shot("CAL", "off_target")], HOME).steps[0];
  assert.ok(away.side === "left" && away.to[0] === 0 && Math.abs(away.to[1] - 34) > 3.66);
  assert.ok(shotEnd(shot("ASH", "blocked"), HOME)[0] < 100);
});

test("the ball eases out along a slight arc and lands exactly on target; players never pass their target", () => {
  assert.deepEqual(ballAt([0, 0], [30, 0], 0), [0, 0]);
  assert.deepEqual(ballAt([0, 0], [30, 0], 1).map((v) => Math.round(v * 1e6) / 1e6 + 0), [30, 0]);
  const mid = ballAt([0, 0], [30, 0], 0.5);
  assert.ok(mid[0] > 15 && mid[1] < 0); // ease-out runs ahead of linear; the arc lifts it
  assert.deepEqual(approach([0, 0], [10, 0], 3), [3, 0]);
  assert.deepEqual(approach([0, 0], [1, 0], 3), [1, 0]);
  assert.ok(ballMs([0, 0], [0, 0]) === 300 && ballMs([0, 0], [100, 0]) === 800);
});

test("the queue speeds up past 2 s behind and skips past 8 s", () => {
  assert.deepEqual(catchUp(500), { scale: 1, skip: false });
  assert.deepEqual(catchUp(3000), { scale: 3, skip: false });
  assert.deepEqual(catchUp(9000), { scale: 4, skip: true });
});

test("every event of the seed-42 replay turns into steps, with no step left pending at full time", () => {
  const lines = readFileSync(new URL("../../tests/fixtures/replay_ASH-CAL-42.jsonl", import.meta.url), "utf8");
  const events = lines.trim().split("\n").map((line) => JSON.parse(line));
  const { steps, pending } = toSteps(events, HOME);
  assert.equal(pending.length, 0);
  const kinds = steps.reduce((n, s) => ({ ...n, [s.kind]: (n[s.kind] ?? 0) + 1 }), {});
  assert.equal(kinds.shot, 32);
  assert.ok(kinds.pass > 500 && kinds.intercept > 50 && kinds.run > 50);
  for (const s of steps) {
    for (const [x, y] of [s.from, s.to].filter(Boolean)) assert.ok(x >= 0 && x <= 105 && y >= -1 && y <= 69);
  }
});
