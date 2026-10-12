"""Signal logic without I/O (deterministic, tested). Messages are Persian, HTML-escaped, for parse_mode=HTML.

The exit rules are the engine's own (agents.risk_agent.manage_position): stop, breakeven after +1.5 % or TP1,
trailing 2 %, TP1 = 1.5R (sell half), TP2 = 3R. On top of that the trader gets early warnings when the price has
covered 50 % and 80 % of the way from entry to the initial stop.
"""
from __future__ import annotations

import html
import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from agents.risk_agent import loss_per_unit, manage_position, take_profit_price
from config import Settings
from core.state import PositionState
from database.models import SignalStatus, TradeSignal

e = html.escape
DANGER_LEVELS = (50, 80)          # % of the distance entry → stop
DANGER_RESET = 25                 # back above this → the warnings can fire again
STOP_NOTIFY_STEP = 0.003          # notify a raised stop only when it moved ≥ 0.3 %


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


@dataclass
class SignalConfig:
    symbols: list[str]
    budget_usd: float = 100.0
    risk_pct: float = 1.0
    fee_pct: float = 0.1                # per side, MEXC spot taker is lower; conservative
    scan_minutes: float = 5.0
    watch_seconds: float = 20.0
    idea_ttl_hours: float = 3.0
    max_chase_pct: float = 1.0
    cooldown_hours: float = 12.0
    min_confidence: float = 0.0         # extra filter on top of the agent's own BUY threshold

    @classmethod
    def from_env(cls) -> "SignalConfig":
        syms = [x.strip().upper() for x in os.getenv("SIGNAL_SYMBOLS", "BTC,ETH,SOL,JUP").split(",") if x.strip()]
        return cls(symbols=list(dict.fromkeys(syms)), budget_usd=_f("SIGNAL_BUDGET_USD", 100),
                   risk_pct=min(_f("SIGNAL_RISK_PCT", 1.0), 2.0), fee_pct=_f("SIGNAL_FEE_PCT", 0.1),
                   scan_minutes=max(_f("SIGNAL_SCAN_MINUTES", 5), 1), watch_seconds=max(_f("SIGNAL_WATCH_SECONDS", 20), 5),
                   idea_ttl_hours=_f("SIGNAL_IDEA_TTL_HOURS", 3), max_chase_pct=_f("SIGNAL_MAX_CHASE_PCT", 1.0),
                   cooldown_hours=_f("SIGNAL_COOLDOWN_HOURS", 12), min_confidence=_f("SIGNAL_MIN_CONFIDENCE", 0))


@dataclass
class Alert:
    kind: str
    text: str
    signal_id: int
    buttons: list[tuple[str, str]] = field(default_factory=list)   # (label, callback_data)


def px(x: float | None) -> str:
    """Price with enough decimals for its size, trailing zeros removed (96.0000 → 96.00, 0.348100 → 0.3481)."""
    if x is None:
        return "-"
    decimals = 2 if x >= 100 else 4 if x >= 1 else max(4, 3 - math.floor(math.log10(x))) if x > 0 else 2
    text = f"{x:,.{decimals}f}"
    if "." in text:
        whole, frac = text.split(".")
        frac = frac.rstrip("0").ljust(2, "0")
        text = f"{whole}.{frac}"
    return text


def pct(a: float, b: float) -> str:
    return f"{(a / b - 1) * 100:+.2f}%"


def tv_link(symbol: str) -> str:
    return f"https://www.tradingview.com/chart/?symbol=MEXC:{symbol.upper()}USDT"


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def position_size(cfg: SignalConfig, entry: float, stop: float) -> tuple[float, float, float]:
    """(qty, usd, loss_at_stop) so that the loss at the stop incl. both fees ≤ risk_pct of the budget,
    and never more than the whole budget (spot: no leverage)."""
    per_unit = loss_per_unit(entry, stop, cfg.fee_pct / 100)
    if per_unit <= 0 or entry <= 0:
        return 0.0, 0.0, 0.0
    qty = min(cfg.budget_usd * cfg.risk_pct / 100 / per_unit, cfg.budget_usd / (entry * (1 + cfg.fee_pct / 100)))
    return qty, qty * entry, qty * per_unit


def new_signal(symbol: str, ask: float, stop: float, confidence: float, reasons: list[str], settings: Settings,
               cfg: SignalConfig, now: datetime) -> TradeSignal | None:
    """None when the plan is not tradable (stop too tight / too wide, nothing fits the budget)."""
    if not (0 < stop < ask) or (ask - stop) / ask < 0.005 or (ask - stop) / ask > 0.15:
        return None
    qty, usd, _ = position_size(cfg, ask, stop)
    if usd < 5:
        return None
    return TradeSignal(symbol=symbol, status=SignalStatus.IDEA.value, created_at=now, confidence=confidence,
                       entry=ask, initial_stop=stop, stop=stop, tp1=take_profit_price(ask, stop, settings.tp1_r),
                       tp2=take_profit_price(ask, stop, settings.tp2_r), qty=qty, reasons="; ".join(reasons),
                       highest=ask, protection="INITIAL_STOP", tp1_hit=False, danger_level=0, flags={})


