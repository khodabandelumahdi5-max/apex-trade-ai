"""Paper-trading competitions.

    python -m competition create "Weekly SOL/JUP duel" SOL,JUP 7 [PRIZE TEXT]   # starts now, lasts 7 days
    python -m competition join CHALLENGE_ID EMAIL
    python -m competition buy CHALLENGE_ID EMAIL SYMBOL USD
    python -m competition sell CHALLENGE_ID EMAIL SYMBOL [QTY]                  # no QTY = sell all
    python -m competition board CHALLENGE_ID                                    # refresh and print the leaderboard
    python -m competition run CHALLENGE_ID                                      # refresh every minute until final
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from sqlalchemy import select

from competition.engine import ChallengeError, Quote, join, place_order, update_leaderboard


async def _quotes(mexc, symbols: list[str]) -> dict[str, Quote]:
    out = {}
    for sym in symbols:
        m = await mexc.fetch_ticker(sym, depth=5)
        out[sym] = Quote(bid=m.bid or m.price, ask=m.ask or m.price)
    return out


async def _run(argv: list[str]) -> int:
    from config import get_settings
    from database.connection import dispose_engine, get_session, init_db
    from database.models import Challenge, ChallengeParticipant, User
    from exchange_connector import MexcSpotConnector

    settings = get_settings()
    await init_db(settings.database_url)
    mexc = MexcSpotConnector(settings)
    cmd, args = (argv[0], argv[1:]) if argv else ("", [])
    try:
        async def board(cid: int) -> bool:
            async with get_session() as s:
                ch = await s.get(Challenge, cid)
                rows = await update_leaderboard(s, ch, {} if ch.finalized else await _quotes(mexc, ch.symbol_list))
                emails = {u.id: u.email for u in (await s.execute(select(User))).scalars()}
                print(f"\n{ch.title}  ({'FINAL' if ch.finalized else 'live'})  prize: {ch.prize_text or '-'}")
                for p in rows:
                    name = emails.get(p.user_id, "?").split("@")[0]      # never show full e-mail addresses
                    print(f"  {p.rank or '-':>3}  {name:<16} ROI {p.roi_pct:+8.2f}%  equity ${p.equity:,.2f}  "
                          f"maxDD {p.max_drawdown_pct:5.2f}%  trades {p.trades_count}")
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
        elif cmd in {"join", "buy", "sell"} and len(args) >= 2:
            async with get_session() as s:
                ch = await s.get(Challenge, int(args[0]))
                user = (await s.execute(select(User).where(User.email == args[1].strip().lower()))).scalar_one()
                p = await join(s, ch, user)
                if cmd == "join":
                    print(f"joined with ${ch.starting_balance:,.0f} virtual USDT")
                else:
                    sym = args[2].upper()
                    q = (await _quotes(mexc, [sym]))[sym]
                    o = await place_order(s, ch, p, sym, cmd.upper(), q,
                                          usd=float(args[3]) if cmd == "buy" else None,
                                          qty=float(args[3]) if cmd == "sell" and len(args) > 3 else None)
                    print(f"{o.side} {o.qty:.6f} {o.symbol} @ {o.price}  fee ${o.fee_usd:.2f}  cash ${p.cash:,.2f}")
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
