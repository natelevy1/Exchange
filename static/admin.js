// Admin page.
const ADMIN_KEY = "exchange_admin_token";
let adminToken = null;
try { adminToken = localStorage.getItem(ADMIN_KEY); } catch { /* storage unavailable */ }

let sessions = [];
let sid = null;          // viewed session id
let st = null;           // admin_state for the viewed session
let socket = null;
let editing = null;      // contract id being edited
const pendingConfirm = {}; // key -> timeout, for two-click confirmations

const hdr = () => ({ "X-Admin-Token": adminToken });
const adm = (method, path, body) => api(method, path, body, hdr());

function setAdminToken(t) {
  adminToken = t;
  try { t ? localStorage.setItem(ADMIN_KEY, t) : localStorage.removeItem(ADMIN_KEY); } catch { /* ignore */ }
}

function showLogin() {
  if (socket) { socket.close(); socket = null; }
  $("#app").hidden = true;
  $("#login").hidden = false;
}

async function guard(fn) {
  try { return await fn(); }
  catch (e) {
    if (e.status === 401) { setAdminToken(null); showLogin(); }
    toast(e.message, true);
  }
}

$("#login-form").addEventListener("submit", async ev => {
  ev.preventDefault();
  try {
    const r = await api("POST", "/api/admin/login", { password: $("#password").value });
    setAdminToken(r.token);
    boot();
  } catch (e) { toast(e.message, true); }
});
$("#logout").addEventListener("click", () => { setAdminToken(null); showLogin(); });

async function boot(preferId = null) {
  if (!adminToken) return showLogin();
  const list = await guard(() => adm("GET", "/api/admin/sessions"));
  if (!list) return;
  sessions = list;
  $("#login").hidden = true;
  $("#app").hidden = false;
  const active = sessions.find(s => s.status === "active");
  $("#new-session").hidden = !!active;
  $("#session-select").innerHTML = sessions.map(s =>
    `<option value="${s.id}">${esc(s.code)} — ${s.status} (${s.players} players)</option>`).join("")
    || `<option value="">no sessions yet</option>`;
  const target = preferId ?? (sessions.some(s => s.id === sid) ? sid : (active || sessions[0] || {}).id ?? null);
  selectSession(target);
}

function selectSession(id) {
  if (socket) { socket.close(); socket = null; }
  sid = id;
  st = null;
  $("#session-view").hidden = sid === null;
  if (sid === null) return;
  $("#session-select").value = String(sid);
  socket = liveSocket(`/ws/admin?token=${encodeURIComponent(adminToken)}&session_id=${sid}`, {
    onMessage: msg => { if (msg.type === "admin_state") { st = msg.state; render(); } },
    onStatus: up => $("#conn").classList.toggle("on", up),
    onAuthFail: () => { setAdminToken(null); showLogin(); },
  });
}

$("#session-select").addEventListener("change", ev => selectSession(Number(ev.target.value)));

$("#session-form").addEventListener("submit", ev => {
  ev.preventDefault();
  guard(async () => {
    const r = await adm("POST", "/api/admin/sessions",
      { code: $("#ns-code").value, starting_cash: parseInt($("#ns-cash").value, 10) });
    toast(`Session ${r.code} created`);
    $("#ns-code").value = "";
    boot(r.id);
  });
});

// Two-click confirmation for destructive actions. State lives in `pendingConfirm` (not the
// button) so it survives the frequent re-renders from the live socket.
function confirmClick(key, button, label, fn) {
  if (pendingConfirm[key]) {
    clearTimeout(pendingConfirm[key].timer);
    delete pendingConfirm[key];
    return fn();
  }
  const original = button.textContent;
  pendingConfirm[key] = {
    label,
    timer: setTimeout(() => { delete pendingConfirm[key]; button.textContent = original; button.classList.remove("sell"); render(); }, 4000),
  };
  button.textContent = label;
  button.classList.add("sell");
}

