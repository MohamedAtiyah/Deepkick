// Run with `npm test`.
import assert from "node:assert/strict";
import test from "node:test";
import { loadTheme, saveTheme } from "./theme.js";

const memory = () => {
  const items = new Map();
  return { getItem: (k) => items.get(k) ?? null, setItem: (k, v) => items.set(k, v) };
};
const broken = { getItem() { throw new Error("SecurityError"); }, setItem() { throw new Error("QuotaExceededError"); } };

test("a saved theme is read back; nothing saved or an unknown value means system", () => {
  const storage = memory();
  assert.equal(loadTheme(storage), "system");
  saveTheme("dark", storage);
  assert.equal(loadTheme(storage), "dark");
  storage.setItem("deepkick-theme", "purple");
  assert.equal(loadTheme(storage), "system");
});

test("storage that throws falls back to system and never breaks the page", () => {
  assert.equal(loadTheme(broken), "system");
  assert.doesNotThrow(() => saveTheme("light", broken));
});
