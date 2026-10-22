from __future__ import annotations

from fastapi import Request, WebSocket

SESSION_HEADER = "x-semantic-home-session"
DEFAULT_SESSION_ID = "global"


def _normalize_session_id(value: str | None) -> str:
    if value is None:
        return DEFAULT_SESSION_ID
    stripped = value.strip()
    return stripped or DEFAULT_SESSION_ID


def get_request_session_id(request: Request) -> str:
    home = getattr(request.state, "home", None)
    if not home:
        from fastapi import HTTPException
        raise HTTPException(401, "An authenticated household is required")
    return home["id"] + ":" + _normalize_session_id(request.headers.get(SESSION_HEADER))[:128]


def get_websocket_session_id(ws: WebSocket) -> str:
    return _normalize_session_id(ws.headers.get(SESSION_HEADER))
