/* Meme Agents Terminal - front end.
 *
 * Rules this file follows:
 *  - No secrets are ever stored here: the API key is posted once and forgotten.
 *  - Every mutation carries the CSRF token from /api/bootstrap.
 *  - The UI never decides whether an order is allowed; it only renders the
 *    server's verdict, including the full checklist it returns.
 */
"use strict";

const S = {
  csrf: "",
  mode: "PAPER",
  liveActive: false,
  snapshot: null,
  market: null,
  symbol: "BTCUSDT",
  interval: "5m",
  side: "BUY",
  orderType: "MARKET",
  preview: null,
  consensus: null,
  config: null,
  phrase: "I ACCEPT THE RISK",
  symbolsLoaded: false,
  busy: false,
};

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
};

/* ---------------------------------------------------------------- format */
const usd = (v, d = 2) => (v === null || v === undefined || isNaN(v)) ? "—" :
  (Math.abs(v) >= 1000 ? v.toLocaleString(undefined, { maximumFractionDigits: 0 })
    : Number(v).toFixed(d));
const money = (v, d = 2) => "$" + usd(v, d);
const pct = (v) => (v === null || v === undefined || isNaN(v)) ? "—" :
  (v >= 0 ? "+" : "") + Number(v).toFixed(2) + "%";
const cls = (v) => (v > 0 ? "pos" : v < 0 ? "neg" : "");
const price = (v) => {
  if (v === null || v === undefined || !isFinite(v)) return "—";
  if (v === 0) return "0";
  const a = Math.abs(v);
  if (a >= 1000) return v.toFixed(2);
  if (a >= 1) return v.toFixed(4);
  if (a >= 0.01) return v.toFixed(6);
  return v.toPrecision(4);
};
const ago = (s) => (s === null || s === undefined) ? "—" :
  (s < 60 ? Math.round(s) + "s ago" : s < 3600 ? Math.round(s / 60) + "m ago"
    : (s / 3600).toFixed(1) + "h ago");

function toast(message, kind = "") {
  const node = $("#toast");
  node.textContent = message;
  node.className = "toast show " + kind;
  clearTimeout(node._t);
  node._t = setTimeout(() => { node.className = "toast"; }, 4200);
}

/* ---------------------------------------------------------------- api */
async function api(path, { method = "GET", body = null, retry = true } = {}) {
  const opts = { method, headers: {}, credentials: "same-origin" };
  if (body !== null) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  if (method !== "GET") opts.headers["X-CSRF-Token"] = S.csrf;
  const response = await fetch(path, opts);
  let payload = null;
  try { payload = await response.json(); } catch (e) { payload = null; }
  if (response.status === 401) {
    showLogin();
    throw new Error("not signed in");
  }
  if (!response.ok) {
    const message = (payload && (payload.error || payload.message)) || ("HTTP " + response.status);
    const error = new Error(message);
    error.code = payload && payload.code;
    error.payload = payload;
    throw error;
  }
  return payload;
}

function showLogin() {
  $("#login").classList.remove("hidden");
}
function hideLogin() {
  $("#login").classList.add("hidden");
}

async function login(code) {
  const response = await fetch("/api/session", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    body: JSON.stringify({ access_code: code }),
  });
  if (!response.ok) throw new Error("access code refused");
  const data = await response.json();
  S.csrf = data.csrf || "";
  hideLogin();
  await boot(true);
}

/* ---------------------------------------------------------------- boot */
async function boot(afterLogin = false) {
  try {
    const info = await api("/api/bootstrap");
    S.csrf = info.csrf || S.csrf;
    S.phrase = info.live_confirmation_phrase || S.phrase;
    $("#live-phrase").textContent = S.phrase;
    hideLogin();
  } catch (error) {
    if (!afterLogin) showLogin();
    return;
  }
  await Promise.all([refreshState(), refreshMarket()]);
  startPolling();
}

function startPolling() {
  if (startPolling.started) return;
  startPolling.started = true;
  setInterval(() => refreshState().catch(() => {}), 5000);
  setInterval(() => refreshMarket().catch(() => {}), 4000);
  setInterval(() => loadAudit().catch(() => {}), 20000);
}

/* ---------------------------------------------------------------- state */
async function refreshState() {
  const data = await api("/api/state");
  S.snapshot = data;
  S.mode = data.mode;
  S.liveActive = data.live_active;
  S.config = data.config;
  if (data.active_symbol && data.active_symbol !== S.symbol) {
    S.symbol = data.active_symbol;
  }
  if (data.agents && data.agents.consensus && Object.keys(data.agents.consensus).length) {
    S.consensus = data.agents.consensus;
    S.agentStates = data.agents.states;
    if (data.agents.registry) S.registry = data.agents.registry;
  }
  renderChrome(data);
  renderDashboard(data);
  renderTicketMeta(data);
  renderAgentsPage();
  renderSettings(data);
}

