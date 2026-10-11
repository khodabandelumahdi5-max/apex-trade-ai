// All text is inserted with textContent (never innerHTML), so names and titles cannot inject markup.
"use strict";
const tg = window.Telegram && window.Telegram.WebApp;
if (tg) { tg.ready(); tg.expand(); }
const $ = (id) => document.getElementById(id);
const el = (tag, text, cls) => { const e = document.createElement(tag); if (text !== undefined) e.textContent = text; if (cls) e.className = cls; return e; };
const pct = (x) => (x >= 0 ? "+" : "") + x.toFixed(2) + "%";
let challenges = [];

async function api(path) {
  const r = await fetch(path, { headers: { "X-Telegram-Init-Data": tg ? tg.initData : "" } });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

function sparkline(points) {
  const svgNS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(svgNS, "svg");
  svg.setAttribute("viewBox", "0 0 300 100");
  svg.setAttribute("preserveAspectRatio", "none");
  if (points.length < 2) return svg;
  const ys = points.map((p) => p[1]);
  const lo = Math.min(...ys), hi = Math.max(...ys), span = hi - lo || 1;
  const d = ys.map((y, i) => `${i ? "L" : "M"}${(i / (ys.length - 1)) * 300},${95 - ((y - lo) / span) * 90}`).join(" ");
  const path = document.createElementNS(svgNS, "path");
  path.setAttribute("d", d);
  path.setAttribute("fill", "none");
  path.setAttribute("stroke", ys[ys.length - 1] >= ys[0] ? "#2fbf71" : "#e5484d");
  path.setAttribute("stroke-width", "2");
  svg.appendChild(path);
  return svg;
}

function renderRules(c) {
  const ul = $("rules"); ul.replaceChildren();
  const r = c.rules;
  const lines = [`Symbols: ${c.symbols.join(", ")} · ends ${new Date(c.end).toLocaleString()}`,
                 `Prize: ${c.prize || "-"}`];
  if (r.risk_per_trade_pct) lines.push(`Max risk per trade ${r.risk_per_trade_pct}% — every buy needs a stop-loss`);
  if (r.daily_loss_pct) lines.push(`Daily loss over ${r.daily_loss_pct}% → disqualified`);
  if (r.max_drawdown_pct) lines.push(`Drawdown over ${r.max_drawdown_pct}% → disqualified`);
  if (r.losses_before_pause) lines.push(`${r.losses_before_pause} losses in a row → ${r.pause_hours} h pause`);
  if (r.entries_per_day) lines.push(`Max ${r.entries_per_day} entries per day`);
  lines.forEach((t) => ul.appendChild(el("li", t)));
}

async function renderMe(cid) {
  const box = $("me"); box.replaceChildren(el("div", "Loading…", "muted"));
  try {
    const m = await api(`/api/me?challenge=${cid}`);
    box.replaceChildren();
    if (!m.joined) {
      box.appendChild(el("div", m.subscribed ? `You have not joined yet. In the bot: /join ${cid}`
                                             : "Subscribe in the bot (/subscribe), then /join " + cid));
      return;
    }
    box.appendChild(el("div", `${m.status}${m.reason ? " — " + m.reason : ""}` +
                              (m.cooldown_until ? ` · paused until ${new Date(m.cooldown_until).toLocaleString()}` : ""), "muted"));
    const g = el("div", undefined, "grid");
    const stat = (label, value, cls) => { const s = el("div", undefined, "stat"); s.appendChild(el("b", value, cls)); s.appendChild(el("span", label, "muted")); g.appendChild(s); };
    stat("rank", m.rank ? "#" + m.rank : "-");
    stat("ROI", pct(m.roi), m.roi >= 0 ? "good" : "bad");
    stat("discipline", m.discipline.toFixed(0) + "/100");
    stat("equity", "$" + m.equity.toLocaleString(undefined, { maximumFractionDigits: 0 }));
    stat("max DD", m.max_dd.toFixed(2) + "%");
    stat("W / L", `${m.wins} / ${m.losses}`);
    box.appendChild(g);
    box.appendChild(sparkline(m.curve));
    m.positions.forEach((p) => box.appendChild(el("div", `${p.symbol}: ${p.qty.toFixed(6)} · avg ${p.avg.toPrecision(6)} · stop ${p.stop}`, "muted")));
  } catch (e) {
    box.replaceChildren(el("div", tg && tg.initData ? "Could not load your stats." : "Open this page from the Telegram bot to see your stats.", "muted"));
  }
}

async function renderBoard(cid) {
  const tb = $("board"); tb.replaceChildren();
  const b = await api(`/api/challenges/${cid}/leaderboard`);
  $("board-title").textContent = `Leaderboard ${b.final ? "(final)" : "(live)"}`;
  b.rows.forEach((r) => {
    const tr = el("tr");
    tr.appendChild(el("td", r.rank ? String(r.rank) : (r.status === "DISQUALIFIED" ? "DQ" : "-")));
    tr.appendChild(el("td", r.name));
    tr.appendChild(el("td", pct(r.roi), "num " + (r.roi >= 0 ? "good" : "bad")));
    tr.appendChild(el("td", r.discipline.toFixed(0), "num"));
    tr.appendChild(el("td", r.max_dd.toFixed(1) + "%", "num"));
    tb.appendChild(tr);
  });
  if (!b.rows.length) { const tr = el("tr"); const td = el("td", "No traders yet", "muted"); td.colSpan = 5; tr.appendChild(td); tb.appendChild(tr); }
}

async function show(cid) {
  const c = challenges.find((x) => x.id === cid);
  if (!c) return;
  renderRules(c);
  await Promise.all([renderMe(cid), renderBoard(cid)]);
}

(async function init() {
  challenges = await api("/api/challenges");
  const sel = $("challenge");
  challenges.forEach((c) => { const o = el("option", `#${c.id} ${c.title}${c.final ? " (final)" : ""}`); o.value = String(c.id); sel.appendChild(o); });
  sel.addEventListener("change", () => show(Number(sel.value)));
  if (challenges.length) await show(challenges[0].id);
  else $("me").replaceChildren(el("div", "No challenges yet.", "muted"));
  setInterval(() => sel.value && show(Number(sel.value)), 60000);
})();
