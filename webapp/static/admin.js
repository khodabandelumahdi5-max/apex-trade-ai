// Admin panel (tab visible only when /api/session says admin; every endpoint re-checks on the server).
"use strict";
const A = { view: "overview", q: "", invStatus: "STARS" };

function adminSeg() {
  const views = [["overview", "Overview"], ["challenges", "Challenges"], ["users", "Users"], ["payments", "Payments"], ["log", "Log"]];
  return el("div", { class: "seg scroll" }, views.map(([v, label]) => el("button", { class: A.view === v ? "on" : "", text: label,
    onclick: () => { A.view = v; renderAdmin(); } })));
}
const when = (iso) => (iso ? new Date(iso).toLocaleString() : "–");

window.renderAdmin = async function renderAdmin() {
  const root = $("tab-admin"); root.replaceChildren(adminSeg());
  const box = el("div"); root.append(box);
  try {
    if (A.view === "overview") await adminOverview(box);
    if (A.view === "challenges") await adminChallenges(box);
    if (A.view === "users") await adminUsers(box);
    if (A.view === "payments") await adminPayments(box);
    if (A.view === "log") await adminLog(box);
  } catch (e) { box.append(el("div", { class: "banner bad", text: e.message })); }
};

async function adminOverview(box) {
  const k = await api("/api/admin/stats");
  const kpi = (label, value, cls) => el("div", { class: "kpi" }, el("b", { class: cls || "", text: value }), el("span", { class: "muted small", text: label }));
  box.append(el("div", { class: "card" }, el("h2", { text: "Overview" }), el("div", { class: "kpis" },
    kpi("users", String(k.users)), kpi("active subscriptions", String(k.active_subscriptions), "good"),
    kpi("revenue (30 days)", k.revenue_30d_usdt.toFixed(2) + " USDT"), kpi("revenue (total)", k.revenue_usdt.toFixed(2) + " USDT"),
    kpi("Stars (30 days)", "⭐ " + k.stars_30d), kpi("Stars (total)", "⭐ " + k.stars_total),
    kpi("pending invoices", String(k.pending_invoices), k.pending_invoices ? "warn" : ""), kpi("live challenges", String(k.live_challenges)),
    kpi("participants", String(k.participants)), kpi("disqualified", String(k.disqualified))),
    el("div", { class: "muted small", style: "margin-top:10px", text: `Telegram users ${k.telegram_users} · e-mail users ${k.email_users} · paid invoices ${k.paid_invoices}` })));
}

async function adminChallenges(box) {
  const f = {};
  const field = (key, label, value, type) => { f[key] = el("input", { value: String(value), inputmode: type === "text" ? null : "decimal" }); return el("div", {}, el("label", { text: label }), f[key]); };
  const form = el("div", { class: "card" }, el("h2", { text: "New challenge" }),
    field("title", "Title", "AlphaDuel 21-day", "text"), field("symbols", "Symbols (comma-separated, MEXC USDT pairs)", "SOL,JUP", "text"),
    el("div", { class: "two" }, field("days", "Days", 21), field("start_in_hours", "Starts in (hours)", 0)),
    field("prize", "Prize (paid by you, not from fees)", "", "text"),
    el("details", {}, el("summary", { text: "Rules" }),
      el("div", { class: "two" }, field("starting_balance", "Start balance $", 10000), field("fee_bps", "Fee (bps)", 10),
        field("max_risk_per_trade_pct", "Max risk/trade %", 1), field("daily_loss_limit_pct", "Daily loss DQ %", 3),
        field("max_drawdown_pct", "Drawdown DQ %", 10), field("max_consecutive_losses", "Losses → pause", 3),
        field("cooldown_hours", "Pause hours", 24), field("max_entries_per_day", "Entries/day", 5)),
      el("div", { class: "muted small", text: "0 switches a rule off." })),
    el("button", { class: "wide", style: "margin-top:12px", text: "Create challenge", onclick: async () => {
      const body = {}; for (const [k, input] of Object.entries(f)) body[k] = input.value;
      if (!(await confirmBox(`Create "${body.title}" (${body.symbols}, ${body.days} days)?`))) return;
      try { const r = await api("/api/admin/challenges", body); toast(`Challenge #${r.id} created`, "good"); renderAdmin(); refresh(); }
      catch (e) { toast(e.message, "bad"); } } }));
  const list = el("div", { class: "card" }, el("h2", { text: "All challenges" }));
  (await api("/api/admin/challenges")).forEach((c) => {
    const prize = el("input", { value: c.prize });
    list.append(el("div", { class: "list-item" },
      el("div", { class: "row between" }, el("b", { text: `#${c.id} ${c.title}` }), el("span", { class: "chip " + (c.final ? "muted" : "good"), text: c.final ? "final" : "open" })),
      el("div", { class: "muted small", text: `${c.symbols.join(", ")} · ${when(c.start)} → ${when(c.end)} · ${c.participants} traders` }),
      c.final ? null : el("div", { class: "inputrow", style: "margin-top:8px" }, prize, el("button", { class: "sm", text: "Save prize",
        onclick: async () => { try { await api(`/api/admin/challenges/${c.id}`, { prize: prize.value }); toast("Saved", "good"); } catch (e) { toast(e.message, "bad"); } } })),
      c.final ? null : el("button", { class: "ghost sm", style: "margin-top:8px", text: "End now", onclick: async () => {
        if (!(await confirmBox(`End #${c.id} now? The leaderboard becomes final within a minute.`))) return;
        try { await api(`/api/admin/challenges/${c.id}`, { end_now: true }); toast("Ending…", "good"); renderAdmin(); } catch (e) { toast(e.message, "bad"); } } })));
  });
  box.append(form, list);
}

