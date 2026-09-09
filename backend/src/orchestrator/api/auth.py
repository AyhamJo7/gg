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
"""

from __future__ import annotations

import secrets
from pathlib import Path

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

TOKEN_FILENAME = "auth_token"  # noqa: S105 - a filename, not a credential value

#: Mutating requests under these paths are exempt — the token bootstrap route
#: itself (chicken-and-egg) and health.
UNAUTHENTICATED_PATHS = frozenset({"/api/auth/token", "/api/health"})


def load_or_create_token(state_dir: Path) -> str:
    """Return the persistent bearer token, generating one on first run."""
    state_dir.mkdir(parents=True, exist_ok=True)
    token_path = state_dir / TOKEN_FILENAME
    try:
        existing = token_path.read_text().strip()
    except FileNotFoundError:
        existing = ""
    if existing:
        return existing
    token = secrets.token_urlsafe(32)
    token_path.write_text(token)
    token_path.chmod(0o600)
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
        if scope["type"] != "http" or scope["method"] in ("GET", "HEAD", "OPTIONS"):
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
