from __future__ import annotations

import asyncio
import ipaddress
import json
import math
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


PAIRING_TTL_SECONDS = 10 * 60
SESSION_TTL_SECONDS = 2 * 60 * 60
MAX_FRAME_BYTES = 1_000_000
WEB_ROOT = Path(__file__).resolve().parent.parent / "web"


@dataclass
class Session:
    code: str
    host_token: str
    created_at: float = field(default_factory=time.monotonic)
    host_ws: WebSocket | None = None
    viewer_ws: WebSocket | None = None
    viewer_token: str | None = None
    viewer_id: str | None = None
    approved: bool = False
    screen_allowed: bool = False
    control_allowed: bool = False


class ConnectRequest(BaseModel):
    code: str = Field(pattern=r"^\d{8}$")


class RateLimiter:
    def __init__(self) -> None:
        self._events: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str, limit: int, window: int) -> bool:
        now = time.monotonic()
        if len(self._events) >= 10_000 and key not in self._events:
            for existing_key, events in tuple(self._events.items()):
                while events and now - events[0] >= 60:
                    events.popleft()
                if not events:
                    self._events.pop(existing_key, None)
            if len(self._events) >= 10_000:
                return False
        events = self._events[key]
        while events and now - events[0] >= window:
            events.popleft()
        if len(events) >= limit:
            return False
        events.append(now)
        return True


sessions: dict[str, Session] = {}
rate_limiter = RateLimiter()
state_lock = asyncio.Lock()
app = FastAPI(title="PC-Plus Remote Desktop", docs_url=None, redoc_url=None)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    if request.url.scheme != "https" and not _is_local_host(request.url.hostname):
        response = RedirectResponse(request.url.replace(scheme="https"), status_code=307)
    else:
        response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' blob:; connect-src 'self' ws: wss:; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    )
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _is_local_host(hostname: str | None) -> bool:
    if hostname is None:
        return False
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _prune_expired() -> None:
    now = time.monotonic()
    expired = [
        code
        for code, session in sessions.items()
        if session.host_ws is None
        and now - session.created_at >= SESSION_TTL_SECONDS
    ]
    for code in expired:
        sessions.pop(code, None)


def _new_code() -> str:
    while True:
        code = f"{secrets.randbelow(100_000_000):08d}"
        if code not in sessions:
            return code


def _parse_input(raw: str) -> dict[str, Any] | None:
    try:
        message = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(message, dict) or message.get("type") != "input":
        return None

    kind = message.get("kind")
    if kind in {"mouse_move", "mouse_click"}:
        x, y = message.get("x"), message.get("y")
        if (
            isinstance(x, bool)
            or isinstance(y, bool)
            or not isinstance(x, (int, float))
            or not isinstance(y, (int, float))
            or not math.isfinite(x)
            or not math.isfinite(y)
            or not 0 <= x <= 1
            or not 0 <= y <= 1
        ):
            return None
        if kind == "mouse_click":
            button = message.get("button")
            if button not in {"left", "right", "middle"}:
                return None
            return {"type": "input", "kind": kind, "x": x, "y": y, "button": button}
        return {"type": "input", "kind": kind, "x": x, "y": y}

    if kind in {"key_down", "key_up"}:
        key = message.get("key")
        allowed_named_keys = {
            "enter", "tab", "backspace", "delete", "escape", "space",
            "left", "right", "up", "down", "home", "end", "pageup",
            "pagedown", "shift", "ctrl", "alt",
        }
        if not isinstance(key, str) or not (
            key in allowed_named_keys
            or (len(key) == 1 and key.isprintable() and key.isascii())
        ):
            return None
        return {"type": "input", "kind": kind, "key": key}

    return None


@app.post("/api/sessions")
async def create_session(request: Request) -> dict[str, Any]:
    if not rate_limiter.allow(f"create:{_client_key(request)}", 5, 60):
        raise HTTPException(status_code=429, detail="Too many session requests")
    async with state_lock:
        _prune_expired()
        if len(sessions) >= 1_000:
            raise HTTPException(status_code=503, detail="Server session capacity reached")
        code = _new_code()
        host_token = secrets.token_urlsafe(32)
        sessions[code] = Session(code=code, host_token=host_token)
    return {
        "code": code,
        "host_token": host_token,
        "pairing_expires_in": PAIRING_TTL_SECONDS,
    }


@app.post("/api/connect")
async def connect_viewer(body: ConnectRequest, request: Request) -> dict[str, str]:
    if not rate_limiter.allow(f"connect:{_client_key(request)}", 8, 60):
        raise HTTPException(status_code=429, detail="Too many pairing attempts")
    async with state_lock:
        _prune_expired()
        session = sessions.get(body.code)
        if (
            session is None
            or time.monotonic() - session.created_at >= PAIRING_TTL_SECONDS
            or session.host_ws is None
            or session.viewer_token is not None
        ):
            raise HTTPException(status_code=400, detail="Pairing code is invalid or expired")
        session.viewer_token = secrets.token_urlsafe(32)
        return {"viewer_token": session.viewer_token}


