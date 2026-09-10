"""Minimal local-operator auth: a shared-secret bearer token.

GG binds to 127.0.0.1 only (config server.host) and is a single-operator
desktop tool, not a multi-tenant service — a full user/session system would
be the wrong tool. The real gap this closes: the backend port is reachable by
ANY other local process (curl, another app, a compromised dependency), not
just the intended frontend, and several mutating routes (plan revision, gate
resolution, waivers) sit directly upstream of subprocess execution. A token
generated once and persisted next to the database (0600, gitignored) raises
that from "anything on the machine" to "anything that can read this user's
files" — the same trust boundary the rest of GG's local-first design already
assumes.

There is deliberately no network route to fetch this token (an earlier draft
had one; any local process could just curl it, defeating the whole scheme).
The frontend gets it out-of-band instead: the Makefile's `dev`/`backend`
targets generate the token file before the frontend starts, `vite.config.ts`
reads it at dev-server/build time and embeds it as `VITE_AUTH_TOKEN`, and the
Tauri-packaged app reads the same file via Tauri's scoped fs IPC at startup
— neither path is reachable from arbitrary web content the way a GET route
would be.

GET/HEAD requests require the token too, same as mutating ones. An earlier
draft exempted them, reasoning "read-only" was lower risk — that reasoning
broke when the fresh-checkout install step (project_engine._install) gained
a network-enabled sandbox mode (sandbox.run_sandboxed(..., allow_network=True)):
a malicious/compromised dependency pulled in during install runs inside the
host's network namespace (bwrap has no per-destination firewall), can reach
127.0.0.1:<port>, and — with GET exempt — could have read every project's
and mission's data (plans, task logs, delivery reports, waiver history)
unauthenticated. The install step has no path to the real token (it operates
on a fresh git clone into a scratch dir; .orchestrator/ is gitignored and
absent from the clone), so requiring the token uniformly closes this: an
unauthenticated GET now gets 401 regardless of what network access a
sandboxed subprocess happens to have. /api/health stays exempt (see
UNAUTHENTICATED_PATHS) as a minimal liveness probe that reveals nothing
project-specific — scripts poll it to detect the backend is up before the
token file is guaranteed readable.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

TOKEN_FILENAME = "auth_token"  # noqa: S105 - a filename, not a credential value

#: Requests (any method) under these paths are exempt from the token check.
#: /api/health only: a minimal liveness probe with no project-specific data,
#: polled by scripts to detect the backend is up (see ui-dogfood.sh) before
#: the token file is guaranteed readable. There is no token bootstrap route
#: (see docs/token delivery in Makefile/vite.config.ts) — every other route
#: requires the token, GET/HEAD included (see module docstring).
UNAUTHENTICATED_PATHS = frozenset({"/api/health"})


def load_or_create_token(state_dir: Path) -> str:
    """Return the persistent bearer token, generating one on first run.

    Creation is atomic and 0600 from the first syscall (O_CREAT|O_EXCL, mode
    0o600 passed to open() — not write-then-chmod, which leaves a window
    where the file exists at the process umask's default mode, typically
    world/group-readable, and stays that way permanently if the process dies
    between the two calls). If another process wins the race and creates it
    first, O_EXCL fails here and the existing token is read instead — never
    regenerated, so a concurrent first-run never produces two different
    tokens for the same state dir.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    token_path = state_dir / TOKEN_FILENAME
    token = secrets.token_urlsafe(32)
    try:
        fd = os.open(token_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return token_path.read_text().strip()
    try:
        os.write(fd, token.encode("ascii"))
    finally:
        os.close(fd)
    return token


def is_authorized(token: str, header_value: str | None) -> bool:
    if not header_value or not header_value.startswith("Bearer "):
        return False
    return secrets.compare_digest(header_value[len("Bearer ") :], token)


class AuthMiddleware:
    """Pure ASGI middleware (not BaseHTTPMiddleware/@app.middleware("http")).

    BaseHTTPMiddleware runs the downstream app in a separate anyio task per
    request, which changes background-task scheduling around each request
    (observed: it altered mission pause/resume/cancel race timing in tests).
    A plain ASGI callable adds a header check with no such side effect.
    """

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] == "OPTIONS":
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        if not path.startswith("/api/") or path in UNAUTHENTICATED_PATHS:
            await self.app(scope, receive, send)
            return
        headers: dict[bytes, bytes] = dict(scope.get("headers") or [])
        auth_header = headers.get(b"authorization")
        if not is_authorized(self.token, auth_header.decode("latin-1") if auth_header else None):
            response = JSONResponse({"detail": "missing or invalid bearer token"}, status_code=401)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)
