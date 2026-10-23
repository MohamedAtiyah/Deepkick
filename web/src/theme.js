// The viewer's theme: "system", "light" or "dark", saved in localStorage when the browser allows it.
import { useEffect, useState } from "react";

export const THEMES = ["system", "light", "dark"];
const KEY = "deepkick-theme"; // index.html reads the same key before first paint

/** The saved choice, or "system" when nothing valid is saved or storage throws (private window, blocked site data). */
export function loadTheme(storage) {
  try {
    const saved = (storage ?? localStorage).getItem(KEY);
    return THEMES.includes(saved) ? saved : "system";
  } catch {
    return "system";
  }
}

/** Save the choice; if storage throws, it simply lasts until the page is closed. */
export function saveTheme(theme, storage) {
  try {
    (storage ?? localStorage).setItem(KEY, theme);
  } catch {
    // nothing to do: the page works without storage
  }
}

/** The theme choice, applied as data-theme on <html> ("system" removes it so the OS setting decides). */
export function useTheme() {
  const [theme, setTheme] = useState(() => loadTheme());
  useEffect(() => {
    if (theme === "system") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = theme;
    saveTheme(theme);
  }, [theme]);
  return [theme, setTheme];
}
