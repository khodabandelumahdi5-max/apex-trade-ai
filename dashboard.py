"""Apex Trade AI — Streamlit monitoring & control dashboard.

Reads the same database as the engine (main.py). Controls are written to the `control_state`
table and picked up by the engine's guard loop within ~5 seconds."""
from __future__ import annotations

import asyncio
import threading
from concurrent.futures import Future
from datetime import datetime, timedelta, timezone
from typing import Any, Coroutine, TypeVar

import pandas as pd
import streamlit as st

from agents.risk_agent import HARD_MAX_RISK_PCT
from config import get_settings
from database import repository as repo
from database.connection import init_db
from database.models import (AgentDecision, PortfolioSnapshot, ProtectionStatus, SystemLog,
                             WhaleTransaction)

T = TypeVar("T")
TARGET_WIN_RATE = 70.0
HEARTBEAT_STALE_SEC = 300
PROTECTION_LABEL = {
    ProtectionStatus.INITIAL_STOP: "🛡️ Initial Stop",
    ProtectionStatus.BREAKEVEN_LOCKED: "🔒 Breakeven Locked",
    ProtectionStatus.TRAILING: "📈 Trailing (profit locked)",
}

st.set_page_config(page_title="Apex Trade AI", page_icon="⚡", layout="wide")
st.markdown("""
<style>
  .block-container {padding-top: 1.2rem;}
  div[data-testid="stMetric"] {background:#131a22;border:1px solid #1f2a36;border-radius:10px;padding:12px 16px;}
  div[data-testid="stMetricLabel"] p {color:#8b98a5;font-size:0.8rem;text-transform:uppercase;letter-spacing:.04em;}
  .agent-card {background:#131a22;border:1px solid #1f2a36;border-radius:10px;padding:10px 14px;}
  .agent-card b {font-size:0.95rem;}
  .ok {color:#22c55e;} .warn {color:#f59e0b;} .bad {color:#ef4444;} .muted {color:#8b98a5;font-size:0.8rem;}
</style>""", unsafe_allow_html=True)


# ---------------------------------------------------------------- async bridge
@st.cache_resource
def _loop() -> asyncio.AbstractEventLoop:
    """One long-lived event loop in a daemon thread: async DB engines are loop-bound."""
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True, name="dashboard-db").start()
    settings = get_settings()
    asyncio.run_coroutine_threadsafe(init_db(settings.database_url), loop).result(timeout=60)
    asyncio.run_coroutine_threadsafe(
        repo.get_control(settings.max_risk_per_trade_pct, settings.kelly_fraction), loop).result(timeout=30)
    return loop


def run(coro: Coroutine[Any, Any, T]) -> T:
    fut: Future[T] = asyncio.run_coroutine_threadsafe(coro, _loop())
    return fut.result(timeout=30)


try:
    settings = get_settings()
    _loop()
except Exception as exc:
    st.error(f"Cannot connect to the database / configuration error: {exc}")
    st.stop()


def _ago(ts: datetime | None) -> float:
    if ts is None:
        return float("inf")
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds()


