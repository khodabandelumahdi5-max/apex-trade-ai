// AlphaDuel Mini App. Text is only ever inserted with textContent (never innerHTML): names, titles and server
// messages cannot inject markup. Auth = Telegram's signed initData, sent as a header on every request.
"use strict";
const tg = window.Telegram && window.Telegram.WebApp;
if (tg) { tg.ready(); tg.expand(); }
const S = { session: null, challenges: [], cid: null, me: null, quotes: {}, symbol: null, tab: "trade", invoice: null };
const $ = (id) => document.getElementById(id);
function el(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") e.className = v; else if (k === "text") e.textContent = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v); else if (v !== false && v != null) e.setAttribute(k, v);
  }
  kids.flat().forEach((c) => c != null && e.appendChild(typeof c === "string" ? document.createTextNode(c) : c));
  return e;
}
const money = (x, d = 2) => "$" + Number(x).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
const pct = (x) => (x >= 0 ? "+" : "") + x.toFixed(2) + "%";
const px = (x) => x >= 100 ? x.toFixed(2) : x >= 1 ? x.toFixed(4) : x.toPrecision(4);
const haptic = (t) => { try { tg && tg.HapticFeedback.notificationOccurred(t); } catch (_) {} };
const challenge = () => S.challenges.find((c) => c.id === S.cid);

function toast(msg, kind) {
  const t = $("toast"); t.textContent = msg; t.className = kind || ""; t.style.display = "block";
  clearTimeout(toast.timer); toast.timer = setTimeout(() => (t.style.display = "none"), 4500);
  if (kind) haptic(kind === "good" ? "success" : "error");
}
function confirmBox(msg) {
  return new Promise((res) => (tg && tg.showConfirm ? tg.showConfirm(msg, res) : res(window.confirm(msg))));
}

