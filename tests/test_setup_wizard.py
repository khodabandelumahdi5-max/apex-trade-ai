import json

import httpx
import pytest

from tgbot import setup_wizard as wz

TOKEN = "123456789:AAH" + "x" * 32


def fake_telegram(monkeypatch, username="alphaduellivebot", updates=None, status=200):
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content or b"{}")
        sent.append((method, body))
        if status != 200:
            return httpx.Response(status, json={"ok": False, "description": "err"})
        result = {"getMe": {"username": username, "id": 1}, "getUpdates": updates or [],
                  "deleteWebhook": True, "sendMessage": {"message_id": 1}}[method]
        return httpx.Response(200, json={"ok": True, "result": result})

    monkeypatch.setattr(wz, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    return sent


def test_token_checks(monkeypatch):
    fake_telegram(monkeypatch)
    assert wz.check_token(" " + TOKEN + " ") == "alphaduellivebot"
    with pytest.raises(wz.WizardError, match="توکن"):
        wz.check_token("hello")
    fake_telegram(monkeypatch, username="otherbot")
    with pytest.raises(wz.WizardError, match="otherbot"):
        wz.check_token(TOKEN)
    fake_telegram(monkeypatch, status=401)
    with pytest.raises(wz.WizardError, match="اشتباه"):
        wz.check_token(TOKEN)
    fake_telegram(monkeypatch, status=409)
    with pytest.raises(wz.WizardError, match="AlphaDuel bot"):
        wz.check_token(TOKEN)


def test_reads_the_owner_id_from_start(monkeypatch):
    updates = [{"update_id": 5, "message": {"from": {"id": 42, "first_name": "Mahdi", "is_bot": False},
                                            "chat": {"type": "private"}, "text": "/start"}}]
    sent = fake_telegram(monkeypatch, updates=updates)
    assert wz.wait_for_start(TOKEN, timeout_s=5) == (42, "Mahdi")
    assert ("getUpdates", {"offset": 6, "timeout": 0}) in sent          # marked as read


def test_write_env_and_finish(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("A=1\nTELEGRAM_BOT_TOKEN=\n# comment\nTELEGRAM_BOT_TOKEN=dup\n", encoding="utf-8")
    wz.write_env({"TELEGRAM_BOT_TOKEN": TOKEN, "ADMIN_TELEGRAM_IDS": "42"}, env)
    text = env.read_text(encoding="utf-8")
    assert text.count("TELEGRAM_BOT_TOKEN=") == 1 and f"TELEGRAM_BOT_TOKEN={TOKEN}" in text
    assert "A=1" in text and "# comment" in text and text.endswith("ADMIN_TELEGRAM_IDS=42\n")
    assert wz.parse_budget("$1,500") == 1500 and wz.parse_budget("") == 100
    with pytest.raises(wz.WizardError):
        wz.parse_budget("abc")
    sent = fake_telegram(monkeypatch)
    written = {}
    monkeypatch.setattr(wz, "write_env", lambda values: written.update(values))
    wz.finish(TOKEN, 42, "Mahdi", 250)
    assert written == {"TELEGRAM_BOT_TOKEN": TOKEN, "BOT_USERNAME": "alphaduellivebot", "ADMIN_TELEGRAM_IDS": "42",
                       "SIGNAL_BUDGET_USD": "250"}
    assert sent[-1][0] == "sendMessage" and sent[-1][1]["chat_id"] == 42 and "$250" in sent[-1][1]["text"]


def test_wait_for_start_survives_a_dropped_long_poll(monkeypatch):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadTimeout("slow vpn")
        upd = [{"update_id": 1, "message": {"from": {"id": 7, "first_name": "A"}, "chat": {"type": "private"}}}]
        return httpx.Response(200, json={"ok": True, "result": upd if calls["n"] == 2 else []})

    monkeypatch.setattr(wz, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(wz.time, "sleep", lambda s: None)
    assert wz.wait_for_start(TOKEN, timeout_s=30) == (7, "A")
