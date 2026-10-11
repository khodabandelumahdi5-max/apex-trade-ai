from __future__ import annotations

import asyncio
import getpass
import os
import sys
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from sqlalchemy import func, select

from billing.service import apply_transfers, create_invoice, expire_invoices
from billing.tron import TronGridClient, is_tron_address


async def _run(argv: list[str]) -> int:
    from config import get_settings
    from database.connection import dispose_engine, get_session, init_db
    from database.models import Invoice, InvoiceStatus, User
    from database.secure import hash_password

    address = os.getenv("TRON_RECEIVE_ADDRESS", "").strip()
    cmd = argv[0] if argv else ""
    if cmd in {"invoice", "check"} and not is_tron_address(address):
        print("Set TRON_RECEIVE_ADDRESS in .env to a valid TRON address (starts with T)")
        return 2
    await init_db(get_settings().database_url)
    try:
        async with get_session() as s:
            def find(email: str):
                return s.execute(select(User).where(User.email == email.strip().lower()))
            if cmd == "adduser" and len(argv) == 2:
                pw = getpass.getpass("password (min 10 chars): ")
                s.add(User(email=argv[1], hashed_password=hash_password(pw),
                           subscription_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
                print("user created (no active subscription yet)")
            elif cmd == "invoice" and len(argv) in (2, 3):
                user = (await find(argv[1])).scalar_one()
                inv = await create_invoice(s, user, address, days=int(argv[2]) if len(argv) == 3 else None)
                print(f"Send EXACTLY {inv.amount_usdt} USDT (TRC20, TRON network) to {address}\n"
                      f"within {(inv.expires_at - inv.created_at).total_seconds() / 3600:.0f} h. "
                      f"A different amount cannot be matched automatically.")
            elif cmd == "check":
                since = (await s.execute(select(func.min(Invoice.created_at))
                                         .where(Invoice.status == InvoiceStatus.PENDING))).scalar()
                await expire_invoices(s)
                if since is None:
                    print("no open invoices")
                    return 0
                since = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
                client = TronGridClient(address, os.getenv("TRONGRID_API_KEY") or None)
                paid = await apply_transfers(s, await client.incoming_usdt(since), address)
                print(f"{len(paid)} invoice(s) paid")
            elif cmd == "status" and len(argv) == 2:
                u = (await find(argv[1])).scalar_one()
                print(f"{u.email}: active={u.subscription_active} until {u.subscription_expires_at}")
            else:
                print(__import__("billing.service").service.__doc__)
                return 2
    finally:
        await dispose_engine()
    return 0


def main() -> int:
    load_dotenv()
    return asyncio.run(_run(sys.argv[1:]))


if __name__ == "__main__":
    sys.exit(main())