function renderChrome(data) {
  const mode = data.mode;
  const badge = $("#mode-badge");
  badge.textContent = mode === "LIVE_SPOT" ? "LIVE" : mode;
  badge.className = "mode " + (mode === "LIVE_SPOT" ? "live" : mode === "PAPER" ? "paper" : "");
  $("#live-banner").classList.toggle("hidden", mode !== "LIVE_SPOT");
  $("#paper-banner").classList.toggle("hidden", mode === "LIVE_SPOT");
  $("#demo-banner").classList.toggle("hidden", !(data.config && data.config.demo));

  const p = data.portfolio || {};
  $("#equity").textContent = money(p.equity);
  const dayPnl = $("#day-pnl");
  dayPnl.textContent = (p.day_pnl >= 0 ? "+" : "") + usd(p.day_pnl);
  dayPnl.className = cls(p.day_pnl);
  $("#available").textContent = money(p.quote_balance);
  $("#quote-asset").textContent = (data.config && data.config.quote_asset) || "USDT";
  $("#available-hint").textContent = data.mode === "LIVE_SPOT"
    ? "from the exchange" : "simulated balance";
  $("#unrealized").textContent = (p.unrealized_pnl >= 0 ? "+" : "") + usd(p.unrealized_pnl);
  $("#unrealized").className = cls(p.unrealized_pnl);
  $("#exposure-hint").textContent = "exposure " + money(p.exposure) + " / " + money(p.max_exposure);
  $("#realized").textContent = (p.realized_pnl >= 0 ? "+" : "") + usd(p.realized_pnl);
  $("#realized").className = cls(p.realized_pnl);
  $("#winrate-hint").textContent = (p.win_rate_pct || 0).toFixed(0) + "% win rate · "
    + p.wins + "W / " + p.losses + "L";

  const loss = p.daily_loss || { pct: 0, used: 0, limit: 0 };
  $("#risk-used").textContent = money(loss.used) + " / " + money(loss.limit);
  const bar = $("#risk-bar");
  bar.style.width = Math.min(100, loss.pct || 0) + "%";
  bar.className = loss.pct >= 100 ? "crit" : loss.pct >= 60 ? "hot" : "";

  const dot = $("#conn-dot");
  const connected = data.connection && data.connection.connected;
  const age = data.health && data.health.market ? data.health.market.last_refresh_age_s : null;
  dot.className = "dot " + (age !== null && age < 30 ? "on" : age === null ? "" : "dead");
  $("#last-update").textContent = "updated " + (data.generated_at_utc || "");

  $("#ticket-mode").textContent = mode;
  $("#mode-pill").textContent = mode;
  const stopBtn = $("#stop-btn");
  stopBtn.disabled = !!data.risk.emergency_stop;
  stopBtn.textContent = data.risk.emergency_stop ? "STOPPED" : "STOP";
  if (data.risk.emergency_stop) stopBtn.classList.add("on");
}

function renderDashboard(data) {
  const p = data.portfolio || {};

  /* watchlist */
  const tbody = $("#watchlist tbody");
  tbody.textContent = "";
  (data.watchlist || []).forEach((row) => {
    const tr = el("tr");
    tr.appendChild(el("td", "sym", row.symbol));
    tr.appendChild(el("td", "num", price(row.price)));
    const change = el("td", "num " + cls(row.change_pct), pct(row.change_pct));
    tr.appendChild(change);
    tr.appendChild(el("td", "num", (row.spread_bps || 0).toFixed(1) + " bps"));
    tr.addEventListener("click", () => {
      setSymbol(row.symbol);
    });
    tbody.appendChild(tr);
  });
  if (!(data.watchlist || []).length) {
    const tr = el("tr");
    const td = el("td", "empty", "No market data yet.");
    td.colSpan = 4;
    tr.appendChild(td);
    tbody.appendChild(tr);
  }
  const age = data.health && data.health.market ? data.health.market.last_refresh_age_s : null;
  $("#watch-age").textContent = age === null ? "waiting" : "feed " + ago(age);

  /* positions */
  const positionsBody = $("#positions tbody");
  positionsBody.textContent = "";
  $("#positions-count").textContent = p.open_positions + " / " + p.max_positions;
  if (!(p.positions || []).length) {
    const tr = el("tr");
    const td = el("td", "empty", "No open positions.");
    td.colSpan = 6;
    tr.appendChild(td);
    positionsBody.appendChild(tr);
  }
  (p.positions || []).forEach((pos) => {
    const tr = el("tr");
    tr.appendChild(el("td", "sym", pos.symbol));
    tr.appendChild(el("td", "num", price(pos.qty)));
    tr.appendChild(el("td", "num", price(pos.entry_price)));
    tr.appendChild(el("td", "num", price(pos.mark_price)));
    tr.appendChild(el("td", "num " + cls(pos.unrealized_pnl),
      (pos.unrealized_pnl >= 0 ? "+" : "") + usd(pos.unrealized_pnl)
      + " (" + pct(pos.unrealized_pct) + ")"));
    const cell = el("td");
    const button = el("button", "btn small danger", "Close");
    button.addEventListener("click", () => confirmClose(pos));
    cell.appendChild(button);
    tr.appendChild(cell);
    positionsBody.appendChild(tr);
  });

  /* open orders */
  const ordersBody = $("#open-orders tbody");
  ordersBody.textContent = "";
  if (!(data.open_orders || []).length) {
    const tr = el("tr");
    const td = el("td", "empty", "None.");
    td.colSpan = 6;
    tr.appendChild(td);
    ordersBody.appendChild(tr);
  }
  (data.open_orders || []).forEach((order) => {
    const tr = el("tr");
    tr.appendChild(el("td", "sym", order.symbol));
    tr.appendChild(el("td", { BUY: "pos", SELL: "neg" }[order.side] || "", order.side));
    tr.appendChild(el("td", "num", price(order.qty)));
    tr.appendChild(el("td", "num", price(order.price)));
    tr.appendChild(el("td", "", order.status));
    const cell = el("td");
    const button = el("button", "btn small ghost", "Cancel");
    button.addEventListener("click", () => cancelOrder(order.client_order_id));
    cell.appendChild(button);
    tr.appendChild(cell);
    ordersBody.appendChild(tr);
  });

  /* fills */
  const fillsBody = $("#fills tbody");
  fillsBody.textContent = "";
  const fills = (data.recent_fills || []).slice(0, 8);
  if (!fills.length) {
    const tr = el("tr");
    const td = el("td", "empty", "No trades yet.");
    td.colSpan = 6;
    tr.appendChild(td);
    fillsBody.appendChild(tr);
  }
  fills.forEach((fill) => {
    const tr = el("tr");
    tr.appendChild(el("td", "", (fill.ts_utc || "").slice(11, 19)));
    tr.appendChild(el("td", "sym", fill.symbol || "—"));
    tr.appendChild(el("td", { BUY: "pos", SELL: "neg" }[fill.side] || "", fill.side || "—"));
    tr.appendChild(el("td", "num", price(Number(fill.qty))));
    tr.appendChild(el("td", "num", price(Number(fill.price))));
    tr.appendChild(el("td", "num", Number(fill.fee || 0).toFixed(4)));
    fillsBody.appendChild(tr);
  });

  /* consensus */
  const c = S.consensus;
  if (c && c.agents_total) {
    const verdict = $("#consensus-verdict");
    verdict.textContent = c.verdict;
    verdict.className = "verdict " + ({ BUY: "buy", SELL: "sell", REJECT: "reject" }[c.verdict] || "");
    $("#consensus-score").textContent = (c.score >= 0 ? "+" : "") + c.score.toFixed(1);
    $("#consensus-bar").style.width = Math.min(100, Math.abs(c.score)) + "%";
    $("#consensus-bar").style.background = c.score >= 0 ? "var(--accent)" : "var(--sell)";
    $("#consensus-detail").textContent = c.agents_ok + "/" + c.agents_total + " agents ok · "
      + (c.confidence * 100).toFixed(0) + "% coverage · " + c.latency_ms.toFixed(0) + " ms";
    $("#consensus-age").textContent = "as of " + (c.evaluated_at_utc || "");
    const teams = $("#consensus-teams");
    teams.textContent = "";
    (c.teams || []).slice(0, 5).forEach((team) => {
      teams.appendChild(el("li", "", team.name + " " + (team.score >= 0 ? "+" : "")
        + team.score.toFixed(2)));
    });
    const vetoes = $("#consensus-vetoes");
    vetoes.textContent = "";
    (c.vetoes || []).forEach((veto) => {
      vetoes.appendChild(el("li", "", "VETO " + veto.role + " · " + (veto.codes || []).join(",")));
    });
  }

  /* health */
  const health = data.health || {};
  const list = $("#health-list");
  list.textContent = "";
  const rows = [
    ["Mode", data.mode],
    ["Exchange key", data.connection.connected ? "stored (" + data.connection.api_key_masked + ")" : "not configured"],
    ["Market feed", (health.market && health.market.last_refresh_age_s !== null)
      ? ago(health.market.last_refresh_age_s) : "waiting"],
    ["API requests", health.market && health.market.client ? health.market.client.requests : "—"],
    ["API errors", health.market && health.market.client ? health.market.client.errors : "—"],
    ["Weight used (1m)", health.market && health.market.client ? health.market.client.weight_used_1m : "—"],
    ["Latency", health.market && health.market.client ? health.market.client.last_latency_ms + " ms" : "—"],
    ["Telegram", health.telegram ? (health.telegram.enabled ? "on (" + health.telegram.sent + " sent)" : "off") : "off"],
    ["Last error", (health.last_error || "none")],
  ];
  rows.forEach(([key, value]) => {
    list.appendChild(el("dt", "", key));
    list.appendChild(el("dd", "", String(value)));
  });
}

