// Run with `npm test`.
import assert from "node:assert/strict";
import test from "node:test";
import { dueOverlays, splitScores } from "./live.js";

test("scores are split out so they can render left-to-right; other text and decimals are left alone", () => {
  assert.deepEqual(splitScores("Calderbrook متقدما 1-0"), [["Calderbrook متقدما ", false], ["1-0", true]]);
  assert.deepEqual(splitScores("ASH 2-3 CAL بعد 9.2 م"), [["ASH ", false], ["2-3", true], [" CAL بعد 9.2 م", false]]);
  assert.deepEqual(splitScores("no numbers"), [["no numbers", false]]);
});

test("the lower-third skips overlays over a match minute behind the clock, except goals, and shows the top priority", () => {
  const overlays = new Map(Object.entries({
    oldSprint: { show_at_ms: 100_000, priority: 2 },
    oldGoal: { show_at_ms: 110_000, priority: 5 },
    recentChance: { show_at_ms: 200_000, priority: 3 },
    recentSprint: { show_at_ms: 210_000, priority: 2 },
    future: { show_at_ms: 300_000, priority: 4 },
  }));
  const queue = [...overlays.keys()];
  assert.deepEqual(dueOverlays(queue, overlays, 220_000, 1),
    { show: ["oldGoal"], stale: ["oldSprint"], keep: ["recentChance", "recentSprint", "future"] });
  // While the slot is busy nothing is shown, but stale overlays are still dropped.
  assert.deepEqual(dueOverlays(queue, overlays, 220_000, 0),
    { show: [], stale: ["oldSprint"], keep: ["oldGoal", "recentChance", "recentSprint", "future"] });
});

test("an overlay that reached the browser late is not stale until it has waited a match minute itself", () => {
  const overlays = new Map([["halfTime", { show_at_ms: 2_700_000, priority: 3 }]]);
  const arrivedMs = new Map([["halfTime", 2_770_000]]);
  assert.deepEqual(dueOverlays(["halfTime"], overlays, 2_775_000, 1, arrivedMs), { show: ["halfTime"], stale: [], keep: [] });
  assert.deepEqual(dueOverlays(["halfTime"], overlays, 2_840_000, 0, arrivedMs), { show: [], stale: ["halfTime"], keep: [] });
});
