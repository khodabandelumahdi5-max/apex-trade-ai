"""Which proxy to use for api.telegram.org.

Telegram is blocked in some countries, so it is reached through a VPN. VPN apps such as v2rayN, Nekoray or
Hiddify usually set a *Windows system proxy*; python-telegram-bot (httpx) ignores that and only reads
environment variables, so without this the bot cannot connect even though Telegram works in the browser.

Order: TELEGRAM_PROXY in .env (e.g. http://127.0.0.1:10809 or socks5://127.0.0.1:10808), "none" to disable,
otherwise the system proxy (Windows registry / macOS settings / *_PROXY variables), otherwise direct.
"""
from __future__ import annotations

import os
import urllib.request


def telegram_proxy() -> str | None:
    raw = os.getenv("TELEGRAM_PROXY", "").strip()
    if raw.lower() in {"none", "off", "direct", "0"}:
        return None
    if not raw:
        proxies = urllib.request.getproxies()          # includes the Windows Internet Settings proxy
        raw = proxies.get("https") or proxies.get("http") or proxies.get("all") or ""
    raw = raw.strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = "http://" + raw
    return raw
