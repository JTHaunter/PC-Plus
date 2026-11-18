# PC-Plus Remote Desktop

PC-Plus is an early, self-hosted remote desktop prototype. The host runs a small Python agent; a remote person connects from a normal browser without installing a client. The server relays screen frames and input but does not save recordings.

## What works in this milestone

- One host and one browser viewer per session.
- An eight-digit, one-use code that expires after 10 minutes.
- Sessions automatically end after two hours.
- The host must approve screen viewing and keyboard/mouse control separately.
- The host can end access at any time by closing the host app or pressing `Ctrl+C`.
- The host streams its primary display; input is limited to mouse movement/clicks and keyboard events.
- No clipboard, file transfer, remote shell, unattended access, or automatic startup.

This is a prototype, not a finished installer. The host currently runs from Python source. Windows is the initial target; the capture/control libraries are cross-platform, but macOS permission flows and Linux desktop environments have not yet been validated. In particular, Wayland may restrict screen capture or input. Only the primary display is shared.

## Run locally for a smoke test

Install Python 3.10 or later. Open two terminals in the repository.

**Terminal 1 — broker and browser app**

```powershell
cd server
py -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

**Terminal 2 — host agent**

```powershell
cd host
py -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python agent.py --server http://127.0.0.1:8000
```

Open `http://127.0.0.1:8000` in a browser and enter the code printed by the host. Approve access in the host terminal. Localhost is the only supported plain-HTTP setup.

For macOS/Linux, create and activate a virtual environment using that platform's Python commands, install `host/requirements.txt`, and run `python agent.py --server https://your-server.example`. macOS may require Screen Recording and Accessibility permissions. Platform support is not yet verified.

## Self-host for remote access

The broker must be reachable by both the host and browser over HTTPS/WSS. Deploy it on a server you control, place it behind a TLS reverse proxy (for example, Caddy), and proxy requests to the Uvicorn process. Keep Uvicorn bound to loopback; do not expose its plain-HTTP port directly to the internet. Configure the proxy to preserve WebSocket upgrades and forward trusted scheme/client-IP headers so HTTP is redirected to HTTPS and per-IP pairing limits work correctly.

Then run the host agent with the public HTTPS address:

```text
python agent.py --server https://your-server.example
```

The host shares its short-lived code with the intended viewer, who opens that HTTPS address in any modern browser. No client-side installation is needed.

The broker relays screen frames and input in plaintext after terminating HTTPS/WSS, so the server operator can see and control the session. Use only a server you control and trust. The broker keeps session tokens and connection state in memory only and does not persist recordings. Restarting it disconnects sessions. Pairing attempts and session creation are rate-limited per client IP in process memory; use a trusted reverse proxy and do not run this as an unauthenticated public service without reviewing the deployment and abuse controls.

## Tests

```powershell
python -m pip install -r server\requirements-test.txt
python -m unittest discover -s server -p test_*.py
```

## Security notes

- Use HTTPS/WSS for any non-local deployment. The host refuses non-local plain HTTP.
- Pairing codes are short-lived and rate-limited; viewer tokens are random and held only in browser memory.
- The host is visibly running and must explicitly approve each viewer's screen and control permissions.
- Treat the pairing code like a password and share it only with the intended viewer.
- This prototype has not had a security audit. It is not designed for unattended access or high-risk networks.