# ---------------------------------------------------------------- live panel
@st.fragment(run_every=timedelta(seconds=5))
def live_panel() -> None:
    try:
        control = run(repo.get_control(settings.max_risk_per_trade_pct, settings.kelly_fraction))
        open_trades = run(repo.open_trades())
        closed = run(repo.closed_trades(1000))
        snaps: list[PortfolioSnapshot] = list(run(repo.recent(PortfolioSnapshot, 2000)))
        beats = run(repo.heartbeats())
        whales: list[WhaleTransaction] = list(run(repo.recent(WhaleTransaction, 60)))
        decisions: list[AgentDecision] = list(run(repo.recent(AgentDecision, 60)))
        logs: list[SystemLog] = list(run(repo.recent(SystemLog, 40)))
    except Exception as exc:
        st.error(f"Database query failed: {exc}")
        return

    mode = ("🔴 LIVE" if settings.is_live else "🧪 PAPER") + f" · {settings.execution_venue.upper()}"
    state = "⛔ HALTED" if control.halted else "🟢 RUNNING"
    engine_age = min((_ago(b.last_seen) for b in beats), default=float("inf"))
    engine = "online" if engine_age < max(3 * settings.loop_interval_sec, 180) else "no recent heartbeat"
    st.markdown(f"### ⚡ Apex Trade AI &nbsp; `{mode}` &nbsp; `{state}` &nbsp; "
                f"<span class='muted'>engine {engine} · {datetime.now(timezone.utc):%H:%M:%S} UTC</span>",
                unsafe_allow_html=True)
    if control.halted and control.reason:
        st.warning(f"Halt reason: {control.reason}")

    # agents
    cols = st.columns(max(len(beats), 1))
    for col, b in zip(cols, beats):
        age = _ago(b.last_seen)
        # risk/execution only report when a trade is evaluated: silence there means idle, not broken
        event_driven = b.agent in ("risk", "execution")
        stale = age > HEARTBEAT_STALE_SEC and not event_driven
        css = "bad" if b.status in ("FAILED", "STOPPED") or stale else \
            "warn" if b.status in ("DEGRADED", "STARTING") else "muted" if event_driven and \
            age > HEARTBEAT_STALE_SEC else "ok"
        latency = f"{b.last_latency_ms:.0f} ms" if b.last_latency_ms else "–"
        detail = (b.last_error or b.detail or "")[:80]
        col.markdown(f"<div class='agent-card'><b>{b.agent.upper()}</b> <span class='{css}'>● {b.status}</span><br>"
                     f"<span class='muted'>{age:.0f}s ago · {latency}</span><br>"
                     f"<span class='muted'>{detail}</span></div>", unsafe_allow_html=True)
    if not beats:
        st.info("No agent heartbeats yet — start the engine with `python main.py`.")

    # KPIs
    last = snaps[0] if snaps else None
    equity = last.equity_usd if last else settings.initial_capital_usd
    pnl_pct = (equity / settings.initial_capital_usd - 1) * 100
    wins = sum(1 for t in closed if t.pnl > 0)
    win_rate = wins / len(closed) * 100 if closed else None
    protected = sum(t.protection_status != ProtectionStatus.INITIAL_STOP for t in open_trades)
    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric("Total Portfolio Balance", f"${equity:,.2f}", f"{pnl_pct:+.2f}% since start")
    k2.metric("Win Rate / Target", f"{win_rate:.1f}%" if win_rate is not None else "n/a",
              f"target {TARGET_WIN_RATE:.0f}% · {len(closed)} closed", delta_color="off")
    k3.metric("Active Protected Trades", f"{protected} / {len(open_trades)}", "breakeven or trailing",
              delta_color="off")
    k4.metric("Portfolio VaR (1d)", f"${last.var_usd:,.2f}" if last else "$0.00",
              f"{(last.var_usd / equity * 100) if last and equity else 0:.2f}% of equity · limit "
              f"{settings.max_portfolio_var_pct}%", delta_color="off")
    k5.metric("Current Drawdown", f"{last.drawdown_pct:.2f}%" if last else "0.00%",
              f"halt at {settings.max_drawdown_pct}%", delta_color="off")

    # positions
    st.markdown("#### Live positions")
    if open_trades:
        rows = []
        for t in open_trades:
            px = t.last_price or t.entry_price
            rows.append({"#": t.id, "Symbol": t.symbol, "Venue": t.venue, "Size": round(t.size, 6),
                         "Entry": t.entry_price, "Last": px, "Stop": round(t.stop_loss, 6),
                         "TP1": ("✅ hit" if t.tp1_hit else (round(t.tp1, 6) if t.tp1 else "–")),
                         "TP2": round(t.tp2, 6) if t.tp2 else "–",
                         "PnL ($)": round((px - t.entry_price) * t.size - t.fees_usd + (t.realized_partial or 0), 2),
                         "PnL (%)": round((px / t.entry_price - 1) * 100, 2),
                         "Risk left ($)": round(max(0.0, (t.entry_price - t.stop_loss) * t.size), 2),
                         "Protection": PROTECTION_LABEL[t.protection_status],
                         "Opened": t.timestamp.strftime("%m-%d %H:%M")})
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.caption("No open positions.")

    # latest technical decision per symbol = the current trade plan
    plan_rows, seen = [], set()
    for d in decisions:
        if d.agent != "technical" or d.symbol in seen:
            continue
        seen.add(d.symbol)
        p = d.payload or {}
        price = (p.get("timeframes", {}).get("1h", {}) or {}).get("close")
        plan_rows.append({"Symbol": d.symbol, "Signal": d.decision, "Confidence": round(d.confidence or 0, 2),
                          "Price": price, "Stop": p.get("suggested_stop"), "TP1": p.get("tp1"), "TP2": p.get("tp2"),
                          "RSI 1h": round(p.get("rsi_14") or 0, 1), "Updated": d.timestamp.strftime("%H:%M:%S")})
    st.markdown("#### Trade plan (latest signal per coin)")
    if plan_rows:
        st.dataframe(pd.DataFrame(plan_rows), use_container_width=True, hide_index=True)
        st.caption("A BUY here opens a position only if the on-chain agent agrees and the risk agent approves.")
    else:
        st.caption("Waiting for the first technical analysis…")

    left, right = st.columns([3, 2])
    with left:
        st.markdown("#### Equity curve")
        if len(snaps) > 1:
            curve = pd.DataFrame([{"time": s.timestamp, "Equity": s.equity_usd} for s in reversed(snaps)])
            st.line_chart(curve.set_index("time"), height=260)
        else:
            st.caption("Waiting for portfolio snapshots…")
        st.markdown("#### Closed trades")
        if closed:
            st.dataframe(pd.DataFrame([{
                "#": t.id, "Symbol": t.symbol, "Entry": t.entry_price, "Exit": t.exit_price,
                "PnL ($)": round(t.pnl, 2), "Reason": t.exit_reason,
                "Closed": t.closed_at.strftime("%m-%d %H:%M") if t.closed_at else ""}
                for t in closed[:25]]), use_container_width=True, hide_index=True, height=240)
        else:
            st.caption("No closed trades yet.")
    with right:
        st.markdown("#### 🐋 On-chain whale radar")
        if whales:
            for w in whales[:25]:
                icon = "🟢" if w.sentiment == "ACCUMULATION" else "🔴" if w.sentiment == "DISTRIBUTION" else "⚪"
                who = f" · `{w.wallet[:4]}…{w.wallet[-4:]}`" if w.wallet else ""
                st.markdown(f"{icon} `{w.timestamp:%H:%M:%S}` **{w.token}** {w.net_flow_usd:+,.0f} USD "
                            f"· {w.sentiment.lower()} · {w.method}{who}")
        else:
            st.caption("No whale flow recorded yet (the on-chain agent needs ~10 min of history).")

    with st.expander("Agent decisions", expanded=False):
        st.dataframe(pd.DataFrame([{"time": d.timestamp.strftime("%H:%M:%S"), "agent": d.agent, "symbol": d.symbol,
                                    "decision": d.decision,
                                    "confidence": None if d.confidence is None else round(d.confidence, 3)}
                                   for d in decisions]), use_container_width=True, hide_index=True)
    with st.expander("System log (WARNING+)", expanded=False):
        for lg in logs:
            st.text(f"{lg.timestamp:%m-%d %H:%M:%S} {lg.level:<8} {lg.message[:300]}")