/* ---------------------------------------------------------------- market + chart */
async function refreshMarket() {
  const data = await api("/api/market?symbol=" + encodeURIComponent(S.symbol)
    + "&interval=" + encodeURIComponent(S.interval));
  S.market = data;
  drawChart(data);
  renderChartMeta(data);
}

function renderChartMeta(data) {
  const meta = $("#chart-meta");
  const last = (data.candles || []).slice(-1)[0];
  meta.textContent = "";
  if (!last) return;
  const [, o, h, l, c] = last;
  const parts = [
    data.symbol, "O " + price(o), "H " + price(h), "L " + price(l), "C " + price(c),
    "data " + ago(data.data_age_s),
  ];
  parts.forEach((part) => meta.appendChild(el("span", "", part)));
}

function drawChart(data) {
  const canvas = $("#chart");
  const ctx = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const cssWidth = canvas.clientWidth || 600;
  const cssHeight = canvas.clientHeight || 300;
  canvas.width = Math.round(cssWidth * dpr);
  canvas.height = Math.round(cssHeight * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssWidth, cssHeight);

  const candles = (data.candles || []).slice(-150);
  $("#chart-title").textContent = data.symbol + " · " + data.interval;
  if (candles.length < 2) {
    ctx.fillStyle = "#5d6b80";
    ctx.font = "13px system-ui";
    ctx.fillText("No candles yet — check the exchange connection.", 14, cssHeight / 2);
    return;
  }

  const padLeft = 6, padRight = 58, padTop = 8, padBottom = 22;
  const volH = Math.max(30, cssHeight * 0.16);
  const plotW = cssWidth - padLeft - padRight;
  const plotH = cssHeight - padTop - padBottom - volH;
  const highs = candles.map((c) => c[2]);
  const lows = candles.map((c) => c[3]);
  const vols = candles.map((c) => c[5]);
  let hi = Math.max(...highs), lo = Math.min(...lows);
  if (hi === lo) { hi += 0.0001; lo -= 0.0001; }
  const pad = (hi - lo) * 0.08;
  hi += pad; lo -= pad;
  const maxVol = Math.max(...vols) || 1;
  const step = plotW / candles.length;
  const bodyW = Math.max(1, Math.min(9, step * 0.62));

  const y = (value) => padTop + plotH - ((value - lo) / (hi - lo)) * plotH;
  const vy = (value) => padTop + plotH + volH - (value / maxVol) * (volH - 4);

  /* grid + price labels */
  ctx.strokeStyle = "rgba(31,41,55,.75)";
  ctx.fillStyle = "#5d6b80";
  ctx.font = "10px ui-monospace, monospace";
  ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const value = lo + ((hi - lo) * i) / 4;
    const py = Math.round(y(value)) + 0.5;
    ctx.beginPath();
    ctx.moveTo(padLeft, py);
    ctx.lineTo(padLeft + plotW, py);
    ctx.stroke();
    ctx.fillText(price(value), padLeft + plotW + 6, py + 3);
  }

  /* candles */
  candles.forEach((candle, i) => {
    const [, o, h, l, c, v] = candle;
    const x = padLeft + i * step + step / 2;
    const up = c >= o;
    ctx.strokeStyle = up ? "#2ee6a8" : "#ff6b81";
    ctx.fillStyle = ctx.strokeStyle;
    ctx.beginPath();
    ctx.moveTo(Math.round(x) + 0.5, y(h));
    ctx.lineTo(Math.round(x) + 0.5, y(l));
    ctx.stroke();
    const top = y(Math.max(o, c));
    const height = Math.max(1, Math.abs(y(o) - y(c)));
    ctx.fillRect(x - bodyW / 2, top, bodyW, height);
    ctx.globalAlpha = 0.45;
    ctx.fillRect(x - bodyW / 2, vy(v), bodyW, Math.max(1, padTop + plotH + volH - vy(v)));
    ctx.globalAlpha = 1;
  });

  /* last price line */
  const lastClose = candles[candles.length - 1][4];
  const ly = y(lastClose);
  ctx.strokeStyle = "rgba(70,224,176,.45)";
  ctx.setLineDash([4, 4]);
  ctx.beginPath();
  ctx.moveTo(padLeft, ly);
  ctx.lineTo(padLeft + plotW, ly);
  ctx.stroke();
  ctx.setLineDash([]);

  /* time labels */
  ctx.fillStyle = "#5d6b80";
  const first = candles[0][0], lastTs = candles[candles.length - 1][0];
  [0, 0.5, 1].forEach((fraction) => {
    const ts = first + (lastTs - first) * fraction;
    const label = new Date(ts).toISOString().slice(11, 16);
    const x = padLeft + plotW * fraction;
    ctx.fillText(label, Math.min(x, padLeft + plotW - 28), cssHeight - 6);
  });
}

