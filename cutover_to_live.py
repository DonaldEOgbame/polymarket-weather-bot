"""One-shot cutover from paper trading to live money. Run ON the Fly machine:

    flyctl ssh console -a stormedge -C "python /app/cutover_to_live.py"

Refuses to run while any position is still open — the paper trades must finish
settling under the current code first (that is the point of the paper era).

What it does, in order:
  1. Freezes the entire paper-era DB to ARCHIVE (/data/paper_archive.db) via the
     sqlite backup API. The dashboard's mode-pill toggle reads this snapshot.
  2. Wipes the MONEY tables in the live DB (bankroll, trades, positions,
     resolutions) — research tables (signals, markets, model_accuracy, scan_log)
     are kept; they are model-side, not money-side.
  3. Seeds the bankroll ledger with the REAL collateral balance read from the
     CLOB at that moment — not a config constant, the actual number.

It does NOT flip PAPER_MODE. That stays a human action:
    edit fly.toml  ->  PAPER_MODE = "false", STARTING_BANKROLL = "<printed seed>"
    flyctl deploy -a stormedge
"""
import os
import sqlite3
import sys
from datetime import datetime, timezone

DB_PATH = os.getenv("DB_PATH", "/data/bot.db")
ARCHIVE = os.getenv("ARCHIVE_DB_PATH", "/data/paper_archive.db")
MONEY_TABLES = ("bankroll", "trades", "positions", "resolutions")


def main():
    db = sqlite3.connect(DB_PATH)

    open_pos = db.execute("SELECT market_id, side, question FROM positions").fetchall()
    if open_pos:
        print("ABORT: positions are still open — let them settle in paper first:")
        for m, s, q in open_pos:
            print(f"  {s}  {q[:70]}")
        return 1

    if os.path.exists(ARCHIVE):
        print(f"ABORT: {ARCHIVE} already exists — cutover looks already done. "
              "Delete it manually only if you are sure.")
        return 1

    # 1. Freeze the archive (backup API = consistent copy even mid-writes)
    print(f"Freezing paper era -> {ARCHIVE} ...")
    dst = sqlite3.connect(ARCHIVE)
    with dst:
        db.backup(dst)
    dst.close()
    n_trades = sqlite3.connect(ARCHIVE).execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    print(f"  archived: {n_trades} trades, "
          f"{os.path.getsize(ARCHIVE)/1e6:.0f} MB")

    # 2. Real balance from the CLOB — the seed must be what the wallet holds
    from py_clob_client_v2.client import ClobClient
    from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
    from config import POLYMARKET_PK, POLYMARKET_SIG_TYPE, POLYMARKET_FUNDER, CLOB_BASE_URL
    client = ClobClient(CLOB_BASE_URL, key=POLYMARKET_PK, chain_id=137,
                        signature_type=POLYMARKET_SIG_TYPE, funder=POLYMARKET_FUNDER)
    client.set_api_creds(client.create_or_derive_api_key())
    client.update_balance_allowance(BalanceAllowanceParams(
        asset_type=AssetType.COLLATERAL, signature_type=POLYMARKET_SIG_TYPE))
    bal = int(client.get_balance_allowance(BalanceAllowanceParams(
        asset_type=AssetType.COLLATERAL, signature_type=POLYMARKET_SIG_TYPE))["balance"]) / 1e6
    print(f"CLOB collateral: ${bal:.2f}")
    if bal <= 0:
        print("ABORT: CLOB reports zero balance — nothing to seed with.")
        return 1

    # 3. Wipe money tables, seed the ledger
    with db:
        for t in MONEY_TABLES:
            db.execute(f"DELETE FROM {t}")
        db.execute(
            "INSERT INTO bankroll (timestamp, event, amount, balance, trade_id) "
            "VALUES (?, 'LIVE_SEED', ?, ?, NULL)",
            (datetime.now(timezone.utc).isoformat(), bal, bal),
        )
    db.execute("VACUUM")
    print(f"Ledger re-seeded at ${bal:.2f}. Money tables wiped; research tables kept.")

    print()
    print("NEXT (human): set in fly.toml  ->")
    print('  PAPER_MODE = "false"')
    print(f'  STARTING_BANKROLL = "{bal:.2f}"')
    print("then: flyctl deploy -a stormedge")
    return 0


if __name__ == "__main__":
    sys.exit(main())