$("#end-session").addEventListener("click", ev => {
  confirmClick("end", ev.currentTarget, "Click again to end session", () => guard(async () => {
    await adm("POST", `/api/admin/sessions/${sid}/end`);
    toast("Session ended");
    boot(sid);
  }));
});

// ---------------------------------------------------------------- rendering
function render() {
  if (!st) return;
  const active = st.session.status === "active";
  $("#join-code").textContent = st.session.code;
  const opt = $(`#session-select option[value="${sid}"]`);
  if (opt) opt.textContent = `${st.session.code} — ${st.session.status} (${st.users.length} players)`;
  $("#sess-status").textContent = st.session.status;
  $("#end-session").hidden = !active;
  $("#contract-editor").hidden = !active;
  $("#announce-panel").hidden = !active;
  renderContracts(active);
  renderPlayers();
  renderOrders();
  renderLog();
  $("#announcements").innerHTML = st.announcements.map(a =>
    `<li><time>${fmtTime(a.ts)}</time>${esc(a.text)}</li>`).join("") || `<li class="empty">None yet</li>`;
  $("#lb-note").textContent = st.final ? "final, realized" : "marked to mid / last";
  $("#leaderboard").innerHTML = `<table><tbody>` + st.leaderboard.map((r, i) =>
    `<tr><td class="muted">${i + 1}</td><td>${esc(r.name)}</td><td class="n">${fmtPnl(r.pnl)}</td></tr>`
  ).join("") + `</tbody></table>`;
}

function contractName(id) {
  const c = st.contracts.find(c => c.id === id);
  return c ? c.name : `#${id}`;
}

function renderContracts(active) {
  if (!st.contracts.length) { $("#contracts").innerHTML = `<div class="empty">No contracts yet</div>`; return; }
  // keep any half-typed settlement values across re-renders
  const typed = {};
  document.querySelectorAll("#contracts input[data-settle]").forEach(i => { typed[i.dataset.settle] = i.value; });
  $("#contracts").innerHTML = `<table><thead><tr><th>Name</th><th>Spec</th><th>State</th>` +
    `<th class="n">Bid</th><th class="n">Ask</th><th class="n">Last</th><th>Actions</th></tr></thead><tbody>` +
    st.contracts.map(c => {
      const bid = c.bids.length ? `${fmt(c.bids[0][1])} @ ${fmt(c.bids[0][0])}` : "–";
      const ask = c.asks.length ? `${fmt(c.asks[0][1])} @ ${fmt(c.asks[0][0])}` : "–";
      let actions = "";
      if (active) {
        if (c.state === "pending") actions += `<button data-act="open" data-id="${c.id}">Open</button> `;
        if (c.state === "open") actions += `<button data-act="halted" data-id="${c.id}">Halt</button> `;
        if (c.state === "halted") actions += `<button data-act="open" data-id="${c.id}">Resume</button> `;
        if (c.state !== "settled") actions += `<button data-act="edit" data-id="${c.id}">Edit</button> `;
        if (c.state === "open" || c.state === "halted") {
          actions += `<input type="number" data-settle="${c.id}" placeholder="value" style="width:80px" ` +
            `min="${c.min}" max="${c.max}" value="${esc(typed[c.id] ?? "")}"> ` +
            (pendingConfirm[`settle${c.id}`]
              ? `<button class="sell" data-act="settle" data-id="${c.id}">${esc(pendingConfirm[`settle${c.id}`].label)}</button>`
              : `<button data-act="settle" data-id="${c.id}">Settle</button>`);
        }
      }
      return `<tr><td title="${esc(c.description)}"><b>${esc(c.name)}</b></td>` +
        `<td class="muted">${c.kind} [${fmt(c.min)}, ${fmt(c.max)}] tick ${c.tick}</td>` +
        `<td><span class="badge ${c.state}">${c.state}</span>` +
        (c.state === "settled" ? ` at <b>${fmt(c.settlement_value)}</b>` : "") + `</td>` +
        `<td class="n buyc">${bid}</td><td class="n sellc">${ask}</td><td class="n">${fmt(c.last)}</td>` +
        `<td>${actions}</td></tr>`;
    }).join("") + `</tbody></table>`;
}

