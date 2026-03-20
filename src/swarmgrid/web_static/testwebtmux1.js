import { Terminal } from "https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/+esm";
import { FitAddon } from "https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.10.0/+esm";

const term = new Terminal({
  cursorBlink: true,
  fontFamily: "Menlo, Monaco, Consolas, monospace",
  fontSize: 14,
  convertEol: false,
  scrollback: 3000,
  theme: {
    background: "#0f1419",
    foreground: "#e7edf3",
    cursor: "#9fd3c7",
  },
});
const fit = new FitAddon();
term.loadAddon(fit);
term.open(document.getElementById("term"));
fit.fit();
term.focus();

let socket = null;
const statusEl = document.getElementById("status");
const issueEl = document.getElementById("issue");
const termEl = document.getElementById("term");

function wsUrl(issueKey) {
  const scheme = window.location.protocol === "https:" ? "wss" : "ws";
  return `${scheme}://${window.location.host}/ws/raw/tickets/${encodeURIComponent(issueKey)}/terminal`;
}

function connect() {
  if (socket) {
    socket.close();
    socket = null;
  }
  term.reset();
  statusEl.textContent = `connecting ${issueEl.value}...`;
  socket = new WebSocket(wsUrl(issueEl.value));
  socket.binaryType = "arraybuffer";
  socket.addEventListener("open", () => {
    statusEl.textContent = `connected ${issueEl.value}`;
    fit.fit();
    socket.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));
    term.focus();
  });
  socket.addEventListener("message", async (event) => {
    if (typeof event.data === "string") {
      try {
        const payload = JSON.parse(event.data);
        if (payload.type === "error") {
          term.writeln(`\r\n[error] ${payload.message}`);
          return;
        }
      } catch {}
      term.write(event.data);
      return;
    }
    const text = new TextDecoder().decode(event.data);
    term.write(text);
  });
  socket.addEventListener("close", () => {
    statusEl.textContent = `closed ${issueEl.value}`;
    term.writeln("\r\n[disconnected]");
  });
  socket.addEventListener("error", () => {
    statusEl.textContent = `error ${issueEl.value}`;
    term.writeln("\r\n[websocket error]");
  });
}

term.onData((data) => {
  if (socket?.readyState === WebSocket.OPEN) {
    socket.send(new TextEncoder().encode(data));
  }
});

window.addEventListener("resize", () => {
  fit.fit();
  if (socket?.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));
  }
});
termEl.addEventListener("click", () => term.focus());
termEl.addEventListener("contextmenu", (event) => event.preventDefault());

document.getElementById("connect").addEventListener("click", connect);
document.getElementById("focus").addEventListener("click", () => term.focus());
connect();