async def _authenticate(websocket: WebSocket, kind: str) -> Session | None:
    if websocket.url.scheme in {"ws", "http"} and not _is_local_host(
        websocket.url.hostname
    ):
        await websocket.close(code=1008)
        return None
    await websocket.accept()
    client = websocket.client.host if websocket.client else "unknown"
    if not rate_limiter.allow(f"ws:{kind}:{client}", 20, 60):
        await websocket.close(code=1013)
        return None
    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=10)
        if len(raw) > 1024:
            raise ValueError("Authentication message too large")
        message = json.loads(raw)
        if not isinstance(message, dict) or message.get("type") != "auth":
            raise ValueError("Invalid authentication message")
        token = message.get("token")
        if not isinstance(token, str):
            raise ValueError("Invalid token")
    except (asyncio.TimeoutError, ValueError, json.JSONDecodeError, WebSocketDisconnect):
        await websocket.close(code=1008)
        return None

    async with state_lock:
        _prune_expired()
        for session in sessions.values():
            expected = session.host_token if kind == "host" else session.viewer_token
            if expected and secrets.compare_digest(expected, token):
                if kind == "host" and session.host_ws is not None:
                    break
                if kind == "viewer" and session.viewer_ws is not None:
                    break
                if kind == "host":
                    session.host_ws = websocket
                else:
                    session.viewer_ws = websocket
                return session
    await websocket.close(code=1008)
    return None


async def _send_json(websocket: WebSocket | None, message: dict[str, Any]) -> None:
    if websocket is not None:
        await websocket.send_json(message)


@app.websocket("/ws/host")
async def host_socket(websocket: WebSocket) -> None:
    session = await _authenticate(websocket, "host")
    if session is None:
        return
    try:
        await _send_json(websocket, {"type": "ready"})
        while True:
            remaining = SESSION_TTL_SECONDS - (time.monotonic() - session.created_at)
            if remaining <= 0:
                break
            message = await asyncio.wait_for(websocket.receive(), timeout=remaining)
            if message["type"] == "websocket.disconnect":
                break
            if message.get("bytes") is not None:
                frame = message["bytes"]
                if (
                    len(frame) <= MAX_FRAME_BYTES
                    and session.approved
                    and session.screen_allowed
                    and session.viewer_ws is not None
                ):
                    try:
                        await session.viewer_ws.send_bytes(frame)
                    except (RuntimeError, WebSocketDisconnect):
                        pass
                continue
            text = message.get("text", "")
            if len(text) > 4096:
                continue
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            if data.get("type") == "approve":
                viewer_id = data.get("viewer_id")
                approved = data.get("approved")
                screen = data.get("screen")
                control = data.get("control")
                if (
                    not isinstance(viewer_id, str)
                    or viewer_id != session.viewer_id
                    or not isinstance(approved, bool)
                    or not isinstance(screen, bool)
                    or not isinstance(control, bool)
                ):
                    continue
                session.approved = approved
                session.screen_allowed = approved and screen
                session.control_allowed = approved and screen and control
                await _send_json(
                    session.viewer_ws,
                    {
                        "type": "access",
                        "approved": session.approved,
                        "screen": session.screen_allowed,
                        "control": session.control_allowed,
                    },
                )
                if not approved:
                    session.viewer_id = None
            elif data.get("type") == "revoke":
                session.approved = False
                session.screen_allowed = False
                session.control_allowed = False
                session.viewer_id = None
                await _send_json(
                    session.viewer_ws,
                    {"type": "access", "approved": False, "screen": False, "control": False},
                )
    except (asyncio.TimeoutError, WebSocketDisconnect, RuntimeError):
        pass
    finally:
        async with state_lock:
            if session.host_ws is websocket:
                session.host_ws = None
            session.approved = False
            session.screen_allowed = False
            session.control_allowed = False
            viewer = session.viewer_ws
            session.viewer_ws = None
            session.viewer_token = None
            session.viewer_id = None
            sessions.pop(session.code, None)
            if viewer is not None:
                try:
                    await viewer.send_json(
                        {"type": "access", "approved": False, "screen": False, "control": False}
                    )
                    await viewer.close(code=1001)
                except (RuntimeError, WebSocketDisconnect):
                    pass


@app.websocket("/ws/viewer")
async def viewer_socket(websocket: WebSocket) -> None:
    session = await _authenticate(websocket, "viewer")
    if session is None:
        return
    try:
        await websocket.send_json({"type": "ready"})
        while True:
            remaining = SESSION_TTL_SECONDS - (time.monotonic() - session.created_at)
            if remaining <= 0:
                break
            message = await asyncio.wait_for(websocket.receive(), timeout=remaining)
            if message["type"] == "websocket.disconnect":
                break
            text = message.get("text", "")
            if len(text) > 4096:
                continue
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            if data.get("type") == "request_access":
                if session.host_ws is None:
                    await websocket.send_json({"type": "error", "message": "Host is offline"})
                elif session.viewer_id is None:
                    session.viewer_id = secrets.token_urlsafe(9)
                    session.approved = False
                    session.screen_allowed = False
                    session.control_allowed = False
                    await _send_json(
                        session.host_ws,
                        {"type": "viewer_request", "viewer_id": session.viewer_id},
                    )
                    await websocket.send_json({"type": "access_pending"})
                else:
                    await websocket.send_json({"type": "access_pending"})
            elif data.get("type") == "end":
                break
            elif session.approved and session.control_allowed:
                safe_input = _parse_input(text)
                if (
                    safe_input is not None
                    and rate_limiter.allow(f"input:{session.viewer_token}", 120, 1)
                ):
                    await _send_json(session.host_ws, safe_input)
    except (asyncio.TimeoutError, WebSocketDisconnect, RuntimeError):
        pass
    finally:
        async with state_lock:
            if session.viewer_ws is websocket:
                session.viewer_ws = None
                session.viewer_token = None
                session.viewer_id = None
                session.approved = False
                session.screen_allowed = False
                session.control_allowed = False
                if session.host_ws is not None:
                    try:
                        await session.host_ws.send_json({"type": "viewer_disconnected"})
                    except (RuntimeError, WebSocketDisconnect):
                        pass


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(WEB_ROOT / "index.html")


app.mount("/", StaticFiles(directory=WEB_ROOT), name="web")
