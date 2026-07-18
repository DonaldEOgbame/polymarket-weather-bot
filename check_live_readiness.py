"""Pre-live readiness check — run against the deployed machine:

    flyctl ssh console -a stormedge -C "python /app/check_live_readiness.py"

Read-only. Places no orders and moves no funds. Verifies the credential chain
(signer -> funder -> CLOB) and reports collateral balance and exchange
allowances, which is as far as verification can go without actually trading.
"""
import os
import sys

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import BalanceAllowanceParams, AssetType


def main():
    pk = os.getenv("POLYMARKET_PK", "")
    sig = int(os.getenv("POLYMARKET_SIG_TYPE", "0"))
    funder = os.getenv("POLYMARKET_FUNDER", "")
    paper = os.getenv("PAPER_MODE", "true").lower() != "false"

    ok = True
    print("=" * 62)
    print("CONFIG")
    print("=" * 62)
    print(f"  PAPER_MODE : {paper}")
    print(f"  SIG_TYPE   : {sig}  ({'proxy wallet' if sig else 'raw EOA'})")
    print(f"  FUNDER     : {funder or '(unset)'}")

    if not pk:
        print("\n  FAIL: POLYMARKET_PK is not set.")
        return 1

    # A proxy account (sig 1/2) funds from an address distinct from the signer.
    # funder == signer means the config claims a proxy but points at the EOA —
    # the exact misconfiguration that reads cannot detect but orders reject on.
    kwargs = {"key": pk, "chain_id": 137}
    if sig:
        kwargs["signature_type"] = sig
        kwargs["funder"] = funder

    print("\n" + "=" * 62)
    print("CREDENTIAL CHAIN")
    print("=" * 62)
    try:
        client = ClobClient("https://clob.polymarket.com", **kwargs)
        client.set_api_creds(client.create_or_derive_api_creds())
        signer = client.get_address()
        print(f"  signer     : {signer}")
        print("  auth       : OK (API creds derived against live CLOB)")
    except Exception as e:
        print(f"  auth       : FAIL {type(e).__name__}: {e}")
        return 1

    if sig and funder.lower() == signer.lower():
        print("\n  FAIL: SIG_TYPE claims a proxy wallet but FUNDER is the signer.")
        print("  Set FUNDER to the address under Profile -> 'Your Polymarket")
        print("  Wallet Address', or set SIG_TYPE=0 for a genuine raw EOA.")
        return 1

    print("\n" + "=" * 62)
    print("FUNDER STATE")
    print("=" * 62)
    try:
        res = client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=sig)
        )
        # USDC is 6-decimal; the API returns integer micro-units as a string.
        balance = int(res.get("balance", "0")) / 1e6
        allowances = res.get("allowances", {})
        print(f"  collateral : ${balance:,.2f} USDC")
        for addr, amt in allowances.items():
            print(f"  allowance  : {addr} -> {int(amt) / 1e6:,.2f}")

        if balance == 0:
            print("\n  BLOCKED: no collateral. Deposit before going live.")
            ok = False
        if allowances and not any(int(a) > 0 for a in allowances.values()):
            # The relayer normally sets these on a proxy account's first trade,
            # so zero here is only a problem once an order actually rejects.
            print("\n  NOTE: all allowances are zero. Expected before the first")
            print("  trade — the relayer sets them then. If an order is later")
            print("  rejected for balance, check this first.")
    except Exception as e:
        print(f"  FAIL {type(e).__name__}: {e}")
        return 1

    print("\n" + "=" * 62)
    print("REMAINING MANUAL STEPS")
    print("=" * 62)
    print("  [ ] $1 manual BUY+SELL round-trip")
    print("  [ ] verify _read_fill against the raw get_order response")
    print("  [ ] set STARTING_BANKROLL to the real deposit; re-seed ledger")
    print("  [ ] set PAPER_MODE=false")
    print()
    print("READY FOR FUNDING" if ok else "NOT READY — see BLOCKED above")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
