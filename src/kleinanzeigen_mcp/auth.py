"""API token auth + per-token rate limiting for the HTTP server (REST and MCP)."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections import deque
from dataclasses import dataclass

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

PUBLIC_PATHS = {"/health", "/"}


@dataclass(frozen=True, slots=True)
class Token:
    name: str
    sha256: bytes


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def generate_token() -> str:
    return "ka_" + secrets.token_urlsafe(32)


def parse_tokens(spec: str) -> list[Token]:
    """``tok`` | ``name:tok`` | ``name:sha256:<hex>`` entries, comma separated."""
    out: list[Token] = []
    for i, raw in enumerate(x.strip() for x in (spec or "").split(",")):
        if not raw:
            continue
        parts = raw.split(":")
        if len(parts) == 3 and parts[1] == "sha256":
            out.append(Token(parts[0], bytes.fromhex(parts[2])))
        elif len(parts) >= 2:
            name, tok = parts[0], ":".join(parts[1:])
            out.append(Token(name, hashlib.sha256(tok.encode()).digest()))
        else:
            out.append(Token(f"token{i + 1}", hashlib.sha256(raw.encode()).digest()))
    return out


class _Window:
    __slots__ = ("hits",)

    def __init__(self) -> None:
        self.hits: deque[float] = deque()


class AuthMiddleware:
    """Pure ASGI middleware (keeps streaming MCP responses intact)."""

    def __init__(self, app: ASGIApp, tokens: list[Token], per_minute: int) -> None:
        self.app = app
        self.tokens = tokens
        self.per_minute = per_minute
        self._windows: dict[str, _Window] = {}

    def _check(self, presented: str | None) -> Token | None:
        if not presented:
            return None
        digest = hashlib.sha256(presented.encode()).digest()
        match = None
        for t in self.tokens:  # constant-time compare against every token
            if hmac.compare_digest(digest, t.sha256):
                match = t
        return match

    def _rate_ok(self, name: str) -> tuple[bool, float]:
        if self.per_minute <= 0:
            return True, 0
        w = self._windows.setdefault(name, _Window())
        now = time.monotonic()
        while w.hits and now - w.hits[0] > 60:
            w.hits.popleft()
        if len(w.hits) >= self.per_minute:
            return False, 60 - (now - w.hits[0])
        w.hits.append(now)
        return True, 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in PUBLIC_PATHS or scope["method"] == "OPTIONS":
            await self.app(scope, receive, send)
            return

        token: Token | None = None
        if self.tokens:
            headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
            presented = None
            auth = headers.get("authorization", "")
            if auth.lower().startswith("bearer "):
                presented = auth[7:].strip()
            presented = presented or headers.get("x-api-key")
            token = self._check(presented)
            if token is None:
                resp = JSONResponse(
                    {"error": "unauthorized", "detail": "send 'Authorization: Bearer <token>'"},
                    status_code=401,
                    headers={"WWW-Authenticate": 'Bearer realm="kleinanzeigen-mcp"'},
                )
                await resp(scope, receive, send)
                return

        ok, retry = self._rate_ok(token.name if token else "anonymous")
        if not ok:
            resp = JSONResponse(
                {"error": "rate_limited", "detail": f"max {self.per_minute} requests/min per token"},
                status_code=429,
                headers={"Retry-After": str(int(retry) + 1)},
            )
            await resp(scope, receive, send)
            return

        scope.setdefault("state", {})["token_name"] = token.name if token else "anonymous"
        await self.app(scope, receive, send)
