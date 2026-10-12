"""One-window setup for the Telegram bot (double-click setup-bot.bat).

1. paste the bot token  → checked with Telegram (must be @BOT_USERNAME, default alphaduellivebot)
2. press Start in the bot → your numeric id is read automatically (no @userinfobot needed)
3. budget               → everything is written to .env, a test message is sent, then the bot starts.
The token never leaves this computer except to Telegram itself.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"
EXPECTED = os.getenv("BOT_USERNAME", "alphaduellivebot").lstrip("@").lower()


class WizardError(Exception):
    pass


def _client() -> httpx.Client:
    from tgbot.netproxy import telegram_proxy
    return httpx.Client(timeout=20, proxy=telegram_proxy())


def api(token: str, method: str, **params) -> dict:
    try:
        with _client() as c:
            r = c.post(f"https://api.telegram.org/bot{token}/{method}", json=params)
    except httpx.HTTPError as exc:
        raise WizardError("به تلگرام وصل نشد. VPN را روشن کن و دوباره امتحان کن.\n"
                          f"(Cannot reach Telegram - turn on your VPN. {type(exc).__name__})") from None
    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code == 401:
        raise WizardError("توکن اشتباه است. دوباره از BotFather کپی کن.\n(Wrong token)")
    if r.status_code == 409:
        raise WizardError("ربات همین الان در یک پنجره‌ی دیگر روشن است. پنجره‌ی «AlphaDuel bot» را ببند و دوباره امتحان کن.\n"
                          "(The bot is already running in another window - close it first)")
    if not data.get("ok"):
        raise WizardError(f"Telegram: {data.get('description') or r.status_code}")
    return data["result"]


def check_token(token: str) -> str:
    token = token.strip()
    if ":" not in token or len(token) < 30 or " " in token:
        raise WizardError("این شبیه توکن نیست. باید چیزی مثل 123456789:AAH... باشد.\n(Not a bot token)")
    me = api(token, "getMe")
    if me["username"].lower() != EXPECTED:
        raise WizardError(f"این توکن مال ربات @{me['username']} است، نه @{EXPECTED}.\n(Token of another bot)")
    api(token, "deleteWebhook", drop_pending_updates=False)
    return me["username"]


def wait_for_start(token: str, timeout_s: int = 180) -> tuple[int, str]:
    """Return (telegram id, first name) of whoever sends a message to the bot within the timeout."""
    offset, deadline = None, time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        updates = api(token, "getUpdates", timeout=20, **({"offset": offset} if offset else {}))
        for u in updates:
            offset = u["update_id"] + 1
            msg = u.get("message") or {}
            sender = msg.get("from") or {}
            if sender.get("id") and msg.get("chat", {}).get("type") == "private" and not sender.get("is_bot"):
                api(token, "getUpdates", offset=offset, timeout=0)        # mark as read
                return int(sender["id"]), sender.get("first_name", "")
    raise WizardError("پیامی نرسید. در تلگرام ربات را باز کن و Start را بزن، بعد دوباره امتحان کن.\n(No /start received)")


def write_env(values: dict[str, str], path: Path = ENV) -> None:
    """Replace or append KEY=VALUE lines; keeps everything else in .env."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else \
        (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    seen: set[str] = set()
    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip()
        if key in values:
            if key not in seen:
                out.append(f"{key}={values[key]}")
                seen.add(key)
        else:
            out.append(line)
    out += [f"{k}={v}" for k, v in values.items() if k not in seen]
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def parse_budget(raw: str) -> float:
    try:
        v = float(raw.replace(",", "").replace("$", "").strip() or "100")
    except ValueError:
        raise WizardError("بودجه باید عدد باشد، مثلاً 100\n(Budget must be a number)") from None
    if not 10 <= v <= 1_000_000:
        raise WizardError("بودجه باید بین 10 و 1000000 دلار باشد.")
    return v


def finish(token: str, user_id: int, name: str, budget: float) -> None:
    write_env({"TELEGRAM_BOT_TOKEN": token, "BOT_USERNAME": EXPECTED, "ADMIN_TELEGRAM_IDS": str(user_id),
               "SIGNAL_BUDGET_USD": f"{budget:g}"})
    api(token, "sendMessage", chat_id=user_id,
        text=f"✅ {name} عزیز، اتصال برقرار شد.\nبودجه: ${budget:g}\nبعد از روشن شدن ربات، /plan را بزن تا وضعیت بازار را ببینی. "
             f"سیگنال‌ها خودکار برایت می‌آیند.")


# ---------------------------------------------------------------------------------------------- user interface
def run_gui() -> bool:
    import tkinter as tk
    from tkinter import messagebox, simpledialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    title = "AlphaDuel"
    while True:
        token = simpledialog.askstring(title, "۱) توکن ربات را این‌جا Paste کن (Ctrl+V)\n"
                                              "از ‎@BotFather ← /mybots ← alphaduellivebot ← API Token\n\n"
                                              "Paste the bot token:", parent=root)
        if token is None:
            return False
        try:
            check_token(token)
            break
        except WizardError as exc:
            messagebox.showerror(title, str(exc), parent=root)
    while True:
        if not messagebox.askokcancel(title, "۲) حالا در تلگرام ربات ‎@alphaduellivebot را باز کن و Start بزن\n"
                                             "(اگر قبلاً زده‌ای، یک پیام مثل سلام بفرست).\nبعد OK را بزن.\n\n"
                                             "Open the bot in Telegram, press Start, then OK.", parent=root):
            return False
        try:
            user_id, name = wait_for_start(token.strip(), timeout_s=120)
            break
        except WizardError as exc:
            messagebox.showerror(title, str(exc), parent=root)
    while True:
        raw = simpledialog.askstring(title, "۳) با چند دلار معامله می‌کنی؟ (مثلاً 100)\nBudget in USDT:",
                                     initialvalue="100", parent=root)
        if raw is None:
            return False
        try:
            budget = parse_budget(raw)
            break
        except WizardError as exc:
            messagebox.showerror(title, str(exc), parent=root)
    try:
        finish(token.strip(), user_id, name, budget)
    except WizardError as exc:
        messagebox.showerror(title, str(exc), parent=root)
        return False
    messagebox.showinfo(title, "✅ تمام شد! ربات الان روشن می‌شود.\nیک پیام تست در تلگرام برایت فرستادم.\n\nDone.",
                        parent=root)
    return True


def run_console() -> bool:
    print("AlphaDuel setup")
    while True:
        try:
            token = input("1) Paste the bot token from @BotFather: ").strip()
            check_token(token)
            break
        except WizardError as exc:
            print(exc)
    input("2) Open @alphaduellivebot in Telegram, press Start, then press Enter here... ")
    user_id, name = wait_for_start(token)
    budget = parse_budget(input("3) Budget in USDT [100]: "))
    finish(token, user_id, name, budget)
    print("Done. A test message was sent to you in Telegram.")
    return True


def main() -> int:
    try:
        ok = run_gui()
    except ImportError:
        ok = run_console()
    except Exception as exc:                          # e.g. no display: fall back to the console
        if "display" in str(exc).lower() or "tcl" in str(exc).lower():
            ok = run_console()
        else:
            raise
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
