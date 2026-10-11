"""21-day discipline challenges (paper trading at live MEXC prices).

    python -m competition create "AlphaDuel 21-day" SOL,JUP 21 [PRIZE TEXT]   # starts now
    python -m competition join CHALLENGE_ID EMAIL
    python -m competition buy CHALLENGE_ID EMAIL SYMBOL USD STOP
    python -m competition sell CHALLENGE_ID EMAIL SYMBOL [QTY]                 # no QTY = sell all
    python -m competition stop CHALLENGE_ID EMAIL SYMBOL NEW_STOP              # raise a stop
    python -m competition board CHALLENGE_ID                                   # tick once and print the board
    python -m competition run CHALLENGE_ID                                     # tick every minute until final

If the Telegram bot (python -m tgbot) is running, it already ticks every live challenge: don't also use `run`.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from sqlalchemy import select

from competition import engine
from competition.engine import ChallengeError, RuleViolation


async def fetch_quotes(mexc, symbols: list[str]) -> dict[str, engine.Quote]:
    out = {}
    for sym in symbols:
        m = await mexc.fetch_ticker(sym, depth=5)
        out[sym] = engine.Quote(bid=m.bid or m.price, ask=m.ask or m.price)
    return out


def format_board(ch, rows, names: dict[int, str]) -> str:
    lines = [f"{ch.title}  ({'FINAL' if ch.finalized else 'live'})  prize: {ch.prize_text or '-'}"]
    for p in rows:
        lines.append(f"  {p.rank or '-':>3}  {names.get(p.user_id, '?'):<16} ROI {p.roi_pct:+8.2f}%  "
                     f"discipline {engine.discipline_score(p):3.0f}  maxDD {p.max_drawdown_pct:5.2f}%  "
                     f"trades {p.trades_count:<3} {p.status}")
    return "\n".join(lines)


async def _run(argv: list[str]) -> int:
    from config import get_settings
    from database.connection import dispose_engine, get_session, init_db
    from database.models import Challenge, User
    from exchange_connector import MexcSpotConnector

    settings = get_settings()
    await init_db(settings.database_url)
    mexc = MexcSpotConnector(settings)
    cmd, args = (argv[0], argv[1:]) if argv else ("", [])
    try:
        async def board(cid: int) -> bool:
            async with get_session() as s:
                ch = await s.get(Challenge, cid)
                if ch is None:
                    raise ChallengeError(f"no challenge {cid}")
                for e in await engine.tick(s, ch, {} if ch.finalized else await fetch_quotes(mexc, ch.symbol_list)):
                    print(f"  event: {e.kind} user {e.user_id} {e.symbol or ''} {e.detail}")
                rows = engine.rank(await engine.participants(s, ch))
                names = {u.id: engine.public_name(u) for u in (await s.execute(select(User))).scalars()}
                print(format_board(ch, rows, names))
                return ch.finalized

        if cmd == "create" and len(args) in (3, 4):
            now = datetime.now(timezone.utc)
            async with get_session() as s:
                ch = Challenge(title=args[0], symbols=args[1].upper(), start_time=now,
                               end_time=now + timedelta(days=float(args[2])),
                               prize_text=args[3] if len(args) == 4 else "")
                for sym in ch.symbol_list:
                    if not await mexc.has_symbol(sym):
                        raise ChallengeError(f"{sym}/USDT is not listed on MEXC")
                s.add(ch)
                await s.flush()
                print(f"challenge {ch.id} created, ends {ch.end_time:%Y-%m-%d %H:%M} UTC")
        elif cmd in {"join", "buy", "sell", "stop"} and len(args) >= 2:
            async with get_session() as s:
                ch = await s.get(Challenge, int(args[0]))
                user = (await s.execute(select(User).where(User.email == args[1].strip().lower()))).scalar_one()
                try:
                    if cmd == "join":
                        await engine.join(s, ch, user)
                        print(f"joined with ${ch.starting_balance:,.0f} virtual USDT")
                    else:
                        p = await engine.get_participant(s, ch, user)
                        sym = args[2].upper()
                        q = (await fetch_quotes(mexc, [sym]))[sym]
                        if cmd == "buy":
                            o = await engine.buy(s, ch, p, sym, q, usd=float(args[3]), stop=float(args[4]))
                        elif cmd == "sell":
                            o, _ = await engine.sell(s, ch, p, sym, q, qty=float(args[3]) if len(args) > 3 else None)
                        else:
                            await engine.set_stop(ch, p, sym, float(args[3]), q)
                            print(f"{sym} stop raised to {args[3]}")
                            return 0
                        print(f"{o.side} {o.qty:.6f} {o.symbol} @ {o.price}  fee ${o.fee_usd:.2f}  cash ${p.cash:,.2f}")
                except RuleViolation as exc:             # caught inside the session so the rule break is saved
                    print(f"refused (rule): {exc}")
                    return 1
        elif cmd == "board" and len(args) == 1:
            await board(int(args[0]))
        elif cmd == "run" and len(args) == 1:
            while not await board(int(args[0])):
                await asyncio.sleep(60)
        else:
            print(__doc__)
            return 2
    except ChallengeError as exc:
        print(f"refused: {exc}")
        return 1
    finally:
        await mexc.close()
        await dispose_engine()
    return 0


def main() -> int:
    load_dotenv()
    return asyncio.run(_run(sys.argv[1:]))


if __name__ == "__main__":
    sys.exit(main())
