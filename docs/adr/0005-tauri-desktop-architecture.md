# ADR 0005: Tauri Desktop Architecture

## Status
Accepted (scaffolded; build UNVERIFIED — no Rust toolchain on this machine)

## Context
The product should feel like one application, not "start two terminals".
The frontend is web-tech (React/Vite); a thin native shell is preferred over
Electron for footprint and security surface.

## Decision
Tauri 2 shell in `frontend/src-tauri`:
- spawns `gg-backend` (expected on PATH) at startup
- polls `GET /api/health` before showing the window
- kills the backend child on window destroy (single-instance ownership)
- CSP restricts connect-src to the local backend

Development flow stays `make dev` (no Rust needed). Packaging requires rustup +
`@tauri-apps/cli`.

## Consequences
- Users get a single desktop app once built; developers never need Rust for
  day-to-day work.
- The backend remains independently usable (API-first), so a pure-web deployment
  is possible by hosting `frontend/dist` behind the same origin.
- Desktop build verification is pending a Rust toolchain — tracked as a known
  limitation, not silently assumed.
