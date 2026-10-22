import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const api = "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    fs: { allow: [".."] }, // the squads come from ../data/league.json
    proxy: {
      "/state": api,
      "/recap": api,
      "/healthz": api,
      "/ws": { target: "ws://localhost:8000", ws: true },
    },
  },
});
