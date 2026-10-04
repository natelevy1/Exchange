// Player page.
const TOKEN_KEY = "exchange_token";
let token = null;
try { token = localStorage.getItem(TOKEN_KEY); } catch { /* storage unavailable */ }

let state = null;        // { public, me } from the server
let selected = null;     // selected contract id
let view = null;         // DOM refs of the rendered contract
let seenAnn = null;      // highest announcement id already shown
let qty = 1;             // sticky order quantity
let socket = null;

const hdr = () => ({ "X-Token": token });

function setToken(t) {
  token = t;
  try { t ? localStorage.setItem(TOKEN_KEY, t) : localStorage.removeItem(TOKEN_KEY); } catch { /* ignore */ }
}

function showJoin() {
  if (socket) { socket.close(); socket = null; }
  $("#app").hidden = true;
  $("#join").hidden = false;
}

async function boot() {
  if (!token) return showJoin();
  try {
    state = await api("GET", "/api/state", undefined, hdr());
  } catch (e) {
    if (e.status === 401) { setToken(null); return showJoin(); }
    toast(e.message, true);
    return setTimeout(boot, 2000);
  }
  $("#join").hidden = true;
  $("#app").hidden = false;
  render();
  socket = liveSocket(`/ws?token=${encodeURIComponent(token)}`, {
    onMessage: msg => { if (msg.type === "state") { state = msg; render(); } },
    onStatus: up => $("#conn").classList.toggle("on", up),
    onAuthFail: () => { setToken(null); showJoin(); },
  });
}

$("#join-form").addEventListener("submit", async ev => {
  ev.preventDefault();
  try {
    const r = await api("POST", "/api/join", { code: $("#join-code").value, name: $("#join-name").value });
    setToken(r.token);
    boot();
  } catch (e) { toast(e.message, true); }
});

// ---------------------------------------------------------------- rendering
const visibleContracts = () => state.public.contracts.filter(c => c.state !== "pending");
const contractById = id => state.public.contracts.find(c => c.id === id);
const sessionEnded = () => state.public.session.status !== "active";

function mid(c) {
  if (c.bids.length && c.asks.length) return (c.bids[0][0] + c.asks[0][0]) / 2;
  return null;
}

function render() {
  const { public: pub, me } = state;
  $("#sess-code").textContent = pub.session.code;
  $("#me-name").textContent = me.user.name;
  $("#me-cash").textContent = fmt(me.user.cash);
  $("#me-avail").textContent = fmt(me.user.available);
  $("#me-pnl").innerHTML = fmtPnl(me.user.pnl);
  $("#ended").hidden = !sessionEnded();
  $("#cancel-everything").hidden = sessionEnded();
  document.title = `${pub.session.code} · Exchange`;

  const cs = visibleContracts();
  if (!cs.some(c => c.id === selected)) {
    selected = (cs.find(c => c.state === "open") || cs[0] || {}).id ?? null;
    view = null;
  }
  $("#tabs").innerHTML = cs.map(c =>
    `<button data-id="${c.id}" class="${c.id === selected ? "active" : ""}">${esc(c.name)}` +
    (c.state !== "open" ? `<span class="badge ${c.state}">${c.state}</span>` : "") + `</button>`
  ).join("") || `<div class="empty">No contracts are trading yet.</div>`;

  renderContract();
  renderOrders();
  renderPositions();
  renderLeaderboard();
  renderAnnouncements();
}

function buildContractView() {
  const host = $("#contract-view");
  host.innerHTML = "";
  if (selected === null) { view = null; return; }
  host.appendChild($("#contract-tpl").content.cloneNode(true));
  view = { id: selected };
  host.querySelectorAll("[data-f]").forEach(el => { view[el.dataset.f] = el; });
  view.qty.value = qty;
  view.qty.addEventListener("input", () => { qty = parseInt(view.qty.value, 10) || 1; });
  host.querySelectorAll("[data-act]").forEach(b => b.addEventListener("click", () => act(b.dataset.act)));
  view.buttons = host.querySelectorAll("[data-act]");
  view.ladder.addEventListener("click", ev => {
    const tr = ev.target.closest("tr[data-price]");
    if (tr) { view.price.value = tr.dataset.price; view.price.focus(); }
  });
}

