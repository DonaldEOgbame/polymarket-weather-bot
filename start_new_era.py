"""Close the current trading era and open a fresh one — CLI wrapper.

    flyctl ssh console -a stormedge -C "python /app/start_new_era.py --label live-2"

You normally DON'T need this: the dashboard's Settings tab has a "Start new
era" button doing exactly the same thing (both call db.cutover_era, one
implementation). This script exists for when the dashboard is unreachable.

Cash sync is likewise automatic — the bot books deposits/withdrawals it sees
on the wallet every monitor cycle (executor.sync_wallet_cash), so funding the
wallet needs NO command at all: money lands, the next cycle books it, trading
resumes at the real bankroll.

What a cutover does (db.cutover_era): refuses while positions are open;
freezes the DB to /data/era_<NNN>_<label>.db (trimmed of SKIP/pre-era signals
so archives stay ~5MB, not 250MB); wipes the MONEY tables (bankroll, trades,
positions, resolutions) while KEEPING research tables (signals, markets,
model_accuracy — calibration learns across eras); seeds the new ledger; opens
the new era row. Settings overrides survive unless --reset-settings.
"""
import argparse
import os
import sys

DB_PATH = os.getenv("DB_PATH", "/data/bot.db")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", help="name for the NEW era (default: <mode>-N)")
    ap.add_argument("--mode", default=None, choices=("live", "paper"),
                    help="mode of the new era (default: from PAPER_MODE)")
    ap.add_argument("--seed", type=float, default=None,
                    help="seed the new ledger with this instead of the CLOB balance")
    ap.add_argument("--reset-settings", action="store_true",
                    help="also clear dashboard setting overrides")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = ap.parse_args()

    if not os.path.exists(DB_PATH):
        print(f"ABORT: no database at {DB_PATH}")
        return 1

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import db as dbmod
    dbmod.init_db()
    from config import PAPER_MODE

    new_mode = args.mode or ("paper" if PAPER_MODE else "live")
    n_existing = dbmod.fetch_query("SELECT COUNT(*) AS c FROM eras")[0]["c"]
    new_label = args.label or f"{new_mode}-{n_existing + 1}"

    if args.seed is not None:
        seed, seed_src = args.seed, "--seed flag"
    else:
        from executor import get_wallet_collateral
        bal = get_wallet_collateral()
        seed = bal if bal is not None else 0.0
        seed_src = "CLOB collateral" if bal is not None else "0.00 (CLOB unreadable)"

    n_trades = dbmod.fetch_query("SELECT COUNT(*) AS c FROM trades")[0]["c"]
    print(f"\nClosing: {n_trades} trades to archive")
    print(f"Opening: '{new_label}' ({new_mode}) seeded ${seed:.2f} (from {seed_src})")
    if seed <= 0:
        print("NOTE: seeding $0.00 — the bot cannot trade until money lands in the")
        print("      wallet; the auto cash-sync will then book it as a deposit.")
    if args.reset_settings:
        print("Settings overrides: CLEARED")

    if not args.yes:
        if input("Proceed? type 'yes' to continue: ").strip().lower() != "yes":
            print("Aborted — nothing changed.")
            return 1

    try:
        summary = dbmod.cutover_era(new_label, new_mode, seed,
                                    reset_settings=args.reset_settings)
    except RuntimeError as e:
        print(f"ABORT: {e}")
        return 1

    print(f"\nEra {summary['new_era_id']} '{summary['new_label']}' is open at "
          f"${summary['seed']:.2f}.")
    if summary["archive_path"]:
        print(f"Archived {summary['archived_trades']} trades "
              f"({summary['closed_balance']:.2f} closing balance) -> {summary['archive_path']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