def signal_text(sig: TradeSignal, cfg: SignalConfig) -> str:
    qty, usd, loss = position_size(cfg, sig.entry, sig.initial_stop)
    r = sig.entry - sig.initial_stop
    lines = [
        f"🟢 <b>سیگنال خرید #{sig.id} — {e(sig.symbol)}/USDT</b>  (اطمینان {sig.confidence:.2f})",
        f"ورود: حدود <b>{px(sig.entry)}</b> (حداکثر تا {px(sig.entry * (1 + cfg.max_chase_pct / 100))}، بالاتر نخر)",
        f"حد ضرر: <b>{px(sig.initial_stop)}</b> ({pct(sig.initial_stop, sig.entry)})",
    ]
    if sig.tp1:
        lines.append(f"هدف ۱: <b>{px(sig.tp1)}</b> ({pct(sig.tp1, sig.entry)}) → نصف را بفروش، حد ضرر به نقطه‌ی ورود")
    if sig.tp2:
        lines.append(f"هدف ۲: <b>{px(sig.tp2)}</b> ({pct(sig.tp2, sig.entry)}) → بقیه را بفروش")
    lines += [
        f"حجم پیشنهادی با بودجه‌ی ${cfg.budget_usd:,.0f}: <b>{qty:.6f} {e(sig.symbol)}</b> ≈ ${usd:,.2f}",
        f"اگر حد ضرر بخورد: حدود <b>-${loss:,.2f}</b> ({cfg.risk_pct:g}% بودجه، با کارمزد) · ریسک به سود ۱:{(sig.tp2 - sig.entry) / r:.1f}"
        if sig.tp2 else f"اگر حد ضرر بخورد: حدود -${loss:,.2f}",
        f"📈 <a href=\"{tv_link(sig.symbol)}\">چارت در TradingView</a> (اسکریپت Apex در TradingView همین قوانین را روی چارت نشان می‌دهد)",
        "",
        "⚠️ بعد از خرید، <b>حد ضرر را همان لحظه در صرافی ثبت کن</b>. اگر لپ‌تاپ یا اینترنت قطع شود، هشدارها نمی‌رسند.",
        f"اعتبار تا {cfg.idea_ttl_hours:g} ساعت. اطلاعات آموزشی است، توصیه‌ی مالی نیست.",
    ]
    return "\n".join(lines)


def _close(sig: TradeSignal, status: SignalStatus, reason: str, price: float | None, now: datetime) -> None:
    sig.status, sig.close_reason, sig.exit_price, sig.closed_at = status.value, reason, price, now


def evaluate_idea(sig: TradeSignal, bid: float, ask: float, cfg: SignalConfig, now: datetime) -> list[Alert]:
    """A signal the trader has not entered yet."""
    if bid <= sig.initial_stop:
        _close(sig, SignalStatus.EXPIRED, "invalidated", bid, now)
        return [Alert("invalidated", f"❌ سیگنال #{sig.id} {e(sig.symbol)} قبل از ورود باطل شد: قیمت به حد ضرر "
                                     f"{px(sig.initial_stop)} رسید. <b>وارد نشو.</b>", sig.id)]
    if now - _aware(sig.created_at) > timedelta(hours=cfg.idea_ttl_hours):
        _close(sig, SignalStatus.EXPIRED, "expired", bid, now)
        return [Alert("expired", f"⌛ سیگنال #{sig.id} {e(sig.symbol)} منقضی شد (وارد نشدی). موقعیت بعدی را صبر کن.",
                      sig.id)]
    flags = dict(sig.flags or {})
    if ask > sig.entry * (1 + cfg.max_chase_pct / 100) and not flags.get("chase"):
        flags["chase"] = True
        sig.flags = flags
        return [Alert("chase", f"🏃 قیمت {e(sig.symbol)} الان {px(ask)} است ({pct(ask, sig.entry)} بالاتر از ورود "
                               f"سیگنال #{sig.id}). <b>دنبال قیمت نخر</b>: نسبت ریسک به سود خراب می‌شود.", sig.id)]
    return []


