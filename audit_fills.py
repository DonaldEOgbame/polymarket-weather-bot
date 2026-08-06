"""Every historical fill against the quote the decision was made on.

The trade row records `fill_price` and nothing else, so a 34-cent slippage and a
0-cent slippage are indistinguishable in the ledger. The quote lives in the
replay log; joining the two is the only way to ask "what did execution actually
cost us, and on which trades".

The question that matters is not P&L. It is POST-FILL EDGE: whether the bet that
existed after execution was still the bet the model wanted. A trade can be
+$0.02 realised and still have been a losing bet at the moment it was placed —
the 2026-08-06 Austin fill is 86.5% to win and has an expected value of −$0.71.
Outcome hides it; this does not.

    python audit_fills.py                      # local DB
    DB_PATH=/data/bot.db python audit_fills.py # on the box
"""
import argparse
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db import fetch_query
import config as C


def collect(mode="live"):
    """One row per trade, joined to the replay row that decided it.

    Matched on (market_id, decision) with the replay row nearest in time BEFORE
    the entry — a market is evaluated every scan cycle, so market_id alone
    returns dozens of rows and only the one that actually fired is the quote the
    trade was made against."""
    trades = fetch_query(
        "SELECT id, market_id, city, side, fill_price, size_usdc, model_prob, "
        "       edge, entry_time, target_date, status, pnl "
        "  FROM trades WHERE mode=? ORDER BY id", (mode,))

    out = []
    for t in trades:
        rows = fetch_query(
            "SELECT no_price, yes_price, spread_fraction, ask_depth_usd, "
            "       usable_depth_usd, stake_usd, walked_vwap, timestamp, "
            "       prob_post_floor, edge_post_fee "
            "  FROM replay_signals "
            " WHERE market_id=? AND decision LIKE 'BUY%' AND timestamp <= ? "
            " ORDER BY timestamp DESC LIMIT 1",
            (t["market_id"], t["entry_time"]))
        r = rows[0] if rows else None

        quoted = None
        if r:
            quoted = r["no_price"] if t["side"] == "NO" else r["yes_price"]
        fill = t["fill_price"]
        slippage = (fill - quoted) if (quoted is not None and fill is not None) else None

        prob = t["model_prob"]
        fair = (1.0 - prob) if (prob is not None and t["side"] == "NO") else prob
        post_fill_edge = (fair - fill) if (fair is not None and fill is not None) else None
        shares = (t["size_usdc"] / fill) if fill else None

        out.append({
            "id": t["id"], "city": t["city"], "side": t["side"],
            "entry_time": t["entry_time"], "status": t["status"], "pnl": t["pnl"],
            "quoted": quoted, "fill": fill, "slippage": slippage,
            "size_usdc": t["size_usdc"], "shares": shares,
            "edge_at_decision": t["edge"], "fair_value": fair,
            "post_fill_edge": post_fill_edge,
            "expected_value": (post_fill_edge * shares
                               if post_fill_edge is not None and shares else None),
            "ask_depth_usd": r["ask_depth_usd"] if r else None,
            "usable_depth_usd": r["usable_depth_usd"] if r else None,
            "size_pct_of_depth": (100.0 * t["size_usdc"] / r["ask_depth_usd"]
                                  if r and r["ask_depth_usd"] else None),
            "would_refuse_now": _would_refuse(r, t),
        })
    return out


def _would_refuse(r, t):
    """Whether today's depth gate would have blocked this entry.

    Uses total ask depth when usable depth was never recorded — every trade
    before 2026-08-06 predates that column. That makes this an OPTIMISTIC
    estimate of the gate's bite: usable depth is always <= total, so a trade
    marked "would pass" on total depth might still be refused on usable."""
    if not r:
        return None
    depth = r["usable_depth_usd"]
    approx = depth is None
    if approx:
        depth = r["ask_depth_usd"]
    if depth is None:
        return "unknown-depth"
    stake = r["stake_usd"] or t["size_usdc"] or 0.0
    refused = depth < C.MIN_DEPTH_MULTIPLE * stake
    return ("REFUSE" if refused else "pass") + (" (total-depth estimate)" if approx else "")