/* ---------------------------------------------------------------- trade */
function setSymbol(symbol) {
  S.symbol = symbol.toUpperCase();
  $("#symbol-input").value = S.symbol;
  refreshMarket().catch(() => {});
  api("/api/market?symbol=" + encodeURIComponent(S.symbol)
    + "&interval=" + encodeURIComponent(S.interval)).catch(() => {});
}

function renderTicketMeta(data) {
  const mark = S.market && S.market.ticker ? Number(S.market.ticker.lastPrice) : null;
  $("#ticket-mark").textContent = mark ? price(mark) : "—";
  const limits = data.risk.limits;
  $("#limit-order").textContent = money(limits.max_order_usdt);
  $("#limit-exposure").textContent = money(limits.max_exposure_usdt);
  $("#limit-daily").textContent = money(limits.daily_loss_limit_usdt);
  $("#limit-positions").textContent = limits.max_positions;
}

function orderPayload() {
  const payload = {
    symbol: S.symbol,
    side: S.side,
    order_type: S.orderType,
  };
  const amount = parseFloat($("#quote-amount").value || "0");
  if (!isFinite(amount) || amount <= 0) throw new Error("enter an amount greater than zero");
  if (S.orderType === "LIMIT") {
    const limitPrice = parseFloat($("#limit-price").value || "0");
    if (!isFinite(limitPrice) || limitPrice <= 0) throw new Error("enter a limit price");
    payload.price = limitPrice;
    payload.qty = amount / limitPrice;
  } else {
    payload.quote_amount = amount;
  }
  return payload;
}

async function previewOrder() {
  let payload;
  try { payload = orderPayload(); } catch (error) { toast(error.message, "bad"); return; }
  try {
    const data = await api("/api/order/preview", { method: "POST", body: payload });
    S.preview = { ...data.preview, payload };
    renderRisk(data.preview);
    $("#preview-note").textContent = data.preview.allowed
      ? "All checks passed. Nothing sent yet."
      : "Blocked: " + data.preview.message;
  } catch (error) {
    toast(error.message, "bad");
  }
}

function renderRisk(preview) {
  const pill = $("#risk-pill");
  pill.textContent = preview.allowed ? "READY" : preview.code;
  pill.className = "pill " + (preview.allowed ? "ok" : "bad");
  const list = $("#risk-checks");
  list.textContent = "";
  (preview.checks || []).forEach((check) => {
    const li = el("li", check.ok ? "ok" : "bad");
    li.appendChild(el("span", "mark", check.ok ? "✓" : (check.severity === "warning" ? "!" : "✗")));
    const body = el("span");
    body.appendChild(el("span", "", check.code.replace(/_/g, " ").toLowerCase()));
    if (check.detail) body.appendChild(el("span", "why", " — " + check.detail));
    li.appendChild(body);
    list.appendChild(li);
  });
  (preview.warnings || []).forEach((warning) => {
    const li = el("li", "ok");
    li.appendChild(el("span", "mark", "!"));
    li.appendChild(el("span", "why", warning));
    list.appendChild(li);
  });
  $("#ticket-fee").textContent = money(preview.estimated_fee, 4);
  $("#ticket-slip").textContent = money(preview.estimated_slippage, 4);
  $("#ticket-total").textContent = money(preview.notional + preview.estimated_total_cost);
}