function ladderRows(c) {
  const t = c.tick, N = 5;
  const clip = p => Math.min(c.max, Math.max(c.min, p));
  const snap = p => c.min + Math.round((p - c.min) / t) * t;
  const prices = new Set();
  const addRange = (a, b) => { for (let p = clip(a); p <= clip(b); p += t) prices.add(p); };
  const bb = c.bids.length ? c.bids[0][0] : null;
  const ba = c.asks.length ? c.asks[0][0] : null;
  if (bb === null && ba === null) {
    const m = snap(c.last ?? (c.min + c.max) / 2);
    addRange(m - N * t, m + N * t);
  } else if (bb !== null && ba !== null && (ba - bb) / t <= 2 * N) {
    addRange(bb - N * t, ba + N * t);
  } else {
    for (const x of [bb, ba]) if (x !== null) addRange(x - N * t, x + N * t);
  }
  c.bids.forEach(([p]) => prices.add(p));
  c.asks.forEach(([p]) => prices.add(p));
  if (c.last !== null) prices.add(c.last);
  return [...prices].sort((a, b) => b - a);
}

function renderContract() {
  if (selected === null) { $("#contract-view").innerHTML = ""; view = null; return; }
  if (!view || view.id !== selected) buildContractView();
  const c = contractById(selected);
  view.name.textContent = c.name;
  let meta = `${c.kind} · range ${fmt(c.min)}–${fmt(c.max)} · tick ${c.tick} · ` +
    `<span class="badge ${c.state}">${c.state}</span>`;
  if (c.state === "settled") meta += ` <b>settled at ${fmt(c.settlement_value)}</b>`;
  view.meta.innerHTML = meta;
  view.desc.textContent = c.description;
  view.desc.hidden = !c.description;
  view.price.min = c.min; view.price.max = c.max; view.price.step = c.tick;
  view.last.textContent = fmt(c.last);
  view.mid.textContent = fmt(mid(c));

  const tradable = c.state === "open" && !sessionEnded();
  view.buttons.forEach(b => {
    b.disabled = b.dataset.act === "cancel-contract"
      ? !state.me.orders.some(o => o.contract_id === c.id) : !tradable;
  });

  const bids = new Map(c.bids), asks = new Map(c.asks);
  const mine = new Map();
  state.me.orders.filter(o => o.contract_id === c.id)
    .forEach(o => mine.set(o.price, (mine.get(o.price) || 0) + o.remaining));
  const bb = c.bids.length ? c.bids[0][0] : null, ba = c.asks.length ? c.asks[0][0] : null;
  const rows = ladderRows(c);
  let html = "", prev = null;
  for (const p of rows) {
    if (prev !== null && prev - p > c.tick) html += `<tr class="gap"><td colspan="3">⋮</td></tr>`;
    prev = p;
    const b = bids.get(p), a = asks.get(p), m = mine.get(p);
    const my = m ? `<span class="mine">(${fmt(m)} mine)</span>` : "";
    html += `<tr data-price="${p}" class="${p === c.last ? "last" : ""}">` +
      `<td class="bq ${b ? "has" : ""}">${b ? fmt(b) : ""}${b && my ? my : ""}</td>` +
      `<td class="px ${p === bb ? "buyc" : p === ba ? "sellc" : ""}">${fmt(p)}</td>` +
      `<td class="aq ${a ? "has" : ""}">${a ? fmt(a) : ""}${a && my ? my : ""}</td></tr>`;
  }
  view.ladder.innerHTML = html;

  view.tape.innerHTML = c.trades.map(t =>
    `<tr><td class="muted">${fmtTime(t.ts)}</td>` +
    `<td class="n ${t.side === "buy" ? "buyc" : "sellc"}">${fmt(t.price)}</td>` +
    `<td class="n">${fmt(t.qty)}</td></tr>`
  ).join("") || `<tr><td class="empty">No trades yet</td></tr>`;
}

function renderOrders() {
  const os = state.me.orders;
  if (!os.length) { $("#my-orders").innerHTML = `<div class="empty">No open orders</div>`; return; }
  $("#my-orders").innerHTML = `<table><thead><tr><th>Contract</th><th>Side</th><th class="n">Price</th>` +
    `<th class="n">Remaining</th><th></th></tr></thead><tbody>` +
    os.map(o => `<tr><td>${esc(contractById(o.contract_id).name)}</td>` +
      `<td class="${o.side === "buy" ? "buyc" : "sellc"}">${o.side}</td>` +
      `<td class="n">${fmt(o.price)}</td><td class="n">${fmt(o.remaining)}</td>` +
      `<td class="n"><button class="link" data-cancel="${o.id}" title="Cancel">✕</button></td></tr>`
    ).join("") + `</tbody></table>`;
}