async function adminUsers(box) {
  const q = el("input", { value: A.q, placeholder: "search name, e-mail, user id or Telegram id" });
  const go = () => { A.q = q.value; renderAdmin(); };
  q.addEventListener("keydown", (e) => e.key === "Enter" && go());
  const list = el("div", { class: "card" }, el("div", { class: "inputrow" }, q, el("button", { class: "sm", text: "Search", onclick: go })));
  (await api("/api/admin/users?q=" + encodeURIComponent(A.q))).forEach((u) => {
    const days = el("input", { value: "30", inputmode: "numeric", style: "max-width:90px" });
    list.append(el("div", { class: "list-item" },
      el("div", { class: "row between" }, el("b", { text: `${u.name} (#${u.id})` }), el("span", { class: "chip " + (!u.active ? "bad" : u.subscribed ? "good" : "muted"), text: !u.active ? "disabled" : u.subscribed ? "subscribed" : "free" })),
      el("div", { class: "muted small wrap", text: `${u.via}${u.email ? " · " + u.email : ""} · until ${when(u.expires)} · joined ${when(u.created)}${u.admin ? " · admin" : ""}` }),
      el("div", { class: "inputrow", style: "margin-top:8px" }, days,
        el("button", { class: "sm", text: "± days", onclick: async () => {
          const d = parseInt(days.value, 10);
          if (!(await confirmBox(`${d > 0 ? "Add" : "Remove"} ${Math.abs(d)} days for ${u.name}?`))) return;
          try { await api(`/api/admin/users/${u.id}/subscription`, { days: d, note: "admin panel" }); toast("Updated", "good"); renderAdmin(); } catch (e) { toast(e.message, "bad"); } } }),
        u.admin ? null : el("button", { class: (u.active ? "danger" : "ghost") + " sm", text: u.active ? "Disable" : "Enable", onclick: async () => {
          if (!(await confirmBox(`${u.active ? "Disable" : "Enable"} ${u.name}?`))) return;
          try { await api(`/api/admin/users/${u.id}/active`, { active: !u.active }); toast("Updated", "good"); renderAdmin(); } catch (e) { toast(e.message, "bad"); } } }))));
  });
  box.append(list);
}