live_panel()

# ---------------------------------------------------------------- controls
st.divider()
st.markdown("### 🚨 Emergency Control Center")
control = run(repo.get_control(settings.max_risk_per_trade_pct, settings.kelly_fraction))
c1, c2 = st.columns(2)
with c1:
    confirm = st.checkbox("I confirm: close ALL open positions at market and halt new entries")
    if st.button("🚨 EMERGENCY HALT", type="primary", disabled=not confirm, use_container_width=True):
        run(repo.update_control(halted=True, close_all_requested=True, reason="Emergency halt from dashboard"))
        st.error("Halt signal sent. The engine closes all positions within ~5 s; watch the positions table.")
    if control.halted and st.button("▶️ Resume trading", use_container_width=True):
        run(repo.update_control(halted=False, close_all_requested=False, reason=None))
        st.success("Resumed: new entries allowed from the next cycle.")
        st.rerun()
with c2:
    with st.form("risk_form"):
        risk = st.slider("Max risk per trade (% of equity)", 0.25, HARD_MAX_RISK_PCT, float(control.risk_pct), 0.05)
        kelly = st.slider("Kelly fraction", 0.05, 1.0, float(control.kelly_fraction), 0.05,
                          help="Share of full-Kelly used. Effective risk = min(slider cap, fraction × Kelly).")
        if st.form_submit_button("Apply risk settings", use_container_width=True):
            run(repo.update_control(risk_pct=risk, kelly_fraction=kelly))
            st.success(f"Applied: risk cap {risk:.2f}%, Kelly fraction {kelly:.2f}")
    st.caption(f"Hard ceiling {HARD_MAX_RISK_PCT}% per trade is enforced in the risk agent regardless of this slider.")