function openModal(title, html, onConfirm, confirmLabel) {
  $("#modal-title").textContent = title;
  $("#modal-body").innerHTML = html;
  $("#modal").classList.remove("hidden");
  const confirm = $("#modal-confirm");
  confirm.textContent = confirmLabel || "Confirm";
  const close = () => $("#modal").classList.add("hidden");
  $("#modal-cancel").onclick = close;
  confirm.onclick = async () => {
    confirm.disabled = true;
    try { await onConfirm(); close(); } catch (error) { toast(error.message, "bad"); }
    finally { confirm.disabled = false; }
  };
}

function confirmOrder() {
  const preview = S.preview;
  if (!preview) { toast("preview the order first", "bad"); return; }
  if (!preview.allowed) { toast("server blocked this order: " + preview.message, "bad"); return; }
  const rows = [
    ["Symbol", preview.symbol],
    ["Side", preview.side],
    ["Type", preview.order_type],
    ["Quantity", preview.qty || "market"],
    ["Notional", money(preview.notional)],
    ["Est. fee", money(preview.estimated_fee, 4)],
    ["Est. slippage", money(preview.estimated_slippage, 4)],
    ["Mode", S.mode],
  ];
  const html = rows.map(([key, value]) =>
    '<div class="row"><span>' + key + '</span><span>' + value + "</span></div>").join("")
    + '<div class="row big"><span>Total</span><span>'
    + money(preview.notional + preview.estimated_total_cost) + "</span></div>"
    + (S.mode === "LIVE_SPOT"
      ? '<p class="warn">This sends a REAL order to Binance Spot.</p>'
      : '<p class="hint">Simulated fill in PAPER mode.</p>');
  openModal("Confirm " + preview.side + " " + preview.symbol, html, async () => {
    const response = await api("/api/order", {
      method: "POST",
      body: { ...preview.payload, confirm: true },
    });
    if (!response.ok) throw new Error(response.message || "order refused");
    toast(response.status + " " + preview.symbol, "good");
    $("#quote-amount").value = "";
    S.preview = null;
    await refreshState();
  }, S.mode === "LIVE_SPOT" ? "Send real order" : "Place paper order");
}

function confirmClose(position) {
  const html = '<div class="row"><span>Symbol</span><span>' + position.symbol + "</span></div>"
    + '<div class="row"><span>Quantity</span><span>' + price(position.qty) + "</span></div>"
    + '<div class="row"><span>Unrealized</span><span class="' + cls(position.unrealized_pnl)
    + '">' + usd(position.unrealized_pnl) + "</span></div>"
    + '<p class="hint">A market sell closes the position at the current mark, minus '
    + "slippage and fees.</p>";
  openModal("Close position", html, async () => {
    const response = await api("/api/positions/close", {
      method: "POST",
      body: { position_id: position.position_id, confirm: true },
    });
    if (!response.ok) throw new Error(response.message || "refused");
    toast("position closed", "good");
    await refreshState();
  }, "Close position");
}

async function cancelOrder(clientOrderId) {
  try {
    const response = await api("/api/order/cancel", {
      method: "POST", body: { client_order_id: clientOrderId },
    });
    toast(response.status, "good");
    await refreshState();
  } catch (error) { toast(error.message, "bad"); }
}

/* ---------------------------------------------------------------- agents */
function renderAgentsPage() {
  const c = S.consensus;
  if (c && c.agents_total) {
    const verdict = $("#agents-verdict");
    verdict.textContent = c.verdict;
    verdict.className = "verdict " + ({ BUY: "buy", SELL: "sell", REJECT: "reject" }[c.verdict] || "");
    $("#agents-score").textContent = (c.score >= 0 ? "+" : "") + c.score.toFixed(1);
    $("#agents-bar").style.width = Math.min(100, Math.abs(c.score)) + "%";
    $("#agents-bar").style.background = c.score >= 0 ? "var(--accent)" : "var(--sell)";
    $("#agents-meta").textContent = c.agents_ok + " ok · " + c.agents_no_data + " no data · "
      + c.agents_error + " error · " + c.agents_total + " total · "
      + (c.missing_sources || []).length + " missing sources";

    const teams = $("#agent-teams");
    teams.textContent = "";
    (c.teams || []).forEach((team) => {
      const li = el("li");
      li.appendChild(el("span", "", team.name));
      const bar = el("div", "bar");
      const fill = el("span", team.score < 0 ? "neg" : "");
      fill.style.width = Math.min(100, Math.abs(team.score) * 100) + "%";
      bar.appendChild(fill);
      li.appendChild(bar);
      li.appendChild(el("span", "num " + cls(team.score), (team.score >= 0 ? "+" : "")
        + team.score.toFixed(2)));
      teams.appendChild(li);
    });

    const evidence = $("#agent-evidence");
    evidence.textContent = "";
    (c.top_evidence || []).forEach((item) => {
      const li = el("li");
      li.appendChild(el("div", "role", item.role + " · " + item.team
        + "  (" + (item.score >= 0 ? "+" : "") + item.score + ", conf " + item.confidence + ")"));
      li.appendChild(el("div", "codes", (item.codes || []).join(" · ")));
      const details = Object.entries(item.evidence || {}).slice(0, 4)
        .map(([k, v]) => k + "=" + (typeof v === "number" ? Number(v.toFixed ? v.toFixed(4) : v) : v))
        .join("  ");
      li.appendChild(el("div", "val", details));
      evidence.appendChild(li);
    });
  }
  renderAgentGrid();
}

