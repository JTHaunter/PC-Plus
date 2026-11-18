from __future__ import annotations

import argparse
import asyncio
import io
import json
import sys
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse

import mss
import websockets
from PIL import Image
from pynput import keyboard, mouse
from pynput.keyboard import Key, KeyCode


MAX_WIDTH = 1280
FRAME_INTERVAL_SECONDS = 0.33
MAX_FRAME_BYTES = 1_000_000
KEYS = {
    "enter": Key.enter,
    "tab": Key.tab,
    "backspace": Key.backspace,
    "delete": Key.delete,
    "escape": Key.esc,
    "space": Key.space,
    "left": Key.left,
    "right": Key.right,
    "up": Key.up,
    "down": Key.down,
    "home": Key.home,
    "end": Key.end,
    "pageup": Key.page_up,
    "pagedown": Key.page_down,
    "shift": Key.shift,
    "ctrl": Key.ctrl,
    "alt": Key.alt,
}


def _request_session(server_url: str) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{server_url}/api/sessions",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not create a session at {server_url}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("code"), str) or not isinstance(
        data.get("host_token"), str
    ):
        raise RuntimeError("The server returned an invalid session response")
    return data


def _server_websocket_url(server_url: str, path: str) -> str:
    parsed = urlparse(server_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return f"{scheme}://{parsed.netloc}{path}"


def _ask(prompt: str) -> bool:
    try:
        return input(prompt).strip().lower() in {"y", "yes"}
    except EOFError:
        return False


def _handle_input(
    message: dict[str, Any],
    pointer: mouse.Controller,
    keys: keyboard.Controller,
    pressed_keys: set[Any],
    screen_width: int,
    screen_height: int,
) -> None:
    kind = message.get("kind")
    if kind in {"mouse_move", "mouse_click"}:
        x, y = message.get("x"), message.get("y")
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            return
        pointer.position = (
            min(screen_width - 1, max(0, round(x * (screen_width - 1)))),
            min(screen_height - 1, max(0, round(y * (screen_height - 1)))),
        )
        if kind == "mouse_click":
            button = {
                "left": mouse.Button.left,
                "right": mouse.Button.right,
                "middle": mouse.Button.middle,
            }.get(message.get("button"))
            if button is not None:
                pointer.click(button)
        return

    key_name = message.get("key")
    if not isinstance(key_name, str):
        return
    key = KEYS.get(key_name)
    if key is None and len(key_name) == 1 and key_name.isascii() and key_name.isprintable():
        try:
            key = KeyCode.from_char(key_name)
        except ValueError:
            return
    if key is None:
        return

    if kind == "key_down":
        keys.press(key)
        pressed_keys.add(key)
    elif kind == "key_up":
        keys.release(key)
        pressed_keys.discard(key)


async def run(server_url: str) -> None:
    session = _request_session(server_url)
    websocket_url = _server_websocket_url(server_url, "/ws/host")
    pressed_keys: set[Any] = set()
    stream_task: asyncio.Task[None] | None = None

    print(f"Connecting to {server_url} ...")
    async with websockets.connect(
        websocket_url,
        max_size=MAX_FRAME_BYTES,
        ping_interval=20,
        ping_timeout=20,
    ) as websocket:
        await websocket.send(
            json.dumps({"type": "auth", "token": session["host_token"]})
        )
        ready = json.loads(await asyncio.wait_for(websocket.recv(), timeout=10))
        if not isinstance(ready, dict) or ready.get("type") != "ready":
            raise RuntimeError("The server rejected the host connection")

        print()
        print(f"One-time browser pairing code: {session['code']}")
        print("Share this code only with the person you want to connect.")
        print("The relay server can see screen contents and input; use only a server you trust.")
        print("Approve screen and control separately in this terminal.")
        print("Press Ctrl+C here to end the session and disconnect the viewer.")
        print()

        with mss.mss() as capture:
            monitor = capture.monitors[1]
            screen_width = monitor["width"]
            screen_height = monitor["height"]
            pointer = mouse.Controller()
            keys = keyboard.Controller()

            async def stream_screen() -> None:
                while True:
                    image = capture.grab(monitor)
                    frame = Image.frombytes("RGB", image.size, image.rgb)
                    if frame.width > MAX_WIDTH:
                        scaled_height = round(frame.height * MAX_WIDTH / frame.width)
                        frame = frame.resize((MAX_WIDTH, scaled_height), Image.Resampling.LANCZOS)
                    output = io.BytesIO()
                    frame.save(output, format="JPEG", quality=60, optimize=True)
                    payload = output.getvalue()
                    if len(payload) <= MAX_FRAME_BYTES:
                        await websocket.send(payload)
                    await asyncio.sleep(FRAME_INTERVAL_SECONDS)

            try:
                async for incoming in websocket:
                    if isinstance(incoming, bytes):
                        continue
                    try:
                        message = json.loads(incoming)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(message, dict):
                        continue

                    if message.get("type") == "viewer_request":
                        viewer_id = message.get("viewer_id")
                        if not isinstance(viewer_id, str):
                            continue
                        print("\nA viewer is requesting access.")
                        allow_screen = await asyncio.to_thread(
                            _ask, "Allow them to view your screen? [y/N] "
                        )
                        allow_control = False
                        if allow_screen:
                            allow_control = await asyncio.to_thread(
                                _ask, "Also allow keyboard and mouse control? [y/N] "
                            )
                        await websocket.send(
                            json.dumps(
                                {
                                    "type": "approve",
                                    "viewer_id": viewer_id,
                                    "approved": allow_screen,
                                    "screen": allow_screen,
                                    "control": allow_control,
                                }
                            )
                        )
                        if allow_screen and (stream_task is None or stream_task.done()):
                            stream_task = asyncio.create_task(stream_screen())
                        elif not allow_screen and stream_task is not None:
                            stream_task.cancel()
                            stream_task = None
                    elif message.get("type") == "access" and not message.get("approved"):
                        if stream_task is not None:
                            stream_task.cancel()
                            stream_task = None
                    elif message.get("type") == "viewer_disconnected":
                        if stream_task is not None:
                            stream_task.cancel()
                            stream_task = None
                        for key in tuple(pressed_keys):
                            keys.release(key)
                        pressed_keys.clear()
                        print("\nViewer disconnected.")
                    elif message.get("type") == "input":
                        _handle_input(
                            message,
                            pointer,
                            keys,
                            pressed_keys,
                            screen_width,
                            screen_height,
                        )
            finally:
                if stream_task is not None:
                    stream_task.cancel()
                    try:
                        await stream_task
                    except asyncio.CancelledError:
                        pass
                for key in tuple(pressed_keys):
                    keys.release(key)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Start a consent-based PC-Plus remote desktop host."
    )
    parser.add_argument(
        "--server",
        required=True,
        help="Public HTTPS URL of your self-hosted PC-Plus server",
    )
    args = parser.parse_args()
    server_url = args.server.rstrip("/")
    parsed = urlparse(server_url)
    if parsed.scheme not in {"https", "http"} or not parsed.netloc:
        parser.error("--server must be an http:// or https:// URL")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        parser.error("Use HTTPS except for local development on this computer")
    try:
        asyncio.run(run(server_url))
    except KeyboardInterrupt:
        print("\nSession ended.")
    except (RuntimeError, OSError, websockets.WebSocketException) as exc:
        print(f"Host error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
