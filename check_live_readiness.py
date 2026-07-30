"""Pre-live readiness check.

Read-only. Places no orders and moves no funds. Verifies the credential chain
(signer -> funder -> CLOB) and reports collateral balance and exchange
allowances, which is as far as verification can go without actually trading.

Two front ends over ONE implementation, so they can never drift:

    preflight()   -> structured results, used by the dashboard's paper -> live
                     switch as a hard gate (app.py /api/live-preflight)
    python check_live_readiness.py
                  -> the same results, printed, for:
                     flyctl ssh console -a stormedge -C "python /app/check_live_readiness.py"
"""
import os
import sys

# NOTE: py_clob_client_v2 is imported lazily inside preflight(). Importing it at
# module scope would make this module unimportable wherever the CLOB library is
# absent (a paper-mode dev box), and the dashboard imports it on every load of
# the trading-mode dialog — including while still in paper mode.

SIG_NAMES = {
    0: "raw EOA",
    1: "Magic/email proxy",
    2: "browser-wallet proxy",
    3: "deposit wallet (POLY_1271)",
}


def _check(cid, label, ok, detail, blocking=True):
    return {"id": cid, "label": label, "ok": bool(ok), "detail": detail,
            "blocking": bool(blocking)}


def preflight():
    """Run every credential/funding check. Never raises.

    Returns {"ok": bool, "balance": float|None, "signer": str|None,
             "checks": [{id, label, ok, detail, blocking}]}

    `ok` is True only when every BLOCKING check passed — that is the condition
    the dashboard requires before it will let anyone switch to live trading.
    Non-blocking checks are advisory (e.g. zero allowances, which the relayer
    sets on a proxy account's first trade).
    """
    checks = []
    pk = os.getenv("POLYMARKET_PK", "")
    try:
        sig = int(os.getenv("POLYMARKET_SIG_TYPE", "0"))
    except ValueError:
        sig = 0
    funder = os.getenv("POLYMARKET_FUNDER", "")

    checks.append(_check(
        "signing_key", "Signing key present",
        bool(pk),
        "POLYMARKET_PK is set" if pk else "POLYMARKET_PK is not set — set it as a Fly secret"))
    if not pk:
        return {"ok": False, "balance": None, "signer": None, "checks": checks}

    try:
        from py_clob_client_v2.client import ClobClient
        from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
    except Exception as e:
        checks.append(_check(
            "clob_library", "CLOB client library available", False,
            f"{type(e).__name__}: {e} — this build cannot place orders"))
        return {"ok": False, "balance": None, "signer": None, "checks": checks}

    # A proxy account (sig 1/2/3) funds from an address distinct from the signer.
    # funder == signer means the config claims a proxy but points at the EOA —
    # the exact misconfiguration that reads cannot detect but orders reject on.
    kwargs = {"key": pk, "chain_id": 137}
    if sig:
        kwargs["signature_type"] = sig
        kwargs["funder"] = funder

    try:
        client = ClobClient("https://clob.polymarket.com", **kwargs)
        client.set_api_creds(client.create_or_derive_api_key())
        signer = client.get_address()
        checks.append(_check("auth", "CLOB authentication", True,
                             f"API creds derived against the live CLOB (signer {signer})"))
    except Exception as e:
        checks.append(_check("auth", "CLOB authentication", False,
                             f"{type(e).__name__}: {e}"))
        return {"ok": False, "balance": None, "signer": None, "checks": checks}

    if sig:
        good = bool(funder) and funder.lower() != signer.lower()
        checks.append(_check(
            "funder", f"Funder wallet ({SIG_NAMES.get(sig, 'unknown')})", good,
            f"funder {funder}" if good else
            "SIG_TYPE claims a proxy wallet but FUNDER is unset or equals the signer. "
            "Set FUNDER to the address under Profile -> 'Your Polymarket Wallet Address', "
            "or set SIG_TYPE=0 for a genuine raw EOA."))
        if not good:
            return {"ok": False, "balance": None, "signer": signer, "checks": checks}
    else:
        checks.append(_check("funder", "Funder wallet (raw EOA)", True,
                             "signature type 0 — the signer funds directly"))

    balance = None
    try:
        res = client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=sig))
        # USDC is 6-decimal; the API returns integer micro-units as a string.
        balance = int(res.get("balance", "0")) / 1e6
        allowances = res.get("allowances", {})
        checks.append(_check(
            "collateral", "Wallet collateral", balance > 0,
            f"${balance:,.2f} USDC" if balance > 0
            else "no collateral — deposit USDC before going live"))
        if allowances:
            any_set = any(int(a) > 0 for a in allowances.values())
            checks.append(_check(
                "allowances", "Exchange allowances", any_set,
                "set" if any_set else
                "all zero — expected before the first trade (the relayer sets them then). "
                "If an order is later rejected for balance, check this first.",
                blocking=False))
    except Exception as e:
        checks.append(_check("collateral", "Wallet collateral", False,
                             f"{type(e).__name__}: {e}"))

    ok = all(c["ok"] for c in checks if c["blocking"])
    return {"ok": ok, "balance": balance, "signer": signer, "checks": checks}


def main():
    paper = os.getenv("PAPER_MODE", "true").lower() != "false"
    try:
        sig = int(os.getenv("POLYMARKET_SIG_TYPE", "0"))
    except ValueError:
        sig = 0

    print("=" * 62)
    print("CONFIG")
    print("=" * 62)
    print(f"  PAPER_MODE : {paper}   (boot default; the dashboard can change it live)")
    print(f"  SIG_TYPE   : {sig}  ({SIG_NAMES.get(sig, 'unknown')})")
    print(f"  FUNDER     : {os.getenv('POLYMARKET_FUNDER') or '(unset)'}")

    result = preflight()

    print("\n" + "=" * 62)
    print("READINESS")
    print("=" * 62)
    for c in result["checks"]:
        mark = "ok  " if c["ok"] else ("FAIL" if c["blocking"] else "note")
        print(f"  [{mark}] {c['label']}: {c['detail']}")

    print("\n" + "=" * 62)
    print("REMAINING MANUAL STEPS")
    print("=" * 62)
    print("  [ ] $1 manual BUY+SELL round-trip")
    print("  [ ] verify _read_fill against the raw get_order response")
    print("  [ ] set STARTING_BANKROLL to the real deposit; re-seed ledger")
    print()
    print("READY FOR LIVE" if result["ok"] else "NOT READY — see FAIL above")
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