function renderPlayers() {
  const live = st.contracts.filter(c => c.state !== "pending");
  if (!st.users.length) { $("#players").innerHTML = `<div class="empty">Nobody has joined. Join code: <b>${esc(st.session.code)}</b></div>`; return; }
  const counts = {};
  st.open_orders.forEach(o => { counts[o.user_id] = (counts[o.user_id] || 0) + 1; });
  const pnl = Object.fromEntries(st.leaderboard.map(r => [r.user_id, r.pnl]));
  $("#players").innerHTML = `<table><thead><tr><th>Player</th><th class="n">Cash</th>` +
    `<th class="n">Available</th><th class="n">P&amp;L</th>` +
    live.map(c => `<th class="n">${esc(c.name)}</th>`).join("") +
    `<th class="n">Orders</th></tr></thead><tbody>` +
    st.users.map(u => `<tr><td>${esc(u.name)}</td><td class="n">${fmt(u.cash)}</td>` +
      `<td class="n">${fmt(u.available)}</td><td class="n">${fmtPnl(pnl[u.id])}</td>` +
      live.map(c => {
        const q = u.positions[c.id] || 0;
        return `<td class="n ${q > 0 ? "buyc" : q < 0 ? "sellc" : "muted"}">${q ? fmt(q) : "·"}</td>`;
      }).join("") +
      `<td class="n">${counts[u.id] || 0}</td></tr>`).join("") + `</tbody></table>`;
}

function renderOrders() {
  if (!st.open_orders.length) { $("#open-orders").innerHTML = `<div class="empty">None</div>`; return; }
  const names = Object.fromEntries(st.users.map(u => [u.id, u.name]));
  $("#open-orders").innerHTML = `<table><thead><tr><th>Player</th><th>Contract</th><th>Side</th>` +
    `<th class="n">Price</th><th class="n">Remaining</th></tr></thead><tbody>` +
    st.open_orders.map(o => `<tr><td>${esc(names[o.user_id])}</td><td>${esc(contractName(o.contract_id))}</td>` +
      `<td class="${o.side === "buy" ? "buyc" : "sellc"}">${o.side}</td><td class="n">${fmt(o.price)}</td>` +
      `<td class="n">${fmt(o.remaining)}</td></tr>`).join("") + `</tbody></table>`;
}

function renderLog() {
  if (!st.order_log.length) { $("#order-log").innerHTML = `<div class="empty">None</div>`; return; }
  $("#order-log").innerHTML = `<table><thead><tr><th>Time</th><th>Player</th><th>Contract</th>` +
    `<th>Side</th><th>Type</th><th class="n">Price</th><th class="n">Qty</th><th class="n">Left</th>` +
    `<th>Status</th></tr></thead><tbody>` +
    st.order_log.map(o => `<tr><td class="muted">${fmtTime(o.created_at)}</td><td>${esc(o.user_name)}</td>` +
      `<td>${esc(contractName(o.contract_id))}</td><td class="${o.side === "buy" ? "buyc" : "sellc"}">${o.side}</td>` +
      `<td>${o.tif}</td><td class="n">${fmt(o.price)}</td><td class="n">${fmt(o.qty)}</td>` +
      `<td class="n">${fmt(o.remaining)}</td>` +
      `<td>${o.status}${o.cancel_reason ? ` <span class="muted">(${o.cancel_reason.replace("_", " ")})</span>` : ""}</td></tr>`
    ).join("") + `</tbody></table>`;
}

