// Shared helpers for the player and admin pages.
const $ = (sel, root = document) => root.querySelector(sel);

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

function fmt(n) {
  if (n === null || n === undefined) return "–";
  return Number(n).toLocaleString(undefined, { maximumFractionDigits: 1 });
}

function fmtPnl(n) {
  const cls = n > 0 ? "good" : n < 0 ? "bad" : "";
  return `<span class="${cls}">${n > 0 ? "+" : ""}${fmt(n)}</span>`;
}

function fmtTime(ms) {
  return new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

async function api(method, path, body, headers = {}) {
  const r = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json", ...headers },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = null;
  try { data = await r.json(); } catch { /* empty body */ }
  if (!r.ok) {
    const err = new Error((data && data.error) || r.statusText);
    err.status = r.status;
    throw err;
  }
  return data;
}

function toast(msg, isError = false) {
  let box = $("#toast");
  if (!box) {
    box = document.createElement("div");
    box.id = "toast";
    document.body.appendChild(box);
  }
  const el = document.createElement("div");
  el.textContent = msg;
  if (isError) el.className = "err";
  box.appendChild(el);
  setTimeout(() => el.remove(), isError ? 6000 : 2500);
}

// WebSocket that reconnects with backoff. The server sends a full snapshot on every
// (re)connect, so the client never needs to replay missed events.
function liveSocket(path, { onMessage, onStatus, onAuthFail }) {
  let ws, delay = 500, pinger, timer, stopped = false;
  function open() {
    clearTimeout(timer);
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}${path}`);
    ws.onopen = () => {
      delay = 500;
      onStatus(true);
      pinger = setInterval(() => { try { ws.send("ping"); } catch { /* closing */ } }, 20000);
    };
    ws.onmessage = ev => onMessage(JSON.parse(ev.data));
    ws.onclose = ev => {
      clearInterval(pinger);
      onStatus(false);
      if (stopped) return;
      if (ev.code === 4401) { onAuthFail(); return; }
      timer = setTimeout(open, delay);
      delay = Math.min(delay * 2, 5000);
    };
  }
  open();
  // Reconnect promptly when a phone wakes up or the tab regains focus.
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && ws.readyState === WebSocket.CLOSED && !stopped) { delay = 500; open(); }
  });
  return { close() { stopped = true; ws.close(); } };
}

function downloadBlob(filename, text, type = "text/csv") {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