function renderAgentGrid() {
  const grid = $("#agent-grid");
  if (!S.registry) return;
  const filter = $("#agent-team-filter").value;
  const search = ($("#agent-search").value || "").toLowerCase();
  grid.textContent = "";
  S.registry.filter((agent) => (!filter || agent.team === filter)
    && (!search || (agent.role || "").toLowerCase().includes(search)))
    .forEach((agent) => {
      const state = (S.agentStates || {})[agent.agent_id] || { state: "idle", vote: "neutral" };
      const node = el("div", "agent " + (state.state === "no_data" ? "no_data" : state.vote));
      node.appendChild(el("div", "id", agent.agent_id + " · " + agent.team));
      node.appendChild(el("span", "role", agent.role));
      if (state.state === "idle") {
        node.appendChild(el("div", "meta", "idle"));
      } else {
        node.appendChild(el("div", "meta", state.state + " · " + (state.score >= 0 ? "+" : "")
          + Number(state.score || 0).toFixed(2) + " · " + Math.round((state.confidence || 0) * 100) + "%"));
        if ((state.reason_codes || []).length) {
          node.appendChild(el("div", "meta", state.reason_codes.slice(0, 2).join(" · ")));
        }
      }
      node.title = JSON.stringify(state.evidence || {}, null, 0).slice(0, 300);
      grid.appendChild(node);
    });
  if (!grid.children.length) {
    grid.appendChild(el("p", "muted", "No agents match that filter."));
  }
}

async function runAgents() {
  try {
    $("#agents-run").disabled = true;
    const data = await api("/api/agents/run", {
      method: "POST", body: { symbol: S.symbol },
    });
    S.consensus = data;
    S.agentStates = null;
    renderAgentsPage();
    toast("100 agents evaluated in " + data.latency_ms.toFixed(0) + " ms", "good");
    await refreshState();
  } catch (error) {
    toast(error.message, "bad");
  } finally {
    $("#agents-run").disabled = false;
  }
}

/* ---------------------------------------------------------------- settings */
function renderSettings(data) {
  const conn = data.connection || {};
  $("#conn-pill").textContent = conn.connected ? "key stored" : "disconnected";
  $("#conn-pill").className = "pill " + (conn.connected ? "ok" : "");
  const kv = $("#conn-kv");
  kv.textContent = "";
  const rows = [
    ["Status", conn.connected ? "API key stored" : "no API key"],
    ["Key", conn.api_key_masked || "—"],
    ["Fingerprint", conn.fingerprint || "—"],
    ["Stored", conn.stored_at_utc || "—"],
    ["File mode", conn.file_mode || "—"],
  ];
  rows.forEach(([key, value]) => { kv.appendChild(el("dt", "", key)); kv.appendChild(el("dd", "", value)); });
  if (conn.warning) kv.appendChild(el("dd", "", "⚠ " + conn.warning));
  const lastTest = conn.last_test || {};
  const checks = $("#conn-checks");
  checks.textContent = "";
  (lastTest.checks || []).forEach((check) => {
    const li = el("li", check.ok ? "ok" : "bad");
    li.appendChild(el("span", "mark", check.ok ? "✓" : "✗"));
    li.appendChild(el("span", "", check.name + (check.detail ? " — " + check.detail : "")));
    checks.appendChild(li);
  });
  (lastTest.warnings || []).forEach((warning) => {
    const li = el("li", "bad");
    li.appendChild(el("span", "mark", "!"));
    li.appendChild(el("span", "why", warning));
    checks.appendChild(li);
  });

  const mode = data.mode;
  $$("#mode-seg button").forEach((button) => {
    button.classList.toggle("on", button.dataset.mode === mode
      || (mode === "LIVE_SPOT" && button.dataset.mode === "LIVE_SPOT"));
  });

  const tg = data.health.telegram || {};
  const tgKv = $("#telegram-kv");
  tgKv.textContent = "";
  [["Status", tg.enabled ? "enabled" : "off"],
   ["Sent", tg.sent || 0],
   ["Skipped (duplicates)", tg.skipped || 0],
   ["Failed", tg.failed || 0]
  ].forEach(([key, value]) => {
    tgKv.appendChild(el("dt", "", key));
    tgKv.appendChild(el("dd", "", String(value)));
  });

  if (S.config && !S.limitsRendered) {
    $("#max-order").value = S.config.max_order_usdt;
    $("#max-exposure").value = S.config.max_exposure_usdt;
    $("#daily-loss").value = S.config.daily_loss_limit_usdt;
    $("#max-positions").value = S.config.max_positions;
    $("#per-symbol").value = S.config.per_symbol_exposure_usdt;
    $("#slippage").value = S.config.slippage_bps;
    $("#refresh").value = S.config.market_refresh_s;
    $("#watchlist").value = (S.config.watchlist || []).join(",");
    S.limitsRendered = true;
  }
}

async function loadAudit() {
  const data = await api("/api/audit?limit=40");
  const list = $("#audit-list");
  list.textContent = "";
  (data.events || []).slice().reverse().forEach((event) => {
    const li = el("li", "", (event.ts_utc || "").slice(11, 19) + "  " + event.event
      + (event.symbol ? " " + event.symbol : "")
      + (event.detail ? " — " + String(event.detail).slice(0, 80) : ""));
    list.appendChild(li);
  });
  if (!list.children.length) list.appendChild(el("li", "muted", "No events yet."));
}

/* ---------------------------------------------------------------- backtest */
async function runBacktest(event) {
  event.preventDefault();
  $("#bt-status").textContent = "collecting candles…";
  $("#bt-results").classList.add("hidden");
  try {
    const data = await api("/api/backtest", {
      method: "POST",
      body: {
        symbol: $("#bt-symbol").value.trim().toUpperCase(),
        interval: $("#bt-interval").value,
        bars: parseInt($("#bt-bars").value, 10) || 1500,
        save: $("#bt-save").checked,
      },
    });
    const report = data.report || {};
    if (!report.ok) throw new Error(report.error || "backtest failed");
    renderBacktest(report);
    $("#bt-status").textContent = "done · " + report.bars.usable + " usable bars";
  } catch (error) {
    $("#bt-status").textContent = "";
    toast(error.message, "bad");
  }
}