async function adminPayments(box) {
  if (A.invStatus === "STARS") return adminStars(box);
  const seg = el("div", { class: "seg" }, ["STARS", "PENDING", "PAID", "EXPIRED", "ALL"].map((st) => el("button", { class: A.invStatus === st ? "on" : "", text: st.toLowerCase(),
    onclick: () => { A.invStatus = st; renderAdmin(); } })));
  const list = el("div", { class: "card" }, seg, el("div", { class: "muted small", style: "margin-bottom:6px", text: "Paid automatically when the exact amount arrives. For a wrong-amount or late payment, check it on Tronscan and attach the transaction hash." }));
  const rows = await api("/api/admin/invoices" + (A.invStatus === "ALL" ? "" : "?status=" + A.invStatus));
  if (!rows.length) list.append(el("div", { class: "muted", text: "Nothing here." }));
  rows.forEach((i) => {
    const tx = el("input", { placeholder: "64-character transaction hash" });
    list.append(el("div", { class: "list-item" },
      el("div", { class: "row between" }, el("b", { text: `${i.amount} USDT · ${i.user}` }), el("span", { class: "chip " + (i.status === "PAID" ? "good" : i.status === "PENDING" ? "warn" : "muted"), text: i.status.toLowerCase() })),
      el("div", { class: "muted small", text: `#${i.id} · ${i.days} days · created ${when(i.created)}${i.paid ? " · paid " + when(i.paid) : ""}` }),
      i.tx ? el("a", { class: "small", href: `https://tronscan.org/#/transaction/${i.tx}`, target: "_blank", rel: "noopener", text: "view transaction" }) : null,
      i.status === "PAID" ? null : el("details", {}, el("summary", { text: "Mark as paid manually" }),
        el("div", { class: "inputrow" }, tx, el("button", { class: "sm", text: "Confirm", onclick: async () => {
          if (!(await confirmBox(`Mark invoice #${i.id} paid and add ${i.days} days to ${i.user}?`))) return;
          try { await api(`/api/admin/invoices/${i.id}/mark_paid`, { tx: tx.value }); toast("Marked as paid", "good"); renderAdmin(); } catch (e) { toast(e.message, "bad"); } } })))));
  });
  box.append(list);
}

async function adminStars(box) {
  const seg = el("div", { class: "seg" }, ["STARS", "PENDING", "PAID", "EXPIRED", "ALL"].map((st) => el("button", { class: A.invStatus === st ? "on" : "", text: st.toLowerCase(),
    onclick: () => { A.invStatus = st; renderAdmin(); } })));
  const list = el("div", { class: "card" }, seg, el("div", { class: "muted small", style: "margin-bottom:6px", text: "Telegram Stars payments. A refund returns the Stars through Telegram and takes the paid days back." }));
  const rows = await api("/api/admin/stars");
  if (!rows.length) list.append(el("div", { class: "muted", text: "No Stars payments yet." }));
  rows.forEach((p) => list.append(el("div", { class: "list-item" },
    el("div", { class: "row between" }, el("b", { text: `⭐ ${p.stars} · ${p.user}` }), el("span", { class: "chip " + (p.refunded ? "muted" : "good"), text: p.refunded ? "refunded" : "paid" })),
    el("div", { class: "muted small", text: `#${p.id} · ${p.days} days · ${when(p.created)}${p.refunded ? " · refunded " + when(p.refunded) : ""}` }),
    p.refunded ? null : el("button", { class: "ghost sm", style: "margin-top:6px", text: "Refund", onclick: async () => {
      if (!(await confirmBox(`Refund ⭐ ${p.stars} to ${p.user} and remove ${p.days} days?`))) return;
      try { await api(`/api/admin/stars/${p.id}/refund`, {}); toast("Refunded", "good"); renderAdmin(); } catch (e) { toast(e.message, "bad"); } } }))));
  box.append(list);
}

async function adminLog(box) {
  const list = el("div", { class: "card" }, el("h2", { text: "Admin log" }));
  const rows = await api("/api/admin/audit");
  if (!rows.length) list.append(el("div", { class: "muted", text: "No admin actions yet." }));
  rows.forEach((a) => list.append(el("div", { class: "list-item" },
    el("div", { class: "row between" }, el("b", { text: `${a.action} ${a.target ? "#" + a.target : ""}` }), el("span", { class: "muted small", text: when(a.at) })),
    el("div", { class: "muted small wrap", text: `${a.admin} · ${JSON.stringify(a.details)}` }))));
  box.append(list);
}
