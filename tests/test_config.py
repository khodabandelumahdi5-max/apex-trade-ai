import os
import shutil
from pathlib import Path

import pytest

import config

ROOT = Path(__file__).resolve().parents[1]
KEYS = ["CEX_EXCHANGE", "HELIUS_API_KEY", "TRADING_MODE", "WATCHLIST", "SOLANA_PRIVATE_KEY"]


@pytest.fixture
def fresh_env(monkeypatch, tmp_path):
    saved = dict(os.environ)
    for k in list(config.Settings.model_fields) + KEYS:
        os.environ.pop(k.upper(), None)
    monkeypatch.chdir(tmp_path)
    config.get_settings.cache_clear()
    yield tmp_path
    os.environ.clear()
    os.environ.update(saved)   # load_dotenv writes into os.environ; undo it
    config.get_settings.cache_clear()


def test_env_example_loads_as_is(fresh_env):
    shutil.copy(ROOT / ".env.example", fresh_env / ".env")
    s = config.get_settings()
    assert s.trading_mode == "paper" and s.cex_exchange is None and s.helius_api_key is None
    assert [t.symbol for t in s.watchlist] == ["SOL", "JUP"]


def test_inline_comment_after_empty_value_is_ignored(fresh_env):
    (fresh_env / ".env").write_text("CEX_EXCHANGE=      # binance | bybit\nHELIUS_API_KEY=  # later\n")
    s = config.get_settings()
    assert s.cex_exchange is None and s.helius_api_key is None


def test_live_mode_requires_confirmation(fresh_env):
    (fresh_env / ".env").write_text("TRADING_MODE=live\n")
    with pytest.raises(Exception, match="LIVE_TRADING_CONFIRM"):
        config.get_settings()