function renderBacktest(report) {
  $("#bt-results").classList.remove("hidden");
  const metrics = report.metrics || {};
  const baseline = report.baseline || {};
  const pill = $("#bt-sample-pill");
  pill.textContent = metrics.sample_sufficient ? "sample ok" : "insufficient sample";
  pill.className = "pill " + (metrics.sample_sufficient ? "ok" : "bad");

  const stats = $("#bt-stats");
  stats.textContent = "";
  [
    ["Net return", pct(metrics.net_return_pct), cls(metrics.net_return_pct)],
    ["Trades", metrics.trades, ""],
    ["Win rate", (metrics.win_rate_pct || 0).toFixed(1) + "%", ""],
    ["Max drawdown", (metrics.max_drawdown_pct || 0).toFixed(2) + "%", "neg"],
    ["Expectancy", money(metrics.expectancy, 4), cls(metrics.expectancy)],
    ["Precision", ((metrics.precision || 0) * 100).toFixed(1) + "%", ""],
  ].forEach(([label, value, klass]) => {
    const card = el("div", "card stat");
    card.appendChild(el("span", "label", label));
    card.appendChild(el("strong", klass, String(value)));
    stats.appendChild(card);
  });

  const body = $("#bt-table tbody");
  body.textContent = "";
  const rows = [
    ["Net return %", metrics.net_return_pct, baseline.net_return_pct],
    ["Expectancy / trade", metrics.expectancy, baseline.expectancy],
    ["Trades", metrics.trades, baseline.trades],
    ["Win rate %", metrics.win_rate_pct, baseline.win_rate_pct],
    ["Max drawdown %", metrics.max_drawdown_pct, baseline.max_drawdown_pct],
    ["False positive rate", (metrics.false_positive_rate || 0) * 100, null],
  ];
  rows.forEach(([label, model, base]) => {
    const tr = el("tr");
    tr.appendChild(el("td", "", label));
    tr.appendChild(el("td", "num", typeof model === "number" ? model.toFixed(3) : String(model)));
    tr.appendChild(el("td", "num", base === null || base === undefined ? "—"
      : (typeof base === "number" ? base.toFixed(3) : String(base))));
    body.appendChild(tr);
  });
  $("#bt-honesty").textContent = (report.honesty || []).join(" ");
}

/* ---------------------------------------------------------------- actions */
async function setMode(mode) {
  try {
    if (mode === "LIVE_SPOT") {
      $("#live-form").classList.remove("hidden");
      const pre = await api("/api/live/preflight");
      const list = $("#preflight-checks");
      list.textContent = "";
      (pre.checks || []).forEach((check) => {
        const li = el("li", check.ok ? "ok" : "bad");
        li.appendChild(el("span", "mark", check.ok ? "✓" : "✗"));
        li.appendChild(el("span", "", check.name + (check.detail ? " — " + check.detail : "")));
        list.appendChild(li);
      });
      return;
    }
    $("#live-form").classList.add("hidden");
    await api("/api/mode", { method: "POST", body: { mode } });
    await refreshState();
    toast("mode: " + mode, "good");
  } catch (error) {
    toast(error.message, "bad");
  }
}

async function activateLive() {
  try {
    const data = await api("/api/live/activate", {
      method: "POST",
      body: { confirmation: $("#live-confirm").value, acknowledge: $("#live-ack").checked },
    });
    $("#live-confirm").value = "";
    $("#live-ack").checked = false;
    await refreshState();
    toast("LIVE SPOT active for this session", "bad");
  } catch (error) {
    toast(error.message, "bad");
  }
}

async function emergencyStop() {
  const proceed = window.confirm(
    "Emergency stop?\n\nNew entries are blocked and open orders are cancelled. "
    + "Open positions are NOT sold automatically.");
  if (!proceed) return;
  try {
    const data = await api("/api/emergency-stop", {
      method: "POST", body: { reason: "dashboard emergency stop" },
    });
    toast("stopped — new entries blocked", "bad");
    await refreshState();
  } catch (error) { toast(error.message, "bad"); }
}

async function saveCredentials(event) {
  event.preventDefault();
  const key = $("#api-key").value.trim();
  const secret = $("#api-secret").value.trim();
  if (!key || !secret) { toast("both fields are required", "bad"); return; }
  try {
    await api("/api/credentials", { method: "POST", body: { api_key: key, api_secret: secret } });
    $("#api-key").value = "";
    $("#api-secret").value = "";
    toast("key stored locally (0600). Live trading is still off.", "good");
    await refreshState();
  } catch (error) { toast(error.message, "bad"); }
}

async function testConnection() {
  try {
    const data = await api("/api/credentials/test", { method: "POST", body: {} });
    const ok = data.result && data.result.ok;
    toast(ok ? "connection ok — no order was placed" : "connection test failed", ok ? "good" : "bad");
    await refreshState();
  } catch (error) { toast(error.message, "bad"); }
}

async function deleteCredentials() {
  if (!window.confirm("Delete the stored API key? Live trading will be disabled.")) return;
  try {
    await api("/api/credentials", { method: "DELETE", body: {} });
    toast("credentials deleted", "good");
    await refreshState();
  } catch (error) { toast(error.message, "bad"); }
}

