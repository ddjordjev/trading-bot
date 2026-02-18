from __future__ import annotations

from fastapi import Depends, HTTPException, WebSocket, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from config.settings import get_settings

_bearer = HTTPBearer(auto_error=False)


async def verify_token(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    token = get_settings().dashboard_token
    if not token:
        return "no-auth"
    if creds is None or creds.credentials != token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    return creds.credentials


async def verify_ws_token(websocket: WebSocket) -> bool:
    token = get_settings().dashboard_token
    if not token:
        return True
    qs_token = websocket.query_params.get("token", "")
    if qs_token == token:
        return True
    await websocket.close(code=4001, reason="Unauthorized")
    return False
