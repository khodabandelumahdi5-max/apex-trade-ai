import pytest

from tgbot import setup


class FakeBot:
    def __init__(self, username):
        self.username, self.calls = username, []

    async def get_me(self):
        return type("Me", (), {"username": self.username, "id": 1})()

    async def get_my_description(self):
        return type("D", (), {"description": ""})()

    async def get_my_short_description(self):
        return type("D", (), {"short_description": setup.SHORT_DESCRIPTION})()

    def __getattr__(self, name):
        async def call(*a, **k):
            self.calls.append(name)
        return call


@pytest.mark.asyncio
async def test_configures_the_right_bot(monkeypatch):
    monkeypatch.setenv("BOT_USERNAME", "@AlphaDuelLiveBot")
    monkeypatch.setenv("MINIAPP_URL", "https://app.example.com")
    bot = FakeBot("alphaduellivebot")
    assert await setup.configure(bot) == "alphaduellivebot"
    assert bot.calls == ["set_my_commands", "set_my_description", "set_chat_menu_button"]   # short desc unchanged
    assert all(len(d) <= 256 for _, d in setup.COMMANDS) and len(setup.DESCRIPTION) <= 512
    assert len(setup.SHORT_DESCRIPTION) <= 120


@pytest.mark.asyncio
async def test_refuses_another_bots_token(monkeypatch):
    monkeypatch.setenv("BOT_USERNAME", "alphaduellivebot")
    with pytest.raises(setup.WrongBotError):
        await setup.configure(FakeBot("someotherbot"))