// ------------------------------------------------------------------ contracts
function syncKind() {
  const binary = $("#ce-kind").value === "binary";
  if (binary) { $("#ce-min").value = 0; $("#ce-max").value = 100; }
  $("#ce-min").disabled = $("#ce-max").disabled = binary;
}
$("#ce-kind").addEventListener("change", syncKind);
syncKind();

function resetEditor() {
  editing = null;
  $("#contract-form").reset();
  $("#ce-title").textContent = "New contract";
  $("#ce-submit").textContent = "Create";
  $("#ce-cancel").hidden = true;
  ["#ce-kind", "#ce-tick"].forEach(s => { $(s).disabled = false; });
  syncKind();
}
$("#ce-cancel").addEventListener("click", resetEditor);

function startEdit(c) {
  editing = c.id;
  $("#ce-title").textContent = `Edit ${c.name}`;
  $("#ce-submit").textContent = "Save";
  $("#ce-cancel").hidden = false;
  $("#ce-name").value = c.name;
  $("#ce-desc").value = c.description;
  $("#ce-kind").value = c.kind;
  $("#ce-min").value = c.min;
  $("#ce-max").value = c.max;
  $("#ce-tick").value = c.tick;
  syncKind();
  const locked = c.state !== "pending";  // spec fields can only change while pending
  ["#ce-kind", "#ce-min", "#ce-max", "#ce-tick"].forEach(s => { if (locked) $(s).disabled = true; });
  $("#ce-name").focus();
}

$("#contract-form").addEventListener("submit", ev => {
  ev.preventDefault();
  const body = {
    name: $("#ce-name").value,
    description: $("#ce-desc").value,
    kind: $("#ce-kind").value,
    min: parseInt($("#ce-min").value, 10),
    max: parseInt($("#ce-max").value, 10),
    tick: parseInt($("#ce-tick").value, 10),
  };
  guard(async () => {
    if (editing !== null) {
      const c = st.contracts.find(x => x.id === editing);
      if (c && c.state !== "pending") { delete body.kind; delete body.min; delete body.max; delete body.tick; }
      await adm("PATCH", `/api/admin/contracts/${editing}`, body);
      toast("Contract saved");
    } else {
      await adm("POST", `/api/admin/sessions/${sid}/contracts`, body);
      toast("Contract created (pending: open it when ready)");
    }
    resetEditor();
  });
});

$("#contracts").addEventListener("click", ev => {
  const b = ev.target.closest("button[data-act]");
  if (!b) return;
  const id = Number(b.dataset.id);
  const c = st.contracts.find(x => x.id === id);
  if (b.dataset.act === "edit") return startEdit(c);
  if (b.dataset.act === "settle") {
    const input = document.querySelector(`input[data-settle="${id}"]`);
    const v = parseInt(input.value, 10);
    if (!Number.isInteger(v)) return toast("Enter a settlement value", true);
    return confirmClick(`settle${id}`, b, `Confirm ${v}?`, () => guard(async () => {
      await adm("POST", `/api/admin/contracts/${id}/settle`, { value: v });
      toast(`${c.name} settled at ${v}`);
    }));
  }
  guard(() => adm("POST", `/api/admin/contracts/${id}/state`, { state: b.dataset.act }));
});

// ---------------------------------------------------------------- misc
$("#announce-form").addEventListener("submit", ev => {
  ev.preventDefault();
  guard(async () => {
    await adm("POST", `/api/admin/sessions/${sid}/announcements`, { text: $("#ann-text").value });
    $("#ann-text").value = "";
  });
});

document.querySelectorAll("[data-export]").forEach(b => b.addEventListener("click", () => guard(async () => {
  const what = b.dataset.export;
  const r = await fetch(`/api/admin/sessions/${sid}/export/${what}.csv`, { headers: hdr() });
  if (!r.ok) throw Object.assign(new Error("Export failed"), { status: r.status });
  downloadBlob(`${st.session.code}-${what}.csv`, await r.text());
})));

boot();
