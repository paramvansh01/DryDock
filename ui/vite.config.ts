import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

const api = "http://127.0.0.1:8765";

// The illustrative fixture lives in public/ and is served ONLY by the dev
// server. Production builds (what the orchestrator serves, what a demo is filmed from) do not
// contain it: publicDir is disabled for `vite build`, and App.tsx hides the fixture option.
export default defineConfig(({ command }) => ({
  plugins: [react(), tailwindcss()],
  publicDir: command === "build" ? false : "public",
  server: {
    port: 5173,
    proxy: {
      "/ws": { target: api.replace("http", "ws"), ws: true },
      "/runs": api, "/merges": api, "/branches": api, "/precedents": api, "/pairs": api,
      "/health": api, "/replays": api, "/db": api, "/system": api,
    },
  },
  test: { environment: "node" },
}));
