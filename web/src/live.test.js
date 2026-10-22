// Run with `npm test`.
import assert from "node:assert/strict";
import test from "node:test";
import { splitScores } from "./live.js";

test("scores are split out so they can render left-to-right; other text and decimals are left alone", () => {
  assert.deepEqual(splitScores("Calderbrook متقدما 1-0"), [["Calderbrook متقدما ", false], ["1-0", true]]);
  assert.deepEqual(splitScores("ASH 2-3 CAL بعد 9.2 م"), [["ASH ", false], ["2-3", true], [" CAL بعد 9.2 م", false]]);
  assert.deepEqual(splitScores("no numbers"), [["no numbers", false]]);
});
