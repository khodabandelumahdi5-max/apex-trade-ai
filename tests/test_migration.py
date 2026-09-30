import sqlite3

import pytest

from database import repository as repo
from database.connection import dispose_engine, init_db


@pytest.mark.asyncio
async def test_old_database_gets_new_columns(tmp_path):
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)      # a trades table as created by an earlier version (no TP columns)
    con.execute("""CREATE TABLE trades (id INTEGER PRIMARY KEY, timestamp DATETIME NOT NULL, symbol VARCHAR(32) NOT NULL,
        direction VARCHAR(5) NOT NULL, entry_price FLOAT NOT NULL, size FLOAT NOT NULL, stop_loss FLOAT NOT NULL,
        status VARCHAR(6) NOT NULL, pnl FLOAT NOT NULL, protection_status VARCHAR(16) NOT NULL, venue VARCHAR(16) NOT NULL,
        mint VARCHAR(64), initial_stop FLOAT NOT NULL, risk_usd FLOAT NOT NULL, highest_price FLOAT NOT NULL,
        last_price FLOAT, exit_price FLOAT, closed_at DATETIME, exit_reason VARCHAR(64), fees_usd FLOAT NOT NULL,
        entry_order_id VARCHAR(128), exit_order_id VARCHAR(128), stop_order_id VARCHAR(128))""")
    con.execute("""INSERT INTO trades VALUES (1,'2026-09-01','SOL','LONG',100,1,97,'CLOSED',5,'INITIAL_STOP','paper',
        NULL,97,3,100,105,105,'2026-09-02','x',0,NULL,NULL,NULL)""")
    con.commit(); con.close()
    await init_db(f"sqlite+aiosqlite:///{db}")
    try:
        closed = await repo.closed_trades()
        assert closed[0].pnl == 5 and closed[0].tp1_hit is False and closed[0].realized_partial == 0
    finally:
        await dispose_engine()
