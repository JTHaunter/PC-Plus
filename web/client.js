const pairing = document.querySelector("#pairing");
const sessionPanel = document.querySelector("#session");
const connectForm = document.querySelector("#connect-form");
const connectButton = document.querySelector("#connect-button");
const requestButton = document.querySelector("#request-button");
const disconnectButton = document.querySelector("#disconnect-button");
const pairingCode = document.querySelector("#pairing-code");
const connectError = document.querySelector("#connect-error");
const statusText = document.querySelector("#status");
const canvas = document.querySelector("#screen");
const placeholder = document.querySelector("#screen-placeholder");
const context = canvas.getContext("2d");

let socket;
let canControl = false;
let lastPointerSend = 0;
const pressedKeys = new Set();

function setStatus(message) {
  statusText.textContent = message;
}

function send(message) {
  if (socket?.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify(message));
  }
}

function keyName(key) {
  const names = {
    " ": "space",
    Enter: "enter",
    Tab: "tab",
    Backspace: "backspace",
    Delete: "delete",
    Escape: "escape",
    ArrowLeft: "left",
    ArrowRight: "right",
    ArrowUp: "up",
    ArrowDown: "down",
    Home: "home",
    End: "end",
    PageUp: "pageup",
    PageDown: "pagedown",
    Shift: "shift",
    Control: "ctrl",
    Alt: "alt",
  };
  if (Object.hasOwn(names, key)) {
    return names[key];
  }
  if (key.length === 1 && key.charCodeAt(0) >= 32 && key.charCodeAt(0) <= 126) {
    return key;
  }
  return null;
}

function releasePressedKeys() {
  for (const key of pressedKeys) {
    send({ type: "input", kind: "key_up", key });
  }
  pressedKeys.clear();
}

function finishSession(message = "Disconnected.") {
  releasePressedKeys();
  canControl = false;
  requestButton.disabled = true;
  if (socket && socket.readyState < WebSocket.CLOSING) {
    send({ type: "end" });
    socket.close();
  }
  socket = null;
  sessionPanel.hidden = true;
  pairing.hidden = false;
  pairingCode.value = "";
  canvas.width = 0;
  canvas.height = 0;
  placeholder.hidden = false;
  setStatus(message);
}

connectForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const code = pairingCode.value.trim();
  if (!/^\d{8}$/.test(code)) {
    connectError.textContent = "Enter the 8-digit code shown by the host.";
    return;
  }

  connectError.textContent = "";
  connectButton.disabled = true;
  connectButton.textContent = "Connecting…";
  try {
    const response = await fetch("/api/connect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      cache: "no-store",
      body: JSON.stringify({ code }),
    });
    if (!response.ok) {
      throw new Error(response.status === 429
        ? "Too many attempts. Wait a minute and try again."
        : "That pairing code is invalid, expired, or already used.");
    }
    const result = await response.json();
    if (typeof result.viewer_token !== "string") {
      throw new Error("The server returned an invalid response.");
    }

    pairing.hidden = true;
    sessionPanel.hidden = false;
    setStatus("Connecting to host…");
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    socket = new WebSocket(`${protocol}//${window.location.host}/ws/viewer`);
    socket.binaryType = "blob";
    socket.addEventListener("open", () => {
      send({ type: "auth", token: result.viewer_token });
    });
    socket.addEventListener("message", async (message) => {
      if (typeof message.data !== "string") {
        try {
          const bitmap = await createImageBitmap(message.data);
          if (canvas.width !== bitmap.width || canvas.height !== bitmap.height) {
            canvas.width = bitmap.width;
            canvas.height = bitmap.height;
          }
          context.drawImage(bitmap, 0, 0);
          bitmap.close();
          placeholder.hidden = true;
        } catch {
          setStatus("Could not display a screen frame.");
        }
        return;
      }

      let data;
      try {
        data = JSON.parse(message.data);
      } catch {
        return;
      }
      if (data.type === "ready") {
        setStatus("Connected. Request access to ask the host.");
        requestButton.disabled = false;
      } else if (data.type === "access_pending") {
        setStatus("Waiting for the host to approve your request…");
        requestButton.disabled = true;
      } else if (data.type === "access") {
        canControl = data.approved && data.control;
        requestButton.disabled = Boolean(data.approved);
        placeholder.hidden = Boolean(data.approved && data.screen && canvas.width);
        if (data.approved && data.screen) {
          setStatus(canControl
            ? "Screen shared; keyboard and mouse control approved."
            : "Screen shared; view-only access.");
        } else {
          setStatus("The host did not approve access, or sharing was stopped.");
          requestButton.disabled = false;
        }
      } else if (data.type === "error") {
        setStatus(data.message);
        requestButton.disabled = false;
      }
    });
    socket.addEventListener("close", () => {
      if (socket) {
        finishSession("The connection ended.");
      }
    });
    socket.addEventListener("error", () => {
      setStatus("Could not connect. Check the server address and try again.");
    });
  } catch (error) {
    connectError.textContent = error instanceof Error ? error.message : "Could not connect.";
  } finally {
    connectButton.disabled = false;
    connectButton.textContent = "Connect";
  }
});

requestButton.addEventListener("click", () => {
  send({ type: "request_access" });
  requestButton.disabled = true;
});

disconnectButton.addEventListener("click", () => finishSession());

canvas.addEventListener("pointermove", (event) => {
  if (!canControl) {
    return;
  }
  const now = performance.now();
  if (now - lastPointerSend < 33) {
    return;
  }
  lastPointerSend = now;
  const rect = canvas.getBoundingClientRect();
  send({
    type: "input",
    kind: "mouse_move",
    x: Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)),
    y: Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height)),
  });
});

canvas.addEventListener("pointerdown", (event) => {
  if (!canControl) {
    return;
  }
  canvas.focus();
  const rect = canvas.getBoundingClientRect();
  const button = ["left", "middle", "right"][event.button];
  if (!button) {
    return;
  }
  send({
    type: "input",
    kind: "mouse_click",
    button,
    x: Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)),
    y: Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height)),
  });
});

canvas.addEventListener("keydown", (event) => {
  if (!canControl) {
    return;
  }
  const key = keyName(event.key);
  if (!key) {
    return;
  }
  event.preventDefault();
  if (!event.repeat && !pressedKeys.has(key)) {
    pressedKeys.add(key);
    send({ type: "input", kind: "key_down", key });
  }
});

canvas.addEventListener("keyup", (event) => {
  const key = keyName(event.key);
  if (key && pressedKeys.has(key)) {
    pressedKeys.delete(key);
    send({ type: "input", kind: "key_up", key });
  }
});

window.addEventListener("blur", releasePressedKeys);