async function saveLimits(event) {
  event.preventDefault();
  const watchlist = $("#watchlist").value.split(",").map((s) => s.trim().toUpperCase())
    .filter(Boolean);
  const payload = {
    max_order_usdt: parseFloat($("#max-order").value),
    max_exposure_usdt: parseFloat($("#max-exposure").value),
    daily_loss_limit_usdt: parseFloat($("#daily-loss").value),
    max_positions: parseInt($("#max-positions").value, 10),
    per_symbol_exposure_usdt: parseFloat($("#per-symbol").value),
    slippage_bps: parseFloat($("#slippage").value),
    market_refresh_s: parseFloat($("#refresh").value),
    watchlist,
  };
  try {
    await api("/api/settings", { method: "POST", body: { config: payload } });
    toast("limits saved and validated server-side", "good");
    S.limitsRendered = false;
    await refreshState();
  } catch (error) {
    $("#limits-status").textContent = error.message;
    toast(error.message, "bad");
  }
}

async function exportAudit() {
  try {
    const data = await api("/api/audit?limit=500");
    const blob = new Blob([JSON.stringify(data.events, null, 2)], { type: "application/json" });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = "meme-terminal-audit.json";
    link.click();
    URL.revokeObjectURL(link.href);
    toast("audit exported (secrets redacted)", "good");
  } catch (error) { toast(error.message, "bad"); }
}

async function loadSymbols() {
  try {
    const data = await api("/api/symbols");
    const list = $("#symbol-list");
    list.textContent = "";
    (data.symbols || []).forEach((symbol) => {
      const option = document.createElement("option");
      option.value = symbol.symbol;
      list.appendChild(option);
    });
    S.symbolsLoaded = true;
    $("#symbol-hint").textContent = data.count + " Binance Spot USDT symbols available.";
  } catch (error) {
    $("#symbol-hint").textContent = "symbol list unavailable (offline?): " + error.message;
  }
}

/* ---------------------------------------------------------------- nav */
function showPage(name) {
  $$(".page").forEach((page) => page.classList.toggle("active", page.id === "page-" + name));
  $$(".tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.page === name));
  $$(".bottom-nav button").forEach((button) =>
    button.classList.toggle("active", button.dataset.page === name));
  if (name === "settings") loadAudit().catch(() => {});
  if (name === "dashboard") setTimeout(() => S.market && drawChart(S.market), 30);
}

/* ---------------------------------------------------------------- wiring */
document.addEventListener("DOMContentLoaded", () => {
  $$(".tab, .bottom-nav button").forEach((button) => {
    button.addEventListener("click", () => showPage(button.dataset.page));
  });

  $$("#interval-seg button").forEach((button) => {
    button.addEventListener("click", () => {
      $$("#interval-seg button").forEach((b) => b.classList.remove("on"));
      button.classList.add("on");
      S.interval = button.dataset.interval;
      refreshMarket().catch((error) => toast(error.message, "bad"));
    });
  });

  $$("#side-seg button").forEach((button) => {
    button.addEventListener("click", () => {
      $$("#side-seg button").forEach((b) => b.classList.remove("on"));
      button.classList.add("on");
      S.side = button.dataset.side;
    });
  });

  $$("#type-seg button").forEach((button) => {
    button.addEventListener("click", () => {
      $$("#type-seg button").forEach((b) => b.classList.remove("on"));
      button.classList.add("on");
      S.orderType = button.dataset.type;
      $("#limit-row").classList.toggle("hidden", S.orderType !== "LIMIT");
      $("#amount-asset").textContent = S.orderType === "LIMIT" ? "Qty (base)" : "USDT";
    });
  });

  $$("#amount-chips button").forEach((button) => {
    button.addEventListener("click", () => { $("#quote-amount").value = button.dataset.amount; });
  });

  $("#preview-btn").addEventListener("click", previewOrder);
  $("#symbol-input").addEventListener("change", () => setSymbol($("#symbol-input").value));
  $("#symbol-refresh").addEventListener("click", loadSymbols);
  $("#agents-run").addEventListener("click", runAgents);
  $("#agent-team-filter").addEventListener("change", renderAgentGrid);
  $("#agent-search").addEventListener("input", renderAgentGrid);
  $("#stop-btn").addEventListener("click", emergencyStop);
  $("#cred-form").addEventListener("submit", saveCredentials);
  $("#test-conn").addEventListener("click", testConnection);
  $("#delete-cred").addEventListener("click", deleteCredentials);
  $("#limits-form").addEventListener("submit", saveLimits);
  $("#audit-export").addEventListener("click", exportAudit);
  $("#backtest-form").addEventListener("submit", runBacktest);
  $("#live-activate").addEventListener("click", activateLive);
  $("#live-off").addEventListener("click", () => setMode("PAPER"));
  $$("#mode-seg button").forEach((button) => {
    button.addEventListener("click", () => setMode(button.dataset.mode));
  });

  const form = $("#login-form");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    $("#login-error").textContent = "";
    try {
      await login($("#access-code").value);
      $("#access-code").value = "";
    } catch (error) {
      $("#login-error").textContent = "Access code refused.";
    }
  });

  boot();
  if (S.registry) {
    const select = $("#agent-team-filter");
    const teams = Array.from(new Set(S.registry.map((a) => a.team)));
    teams.forEach((team) => {
      const option = document.createElement("option");
      option.value = team;
      option.textContent = team;
      select.appendChild(option);
    });
  }

  // Populate the team filter and symbol list once state arrives.
  setTimeout(() => {
    if (S.registry && $("#agent-team-filter").options.length <= 1) {
      Array.from(new Set(S.registry.map((a) => a.team))).forEach((team) => {
        const option = document.createElement("option");
        option.value = team;
        option.textContent = team;
        $("#agent-team-filter").appendChild(option);
      });
    }
    loadSymbols();
  }, 1200);
});
