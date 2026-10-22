from __future__ import annotations

import hashlib
import hmac
import secrets
from contextvars import ContextVar

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

home_context: ContextVar[str | None] = ContextVar("home_id", default=None)


def password_hash(password):
    salt = secrets.token_hex(16)
    value = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()
    return f"{salt}:{value}"


def password_matches(password, encoded):
    salt, expected = encoded.split(":")
    actual = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()
    return hmac.compare_digest(actual, expected)


class HouseholdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        from backend.settings.config import settings
        prefix = settings.api_prefix or ""
        path = request.url.path.removeprefix(prefix)
        if not request.url.path.startswith(prefix) or path in {"/health", "/openapi.json", "/scalar", "/docs", "/redoc"}:
            return await call_next(request)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            own_origins = {f"http://{request.headers.get('host')}", f"https://{request.headers.get('host')}"}
            allowed = own_origins | set(settings.allowed_origins.split(","))
            if origin and origin not in allowed:
                return JSONResponse({"detail": "Origin not allowed"}, status_code=403)
        if path in {"/auth/register", "/auth/login", "/auth/logout", "/auth/me"}:
            return await call_next(request)
        user = request.app.state.db.user(request.cookies.get("semantic_session", ""))
        if not user:
            return JSONResponse({"detail": "Please sign in"}, status_code=401)
        request.state.user = user
        homes = request.app.state.db.homes(user["id"])
        selected = request.headers.get("x-semantic-home-household")
        home = next((h for h in homes if h["id"] == selected), None) if selected else (homes[0] if homes else None)
        if selected and not home:
            return JSONResponse({"detail": "Unknown household"}, status_code=404)
        if not home and path != "/homes":
            return JSONResponse({"detail": "Create a household first"}, status_code=409)
        if home and home["role"] == "viewer" and request.method not in {"GET", "HEAD", "OPTIONS"} and path != "/homes":
            return JSONResponse({"detail": "Read-only household membership"}, status_code=403)
        request.state.home = home
        token = home_context.set(home["id"] if home else None)
        try:
            return await call_next(request)
        finally:
            home_context.reset(token)