async function api(path, body) {
  const opt = { headers: { "X-Telegram-Init-Data": tg ? tg.initData : "" } };
  if (body !== undefined) { opt.method = "POST"; opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch(path, opt);
  let data = null; try { data = await r.json(); } catch (_) {}
  if (!r.ok) { const e = new Error((data && data.error) || `error ${r.status}`); e.rule = data && data.rule; e.status = r.status; throw e; }
  return data;
}

// ------------------------------------------------------------------------------------------- chart
function chart(points) {
  const NS = "http://www.w3.org/2000/svg", svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", "0 0 300 100"); svg.setAttribute("preserveAspectRatio", "none"); svg.setAttribute("class", "chart");
  if (points.length < 2) {
    return el("div", { class: "muted small", style: "margin-top:10px", text: "Your equity chart appears after the first 30 minutes." });
  }
  const ys = points.map((p) => p[1]), lo = Math.min(...ys), hi = Math.max(...ys), span = hi - lo || 1;
  const xy = ys.map((y, i) => [(i / (ys.length - 1)) * 300, 92 - ((y - lo) / span) * 84]);
  const up = ys[ys.length - 1] >= ys[0], col = up ? "#2fbf71" : "#e5484d";
  const area = document.createElementNS(NS, "path");
  area.setAttribute("d", `M0,100 ${xy.map(([x, y]) => `L${x},${y}`).join(" ")} L300,100 Z`);
  area.setAttribute("fill", col); area.setAttribute("opacity", "0.12");
  const line = document.createElementNS(NS, "path");
  line.setAttribute("d", xy.map(([x, y], i) => `${i ? "L" : "M"}${x},${y}`).join(" "));
  line.setAttribute("fill", "none"); line.setAttribute("stroke", col); line.setAttribute("stroke-width", "2");
  svg.append(area, line);
  return svg;
}

// ------------------------------------------------------------------------------------------- data
async function loadSession() {
  try { S.session = await api("/api/session"); }
  catch (e) { S.session = null; }
  const chip = $("subchip");
  if (!S.session) { chip.textContent = "open from the bot"; chip.className = "chip muted"; return; }
  chip.textContent = S.session.subscribed ? "✓ subscribed" : "not subscribed";
  chip.className = "chip " + (S.session.subscribed ? "good" : "warn");
}
async function loadMe() {
  S.me = S.cid && S.session ? await api(`/api/me?challenge=${S.cid}`).catch(() => null) : null;
}
async function loadQuotes() {
  const c = challenge();
  if (!c || !c.running) return;
  try { S.quotes = await api(`/api/quotes?challenge=${c.id}`); } catch (_) { return; }
  updateLive();
}

// ------------------------------------------------------------------------------------------- trade tab
function rulesList(c) {
  const r = c.rules, li = [];
  li.push(`Start with ${money(c.starting_balance, 0)} virtual · fee ${c.fee_pct}% per trade · ${c.symbols.join(", ")}`);
  if (r.risk_per_trade_pct) li.push(`Every buy needs a stop-loss; max risk ${r.risk_per_trade_pct}% of equity per trade`);
  if (r.daily_loss_pct) li.push(`Daily loss over ${r.daily_loss_pct}% → disqualified`);
  if (r.max_drawdown_pct) li.push(`Drawdown over ${r.max_drawdown_pct}% from the peak → disqualified`);
  if (r.losses_before_pause) li.push(`${r.losses_before_pause} losses in a row → ${r.pause_hours} h without new entries`);
  if (r.entries_per_day) li.push(`Max ${r.entries_per_day} entries per day · stops can only be raised`);
  li.push(`Ranking: discipline score, then ROI · ends ${new Date(c.end).toLocaleString()}`);
  return el("ul", { class: "rules muted" }, li.map((t) => el("li", { text: t })));
}

function challengePicker() {
  if (S.challenges.length < 2) return null;
  const sel = el("select", { onchange: async (ev) => { S.cid = Number(ev.target.value); S.symbol = null; await refresh(); } },
    S.challenges.map((c) => { const o = el("option", { value: String(c.id), text: `#${c.id} ${c.title}${c.final ? " (final)" : ""}` }); if (c.id === S.cid) o.selected = true; return o; }));
  return el("div", { class: "card" }, sel);
}

function renderTrade() {
  const keep = { symbol: S.symbol, stop: $("stop") && $("stop").value, usd: $("usd") && $("usd").value };
  const root = $("tab-trade"); root.replaceChildren();
  renderTradeInto(root);
  if (keep.symbol === S.symbol && $("stop")) { $("stop").value = keep.stop || ""; $("usd").value = keep.usd || ""; }
}

function renderTradeInto(root) {
  const c = challenge();
  if (!c) { root.append(el("div", { class: "card muted", text: "No challenges yet. Check back soon." })); return; }
  const picker = challengePicker(); if (picker) root.append(picker);
  const head = el("div", { class: "card" },
    el("div", { class: "row between" }, el("h2", { text: c.title }), el("span", { class: "chip " + (c.running ? "good" : "muted"), text: c.final ? "final" : c.running ? "live" : "upcoming" })),
    el("div", { class: "muted", text: `Prize: ${c.prize || "-"}` }));
  root.append(head);

  if (!S.session) { root.append(el("div", { class: "banner warn", text: "Open AlphaDuel from the Telegram bot to trade." }), el("div", { class: "card" }, rulesList(c))); return; }
  const me = S.me;
  if (!me || !me.joined) {
    const card = el("div", { class: "card" }, el("h2", { text: "Rules" }), rulesList(c));
    if (!S.session.subscribed) card.append(el("button", { class: "wide", style: "margin-top:12px", text: "Subscribe to join", onclick: () => switchTab("account") }));
    else if (!c.final) card.append(el("button", { class: "wide", style: "margin-top:12px", text: "Join the challenge", onclick: joinChallenge }));
    root.append(card);
    return;
  }

  if (me.status === "DISQUALIFIED") root.append(el("div", { class: "banner bad", text: `Disqualified: ${me.reason || ""}` }));
  if (me.status === "COMPLETED") root.append(el("div", { class: "banner warn", text: "Challenge completed — well done for finishing!" }));
  if (me.cooldown_until && new Date(me.cooldown_until) > new Date())
    root.append(el("div", { class: "banner warn", text: `⏸ Pause after losses in a row: no new entries until ${new Date(me.cooldown_until).toLocaleString()}` }));

  const stat = (label, value, cls) => el("div", { class: "stat" }, el("b", { id: "stat-" + label.replace(/\W/g, ""), class: cls || "", text: value }), el("span", { class: "muted small", text: label }));
  root.append(el("div", { class: "card" },
    el("div", { class: "grid" },
      stat("rank", me.rank ? "#" + me.rank : "-"), stat("ROI", pct(me.roi), me.roi >= 0 ? "good" : "bad"),
      stat("discipline", me.discipline.toFixed(0) + "/100", me.discipline < 100 ? "warn" : ""),
      stat("equity", money(me.equity, 0)), stat("cash", money(me.cash, 0)), stat("max DD", me.max_dd.toFixed(2) + "%")),
    chart(me.curve),
    el("div", { class: "muted small", text: `W/L ${me.wins}/${me.losses} · entries today ${me.entries_today}${c.rules.entries_per_day ? "/" + c.rules.entries_per_day : ""} · rule breaks ${me.rule_breaks}` })));

  if (c.running && me.status === "ACTIVE") root.append(orderCard(c, me));
  if (me.positions.length) root.append(positionsCard(c, me));
}

function orderCard(c, me) {
  S.symbol = S.symbol && c.symbols.includes(S.symbol) ? S.symbol : c.symbols[0];
  const seg = el("div", { class: "seg" }, c.symbols.map((s) => el("button", { class: s === S.symbol ? "on" : "", text: s,
    onclick: () => { S.symbol = s; $("stop").value = ""; $("usd").value = ""; renderTrade(); updateLive(); } })));
  const stopIn = el("input", { id: "stop", inputmode: "decimal", placeholder: "stop-loss price", oninput: updateRisk });
  const usdIn = el("input", { id: "usd", inputmode: "decimal", placeholder: "amount in USD", oninput: updateRisk });
  const quick = el("div", { class: "actions" }, [2, 3, 5].map((d) => el("button", { class: "ghost sm", text: `stop −${d}%`,
    onclick: () => { const q = S.quotes[S.symbol]; if (q) { stopIn.value = px(q.ask * (1 - d / 100)); updateRisk(); } } })),
    el("button", { class: "ghost sm", text: "max size", onclick: fillMax }));
  return el("div", { class: "card" },
    el("div", { class: "row between" }, el("h2", { text: "Buy" }), el("span", { id: "live", class: "muted small", text: "loading price…" })),
    seg,
    el("label", { text: "Stop-loss (required)" }), stopIn,
    el("label", { text: "Amount (USD, fee included)" }), usdIn, quick,
    el("div", { id: "risk", class: "muted small", style: "margin:10px 0" }),
    el("button", { id: "buybtn", class: "wide", text: "Buy", onclick: placeBuy }));
}

function positionsCard(c, me) {
  const f = c.fee_pct / 100;
  const card = el("div", { class: "card" }, el("h2", { text: "Open positions" }));
  me.positions.forEach((p) => {
    const be = p.avg / (1 - f);                     // stop price that exits at ~0 after the exit fee
    card.append(el("div", { class: "pos" },
      el("div", { class: "row between" }, el("b", { text: p.symbol }), el("span", { class: "num", "data-upnl": p.symbol })),
      el("div", { class: "muted small", text: `${p.qty.toFixed(6)} @ avg ${px(p.avg)} · stop ${px(p.stop)} · risk ${money(Math.max(0, p.risk))}` }),
      el("div", { class: "actions" },
        el("button", { class: "ghost sm", text: "Stop → breakeven", "data-be": p.symbol, onclick: () => raiseStop(p.symbol, be) }),
        el("button", { class: "ghost sm", text: "Raise stop…", onclick: () => $("edit-" + p.symbol).classList.toggle("hidden") }),
        el("button", { class: "danger sm", text: "Sell all", onclick: () => sellAll(p.symbol) })),
      el("div", { id: "edit-" + p.symbol, class: "inputrow hidden", style: "margin-top:8px" },
        el("input", { id: "newstop-" + p.symbol, inputmode: "decimal", value: px(p.stop) }),
        el("button", { class: "sm", text: "Save", onclick: () => raiseStop(p.symbol, parseFloat($("newstop-" + p.symbol).value.replace(",", "."))) }))));
  });
  return card;
}

function updateLive() {
  const q = S.quotes[S.symbol]; const live = $("live");
  if (live && q) live.textContent = `bid ${px(q.bid)} · ask ${px(q.ask)}`;
  const me = S.me, c = challenge();
  // live equity / ROI from current bids (the server re-values everyone once a minute)
  if (me && me.joined && me.status === "ACTIVE" && c && me.positions.every((p) => S.quotes[p.symbol])) {
    const eq = me.cash + me.positions.reduce((a, p) => a + p.qty * S.quotes[p.symbol].bid, 0);
    const roi = (eq / c.starting_balance - 1) * 100;
    const eqEl = $("stat-equity"), roiEl = $("stat-ROI");
    if (eqEl) eqEl.textContent = money(eq, 0);
    if (roiEl) { roiEl.textContent = pct(roi); roiEl.className = roi >= 0 ? "good" : "bad"; }
  }
  if (me && me.positions && c) {
    me.positions.forEach((p) => {
      const pq = S.quotes[p.symbol]; const span = document.querySelector(`[data-upnl="${p.symbol}"]`);
      if (pq && span) {
        const upnl = p.qty * pq.bid * (1 - c.fee_pct / 100) - p.qty * p.avg;
        span.textContent = money(upnl); span.className = "num " + (upnl >= 0 ? "good" : "bad");
      }
      const beBtn = document.querySelector(`[data-be="${p.symbol}"]`);
      if (pq && beBtn) beBtn.disabled = !(p.avg / (1 - c.fee_pct / 100) < pq.bid * 0.9995 && p.avg / (1 - c.fee_pct / 100) > p.stop);
    });
  }
  updateRisk();
}

function updateRisk() {
  const out = $("risk"), c = challenge(), me = S.me, q = S.quotes[S.symbol];
  if (!out || !c || !me || !q) return;
  const stop = parseFloat(($("stop").value || "").replace(",", ".")), usd = parseFloat(($("usd").value || "").replace(/,/g, ""));
  const btn = $("buybtn");
  if (!(stop > 0) || !(usd > 0)) { out.textContent = `Risk budget per trade: ${money(me.risk_budget)}`; out.className = "muted small"; btn.disabled = true; return; }
  if (stop >= q.ask * 0.9995) { out.textContent = "Stop must be at least 0.05% below the price."; out.className = "bad small"; btn.disabled = true; return; }
  const f = c.fee_pct / 100, pos = me.positions.find((p) => p.symbol === S.symbol);
  const existing = pos ? pos.qty * pos.avg - pos.qty * stop * (1 - f) : 0;
  const risk = existing + usd * (1 - (1 - f) * (1 - f) * stop / q.ask);
  const share = (risk / me.equity) * 100, over = c.rules.risk_per_trade_pct && risk > me.risk_budget + 1e-9;
  out.textContent = `If the stop is hit you lose ≈ ${money(risk)} (${share.toFixed(2)}% of equity)` +
                    (over ? ` — over the ${c.rules.risk_per_trade_pct}% limit, this would count as a rule break` : "") +
                    (usd > me.cash ? " — more than your cash" : "");
  out.className = (over || usd > me.cash ? "bad" : "good") + " small";
  btn.disabled = usd > me.cash;
}

async function fillMax() {
  const stop = parseFloat(($("stop").value || "").replace(",", "."));
  if (!(stop > 0)) { toast("Set the stop-loss first"); return; }
  try {
    const r = await api("/api/max_buy", { challenge: S.cid, symbol: S.symbol, stop });
    $("usd").value = Math.floor(r.max_usd * 100) / 100; updateRisk();
  } catch (e) { toast(e.message, "bad"); }
}

async function placeBuy() {
  const stop = parseFloat(($("stop").value || "").replace(",", ".")), usd = parseFloat(($("usd").value || "").replace(/,/g, ""));
  if (!(await confirmBox(`Buy ${S.symbol} for ${money(usd)} with stop ${stop}?`))) return;
  const ok = await act({ challenge: S.cid, side: "BUY", symbol: S.symbol, usd, stop }, (r) => `Bought ${r.qty.toFixed(6)} ${S.symbol} @ ${px(r.price)}`);
  if (ok && $("usd")) { $("usd").value = ""; updateRisk(); }
}
async function sellAll(symbol) {
  if (!(await confirmBox(`Sell all ${symbol} at the market (bid)?`))) return;
  await act({ challenge: S.cid, side: "SELL", symbol }, (r) => `Sold @ ${px(r.price)} · P&L ${money(r.pnl)}${r.events.length ? " · " + r.events.join(" ") : ""}`);
}
async function raiseStop(symbol, price) {
  if (!(price > 0)) { toast("Enter a valid price"); return; }
  const v = Number(px(price));
  await act({ challenge: S.cid, symbol, stop: v }, () => `${symbol} stop raised to ${v}`, "/api/stop");
}
async function act(body, okText, path = "/api/order") {
  let ok = false;
  try { const r = await api(path, body); toast(okText(r), "good"); ok = true; }
  catch (e) { toast((e.rule ? "⛔ Rule: " : "⚠️ ") + e.message, "bad"); }
  await loadMe(); renderTrade(); updateLive();
  return ok;
}
async function joinChallenge() {
  try { await api("/api/join", { challenge: S.cid }); toast("You're in! Every buy needs a stop-loss.", "good"); }
  catch (e) { toast(e.message, "bad"); }
  await loadMe(); renderTrade(); loadQuotes();
}

// ------------------------------------------------------------------------------------------- leaderboard
async function renderBoard() {
  const root = $("tab-board"); root.replaceChildren();
  const c = challenge(); if (!c) { root.append(el("div", { class: "card muted", text: "No challenges yet." })); return; }
  const picker = challengePicker(); if (picker) root.append(picker);
  const b = await api(`/api/challenges/${c.id}/leaderboard`).catch(() => ({ rows: [], final: false }));
  const myName = S.session && S.session.name;
  const rows = b.rows.map((r) => el("tr", { class: r.name === myName ? "me" : "" },
    el("td", { text: r.rank ? String(r.rank) : r.status === "DISQUALIFIED" ? "DQ" : "–" }), el("td", { text: r.name }),
    el("td", { class: "num " + (r.roi >= 0 ? "good" : "bad"), text: pct(r.roi) }),
    el("td", { class: "num", text: r.discipline.toFixed(0) }), el("td", { class: "num", text: r.max_dd.toFixed(1) + "%" })));
  if (!rows.length) rows.push(el("tr", {}, el("td", { colspan: "5", class: "muted", text: "No traders yet — be the first." })));
  root.append(el("div", { class: "card" },
    el("div", { class: "row between" }, el("h2", { text: c.title }), el("span", { class: "chip muted", text: b.final ? "final" : "live" })),
    el("table", {}, el("thead", {}, el("tr", {}, ["#", "Trader", "ROI", "Disc.", "DD"].map((h, i) => el("th", { class: i > 1 ? "num" : "", text: h })))), el("tbody", {}, rows)),
    el("div", { class: "muted small", style: "margin-top:8px", text: "Ranked by discipline score, then ROI, then lower drawdown." })));
}

// ------------------------------------------------------------------------------------------- account
function copyRow(value) {
  return el("div", { class: "copy" }, el("code", { text: value }), el("button", { class: "sm", text: "Copy",
    onclick: async () => { try { await navigator.clipboard.writeText(value); toast("Copied", "good"); } catch (_) { toast("Long-press to copy"); } } }));
}
function renderAccount() {
  const root = $("tab-account"); root.replaceChildren();
  if (!S.session) { root.append(el("div", { class: "banner warn", text: "Open AlphaDuel from the Telegram bot to manage your account." })); return; }
  const s = S.session;
  root.append(el("div", { class: "card" }, el("h2", { text: s.name }),
    el("div", { class: s.subscribed ? "good" : "warn", text: s.subscribed ? `Subscription active until ${new Date(s.expires).toLocaleString()}` : "No active subscription" })));
  const pay = el("div", { class: "card" }, el("h2", { text: s.subscribed ? "Extend subscription" : "Subscribe" }));
  if (!s.payments) pay.append(el("div", { class: "muted", text: "Payments are not open yet." }));
  else if (!S.invoice) pay.append(el("div", { class: "muted", style: "margin-bottom:10px", text: "Pay with USDT on the TRON (TRC20) network. Paying early adds to your remaining time." }),
    el("button", { class: "wide", text: "Get payment details", onclick: getInvoice }));
  else {
    const inv = S.invoice;
    pay.append(el("div", { class: "muted", text: `${inv.days} days · send exactly` }), el("div", { class: "big", text: `${inv.amount} USDT` }), copyRow(inv.amount),
      el("div", { class: "muted", text: `Network: ${inv.network} only — to this address:` }), copyRow(inv.address),
      el("div", { class: "banner warn", text: "Send the exact amount, only USDT, only on TRON (TRC20). Another amount, token or network cannot be matched automatically and may be lost." }),
      el("div", { id: "countdown", class: "muted small" }),
      el("button", { class: "ghost wide", style: "margin-top:10px", text: "I've paid — check status", onclick: checkPaid }));
    tickCountdown();
  }
  root.append(pay, el("div", { class: "card muted small", text: "AlphaDuel is a paper-trading competition with a virtual balance. It is not investment advice and challenge results do not predict real trading results." }));
}
async function getInvoice() {
  try { S.invoice = await api("/api/invoice", {}); } catch (e) { toast(e.message, "bad"); }
  renderAccount();
}
function tickCountdown() {
  const c = $("countdown"); if (!c || !S.invoice) return;
  const ms = new Date(S.invoice.expires) - new Date();
  c.textContent = ms > 0 ? `Valid for ${Math.floor(ms / 3600000)} h ${Math.floor((ms % 3600000) / 60000)} min` : "Expired — get new payment details";
}
async function checkPaid() {
  const was = S.session && S.session.expires;
  await loadSession();
  if (S.session && S.session.expires !== was) { S.invoice = null; toast("Payment received — thank you!", "good"); renderAccount(); }
  else toast("Not seen yet. Confirmation takes 1–3 minutes after the transfer; you'll also get a bot message.");
}

// ------------------------------------------------------------------------------------------- shell
function switchTab(tab) {
  S.tab = tab;
  document.querySelectorAll("nav button").forEach((b) => b.classList.toggle("on", b.dataset.tab === tab));
  ["trade", "board", "account"].forEach((t) => $("tab-" + t).classList.toggle("hidden", t !== tab));
  if (tab === "board") renderBoard(); if (tab === "account") renderAccount(); if (tab === "trade") { renderTrade(); updateLive(); }
}
async function refresh() {
  S.challenges = await api("/api/challenges").catch(() => []);
  if (!S.challenges.some((c) => c.id === S.cid)) {
    const live = S.challenges.find((c) => c.running) || S.challenges[0];
    S.cid = live ? live.id : null;
  }
  await loadMe(); switchTab(S.tab); loadQuotes();
}
document.querySelectorAll("nav button").forEach((b) => b.addEventListener("click", () => switchTab(b.dataset.tab)));
(async function init() {
  await loadSession(); await refresh();
  setInterval(() => { if (!document.hidden && S.tab === "trade") loadQuotes(); }, 5000);
  setInterval(async () => { if (document.hidden) return; await loadMe(); if (S.tab === "trade" && !document.activeElement.matches("input")) { renderTrade(); updateLive(); } if (S.tab === "board") renderBoard(); }, 60000);
  setInterval(tickCountdown, 30000);
})();