function renderPositions() {
  const ps = state.me.positions;
  if (!ps.length) { $("#my-positions").innerHTML = `<div class="empty">No positions</div>`; return; }
  $("#my-positions").innerHTML = `<table><thead><tr><th>Contract</th><th class="n">Position</th>` +
    `<th class="n">Mark</th></tr></thead><tbody>` +
    ps.map(p => {
      const c = contractById(p.contract_id);
      const mark = p.settled ? `settled ${fmt(c.settlement_value)} → ${fmtPnl(p.payout)}`
        : fmt(mid(c) ?? c.last);
      return `<tr><td>${esc(c.name)}</td>` +
        `<td class="n ${p.qty > 0 ? "buyc" : "sellc"}">${p.qty > 0 ? "+" : ""}${fmt(p.qty)}</td>` +
        `<td class="n">${mark}</td></tr>`;
    }).join("") + `</tbody></table>`;
}

function renderLeaderboard() {
  const pub = state.public;
  $("#lb-note").textContent = pub.final ? "final, realized" : "marked to mid / last trade";
  $("#leaderboard").innerHTML = `<table><tbody>` + pub.leaderboard.map((r, i) =>
    `<tr class="${r.user_id === state.me.user.id ? "me" : ""}"><td class="muted">${i + 1}</td>` +
    `<td>${esc(r.name)}</td><td class="n">${fmtPnl(r.pnl)}</td></tr>`
  ).join("") + `</tbody></table>`;
}

function renderAnnouncements() {
  const anns = state.public.announcements;  // newest first
  const top = anns.length ? anns[0].id : 0;
  if (seenAnn !== null) {
    anns.filter(a => a.id > seenAnn).reverse().forEach(a => toast(a.text));
  }
  $("#announcements").innerHTML = anns.map(a =>
    `<li class="${seenAnn !== null && a.id > seenAnn ? "fresh" : ""}"><time>${fmtTime(a.ts)}</time>${esc(a.text)}</li>`
  ).join("") || `<li class="empty">Nothing yet</li>`;
  seenAnn = Math.max(seenAnn ?? 0, top);
}

// ------------------------------------------------------------------ actions
function describe(r, side, price) {
  const parts = [];
  if (r.filled) {
    const notional = r.fills.reduce((s, f) => s + f.price * f.qty, 0);
    const prices = new Set(r.fills.map(f => f.price));
    const px = prices.size === 1 ? `@ ${fmt(r.fills[0].price)}` : `@ avg ${fmt(notional / r.filled)}`;
    parts.push(`${side === "buy" ? "Bought" : "Sold"} ${fmt(r.filled)} ${px}`);
  }
  if (r.remaining) parts.push(`${r.filled ? "" : side === "buy" ? "Bid " : "Offer "}${fmt(r.remaining)} resting @ ${fmt(price)}`);
  if (r.status === "cancelled") parts.push(r.filled ? "rest cancelled (IOC)" : "nothing to trade against (IOC cancelled)");
  if (r.self_trade_cancelled.length) parts.push(`cancelled ${r.self_trade_cancelled.length} of your own resting orders (self-trade)`);
  return parts.join(" · ");
}

async function send(side, price, ioc) {
  const c = contractById(selected);
  const q = parseInt(view.qty.value, 10);
  if (!Number.isInteger(price)) return toast("Enter a price", true);
  if (!Number.isInteger(q) || q < 1) return toast("Enter a quantity of at least 1", true);
  try {
    const r = await api("POST", "/api/orders",
      { contract_id: c.id, side, price, qty: q, tif: ioc ? "ioc" : "limit" }, hdr());
    toast(describe(r, side, price));
  } catch (e) { toast(e.message, true); }
}

async function act(what) {
  const c = contractById(selected);
  const price = parseInt(view.price.value, 10);
  if (what === "buy" || what === "sell") return send(what, price, view.ioc.checked);
  if (what === "mkt-buy") return send("buy", c.max, true);
  if (what === "mkt-sell") return send("sell", c.min, true);
  if (what === "cancel-contract") return cancelAll(c.id);
}

async function cancelAll(contractId = null) {
  try {
    const r = await api("POST", "/api/cancel_all", { contract_id: contractId }, hdr());
    toast(`Cancelled ${r.cancelled} order${r.cancelled === 1 ? "" : "s"}`);
  } catch (e) { toast(e.message, true); }
}

$("#tabs").addEventListener("click", ev => {
  const b = ev.target.closest("button[data-id]");
  if (b) { selected = Number(b.dataset.id); render(); }
});
$("#my-orders").addEventListener("click", async ev => {
  const b = ev.target.closest("[data-cancel]");
  if (!b) return;
  try { await api("DELETE", `/api/orders/${b.dataset.cancel}`, undefined, hdr()); }
  catch (e) { toast(e.message, true); }
});
$("#cancel-everything").addEventListener("click", () => cancelAll(null));

boot();
