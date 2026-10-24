// Run with `npm test`.
import assert from "node:assert/strict";
import test from "node:test";
import { nextClip } from "./commentary.js";

test("commentary skips clips queued more than 15 s ago and plays the rest in order", () => {
  const queue = [{ key: "a", queuedAt: 0 }, { key: "b", queuedAt: 10_000 }, { key: "c", queuedAt: 20_000 }];
  assert.deepEqual(nextClip(queue, 24_000), { clip: queue[1], rest: [queue[2]] }); // "a" is 24 s old: skipped
  assert.deepEqual(nextClip(queue, 25_000), { clip: queue[1], rest: [queue[2]] }); // "b" at exactly 15 s still plays
  assert.deepEqual(nextClip(queue, 40_000), { clip: undefined, rest: [] });
});
