# ADR 0005: Tauri Desktop Architecture

## Status
Accepted (scaffolded; desktop packaging not verified by the 2026-09-10 audit)

## Context
The product should feel like one application, not "start two terminals".
The frontend is web-tech (React/Vite); a thin native shell is preferred over
Electron for footprint and security surface.

## Decision
Tauri 2 shell in `frontend/src-tauri`:
- spawns `gg-backend` (expected on PATH) at startup
- probes `GET /api/health`; still loads the window after a failed health wait
- kills its child handle on window destroy; no cross-process single-owner guard
- CSP restricts connect-src to the local backend

Development flow stays `make dev` (no Rust needed). Packaging requires rustup +
`@tauri-apps/cli`.

## Consequences
- Users get a single desktop app once built; developers never need Rust for
  day-to-day work.
- The backend remains independently usable (API-first), so a pure-web deployment
  is possible by hosting `frontend/dist` behind the same origin.
- Desktop packaging/ownership requires separate verification; backend configuration,
  migrations and cwd-relative state resolution currently assume checkout-like layout.