def evaluate_position(sig: TradeSignal, bid: float, settings: Settings, now: datetime) -> list[Alert]:
    """A position the trader entered: early warnings, stop moves, targets, exits."""
    entry = sig.user_entry or sig.entry
    alerts: list[Alert] = []
    pos = PositionState(trade_id=sig.id, symbol=sig.symbol, mint=None, venue="paper", entry_price=entry, size=1.0,
                        stop_loss=sig.stop, highest_price=max(sig.highest or entry, entry), last_price=bid,
                        protection_status=sig.protection, initial_stop=sig.initial_stop, tp1_hit=sig.tp1_hit)
    upd = manage_position(pos, bid, settings)
    sig.highest = upd.highest_price
    sig.protection = upd.protection_status.value
    result = pct(bid, entry)
    exit_btn = [("✅ فروختم", f"sig:exit:{sig.id}")]

    if upd.exit:
        _close(sig, SignalStatus.CLOSED, upd.reason or "exit", bid, now)
        sig.stop = upd.stop_loss
        if upd.reason == "stop_loss":
            text = (f"🛑 <b>حد ضرر #{sig.id} {e(sig.symbol)} خورد ({px(bid)}). همین الان بفروش</b> "
                    f"(اگر در صرافی ثبتش کرده بودی، خودش فروخته شده). نتیجه: {result}")
        elif upd.reason == "take_profit_2":
            text = f"🎯🎯 <b>هدف ۲ #{sig.id} {e(sig.symbol)} رسید ({px(bid)}). باقی‌مانده را بفروش.</b> نتیجه: {result}"
        else:
            text = (f"🔒 <b>حد ضرر محافظ #{sig.id} {e(sig.symbol)} خورد ({px(bid)}). بفروش.</b> "
                    f"نتیجه از ورود: {result} (بدون ضرر یا با سود)")
        return [Alert("exit", text, sig.id)]

    if upd.reason == "take_profit_1":
        sig.tp1_hit = True
        sig.stop = upd.stop_loss
        sig.notified_stop = upd.stop_loss
        return [Alert("tp1", f"🎯 <b>هدف ۱ #{sig.id} {e(sig.symbol)} رسید ({px(bid)}).</b>\n"
                             f"۱) نصف حجم را بفروش\n۲) حد ضرر بقیه را به <b>{px(upd.stop_loss)}</b> ببر (بالای نقطه‌ی "
                             f"ورود؛ از این به بعد این معامله ضرر نمی‌دهد)", sig.id, exit_btn)]

    if upd.stop_loss > sig.stop:
        sig.stop = upd.stop_loss
        last = sig.notified_stop or sig.initial_stop
        if sig.stop >= last * (1 + STOP_NOTIFY_STEP):
            sig.notified_stop = sig.stop
            label = "نقطه‌ی سربه‌سر" if sig.protection == "BREAKEVEN_LOCKED" else "حد ضرر متحرک"
            alerts.append(Alert("move_stop", f"🔒 #{sig.id} {e(sig.symbol)}: حد ضرر را به <b>{px(sig.stop)}</b> ببر "
                                             f"({label}). قیمت فعلی {px(bid)} ({result})", sig.id))

    if sig.protection == "INITIAL_STOP" and entry > sig.initial_stop:
        way = (entry - bid) / (entry - sig.initial_stop) * 100
        if way < DANGER_RESET:
            sig.danger_level = 0
        for level in DANGER_LEVELS:
            if way >= level > sig.danger_level:
                sig.danger_level = level
                icon, advice = ("⚠️", "مراقب باش، حد ضرر را در صرافی چک کن") if level == 50 else \
                    ("🚨", "نزدیک حد ضرر است. اگر سیگنال خروج آمد یا شک داری، زودتر خارج شو")
                alerts.append(Alert("danger", f"{icon} <b>#{sig.id} {e(sig.symbol)} به سمت ضرر می‌رود</b>: قیمت "
                                              f"{px(bid)} ({result})، {level}% راه تا حد ضرر {px(sig.initial_stop)} "
                                              f"را رفته. {advice}.", sig.id, exit_btn))
    return alerts


def trend_alert(sig: TradeSignal, tech_signal: str, bid: float) -> list[Alert]:
    """1h trend broken while in a position: the engine would exit here."""
    flags = dict(sig.flags or {})
    if sig.status != SignalStatus.ENTERED.value or tech_signal != "SELL" or flags.get("trend_exit"):
        return []
    flags["trend_exit"] = True
    sig.flags = flags
    entry = sig.user_entry or sig.entry
    return [Alert("trend", f"📉 <b>سیگنال خروج #{sig.id} {e(sig.symbol)}</b>: روند ۱ ساعته شکست (قیمت زیر EMA50 و "
                           f"EMA50 زیر EMA200). پیشنهاد: خارج شو. قیمت {px(bid)} ({pct(bid, entry)})", sig.id,
                  [("✅ فروختم", f"sig:exit:{sig.id}")])]
