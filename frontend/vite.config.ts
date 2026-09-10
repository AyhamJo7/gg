/// <reference types="vitest/config" />
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Embed the backend's bearer token at dev-server-start/build time so the
// browser page can authenticate mutating API calls without a network route
// to fetch it (see backend/src/orchestrator/api/auth.py — an always-open
// GET would hand the secret to any local process). `make dev`/`make build`
// run `gg-backend --init-token-only` first to guarantee the file exists;
// missing here just means an unauthenticated build (e.g. a plain `vitest`
// run, or a checkout that hasn't started the backend yet) — never throw.
function readAuthToken(warnIfMissing: boolean): string {
  try {
    return readFileSync(resolve(__dirname, "../backend/.orchestrator/auth_token"), "utf-8").trim();
  } catch {
    if (warnIfMissing) {
      // Bakes in as "" otherwise, silently 401-ing every mutating call for
      // the whole dev-server session with no clue why — e.g. running
      // `npm run dev` directly instead of `make dev`/`make frontend`, which
      // run `gg-backend --init-token-only` first to guarantee this exists.
      console.warn(
        "[vite] backend/.orchestrator/auth_token not found — VITE_AUTH_TOKEN will be empty and every " +
          "mutating API call will 401. Run `make token` (or `make dev`/`make frontend`) first.",
      );
    }
    return "";
  }
}

export default defineConfig(() => ({
  plugins: [react()],
  define: {
    "import.meta.env.VITE_AUTH_TOKEN": JSON.stringify(readAuthToken(!process.env.VITEST)),
  },
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8787",
      "/ws": { target: "ws://127.0.0.1:8787", ws: true },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: "./src/test/setup.ts",
  },
}));