def render(rows):
    bad = [r for r in rows if r["post_fill_edge"] is not None and r["post_fill_edge"] < 0]
    slipped = [r for r in rows if r["slippage"] is not None
               and abs(r["slippage"]) > C.MAX_FILL_SLIPPAGE_ALERT]
    refused = [r for r in rows if str(r["would_refuse_now"]).startswith("REFUSE")]

    lines = [
        "# Fill audit — every live-era trade against its quote",
        "",
        f"{len(rows)} trades. **{len(bad)}** had NEGATIVE edge at the fill. "
        f"**{len(slipped)}** filled more than "
        f"{C.MAX_FILL_SLIPPAGE_ALERT:.2f} from the quote. "
        f"**{len(refused)}** would be refused by today's depth gate.",
        "",
        "Post-fill edge is the number that matters, not P&L: a trade can realise "
        "a profit and still have been a losing bet at the moment it was placed. "
        "Any trade whose slippage exceeded its modelled edge was one.",
        "",
        "| # | city | quoted | fill | slippage | edge@decision | fair | edge@fill | EV | ask depth | size % | depth gate |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    fmt = lambda v, p=4: "—" if v is None else f"{v:.{p}f}"
    for r in rows:
        flag = " ⚠️" if (r["post_fill_edge"] is not None and r["post_fill_edge"] < 0) else ""
        lines.append(
            f"| {r['id']} | {r['city']}{flag} | {fmt(r['quoted'])} | {fmt(r['fill'])} | "
            f"{fmt(r['slippage'])} | {fmt(r['edge_at_decision'])} | {fmt(r['fair_value'])} | "
            f"{fmt(r['post_fill_edge'])} | "
            f"{'—' if r['expected_value'] is None else '$%+.2f' % r['expected_value']} | "
            f"{'—' if r['ask_depth_usd'] is None else '$%.2f' % r['ask_depth_usd']} | "
            f"{'—' if r['size_pct_of_depth'] is None else '%.1f%%' % r['size_pct_of_depth']} | "
            f"{r['would_refuse_now'] or '—'} |")

    if bad:
        lines += ["", "## Losing bets at the moment of execution", ""]
        for r in bad:
            lines.append(
                f"- **{r['city']} (#{r['id']})** — paid {r['fill']:.4f} for a token "
                f"worth {r['fair_value']:.4f}. The decision claimed "
                f"{r['edge_at_decision']:+.4f}; execution made it "
                f"{r['post_fill_edge']:+.4f}, an expected "
                f"${r['expected_value']:+.2f} on {r['shares']:.2f} shares. "
                f"Status: {r['status']}, realised P&L "
                f"{'—' if r['pnl'] is None else '$%+.2f' % r['pnl']}.")
        lines += ["",
                  "Realised P&L does not redeem these. A bet that is 86.5% to win "
                  "and pays 1.9% is still negative expectation, and winning it "
                  "is the outcome that hides the defect."]

    clean = [r for r in rows if r["slippage"] is not None and abs(r["slippage"]) < 1e-9]
    lines += ["", "## Execution quality", "",
              f"- {len(clean)} of {len(rows)} trades filled AT the quote.",
              f"- {len(slipped)} exceeded the {C.MAX_FILL_SLIPPAGE_ALERT:.2f} alert threshold.",
              "",
              "Clean fills to date were luck rather than design: depth was never "
              "gated on, so nothing prevented any of them from walking the book "
              "the way the Austin fill did.",
              ""]
    return "\n".join(lines)


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="live")
    ap.add_argument("--out", default="reports/fill-audit.md")
    ap.add_argument("--json-out", default="reports/fill-audit.json")
    args = ap.parse_args()

    rows = collect(args.mode)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.json_out, "w") as fh:
        json.dump(rows, fh, indent=2)
    md = render(rows)
    with open(args.out, "w") as fh:
        fh.write(md)
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
