#!/usr/bin/env python3
"""Re-score the causal ledger against the SETTLEMENT source (METAR/IEM, quantised
onto each city's actual settlement grid) instead of Open-Meteo archive.

metar.py's own docstring warns Open-Meteo ERA5 "was found to differ by up to ~1C
and flip whole-degree-Celsius outcomes". The causal backtest scored outcomes from
Open-Meteo. This measures how many of the 1,956 outcomes change.

Writes ledger_365d_resettled.csv with both outcomes side by side.
"""
import csv, json, os, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import metar  # noqa: E402

SRC = "scripts/_causal_cache/ledger_365d.csv"
OUT = "scripts/_causal_cache/ledger_365d_resettled.csv"
CACHE = "scripts/_causal_cache/settle"
os.makedirs(CACHE, exist_ok=True)


def settled(city, date_str):
    p = os.path.join(CACHE, f"{city.replace(' ','_')}_{date_str}.json")
    if os.path.exists(p):
        try:
            return json.load(open(p)).get("v")
        except Exception:
            pass
    v = None
    for _ in range(3):
        try:
            v = metar.final_extreme_f(city, date_str, True) \
                if hasattr(metar, "final_extreme_f") else \
                metar.resolved_extreme_f(city, date_str, True,
                                         require_settlement_source=True)
            break
        except Exception:
            time.sleep(2)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"v": v}, f)
    os.replace(tmp, p)
    return v


def no_won(lo, hi, act):
    """NO wins iff the settled max is OUTSIDE [lo,hi]."""
    if act is None:
        return None
    inside = True
    if lo is not None and act < lo:
        inside = False
    if hi is not None and act > hi:
        inside = False
    return not inside


def main():
    rows = list(csv.DictReader(open(SRC)))
    keys = sorted({(r["city"], r["target_date"]) for r in rows})
    print(f"trades {len(rows)}  unique city-days {len(keys)}", flush=True)

    vals = {}
    done = 0
    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = {ex.submit(settled, c, d): (c, d) for c, d in keys}
        for f in as_completed(futs):
            k = futs[f]
            try:
                vals[k] = f.result()
            except Exception:
                vals[k] = None
            done += 1
            if done % 200 == 0:
                print(f"  settled {done}/{len(keys)}", flush=True)

    out = []
    flips = unver = 0
    for r in rows:
        k = (r["city"], r["target_date"])
        s = vals.get(k)
        lo = float(r["bucket_low"]) if r["bucket_low"] not in ("", "None") else None
        hi = float(r["bucket_high"]) if r["bucket_high"] not in ("", "None") else None
        w2 = no_won(lo, hi, s)
        w1 = r["won"] == "True"
        r2 = dict(r)
        r2["settled_f"] = s
        r2["won_settlement"] = "" if w2 is None else str(w2)
        r2["flipped"] = "" if w2 is None else str(w2 != w1)
        if w2 is None:
            unver += 1
        elif w2 != w1:
            flips += 1
        out.append(r2)

    with open(OUT, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0].keys()))
        w.writeheader(); w.writerows(out)

    ver = [r for r in out if r["won_settlement"] != ""]
    l1 = sum(1 for r in out if r["won"] != "True")
    l2 = sum(1 for r in ver if r["won_settlement"] != "True")
    print(f"\nwrote {OUT}")
    print(f"unverifiable (no settlement obs): {unver} ({unver/len(out)*100:.1f}%)")
    print(f"verifiable: {len(ver)}")
    print(f"\nOpen-Meteo basis : losses={l1}  win%={(len(out)-l1)/len(out)*100:.2f}%")
    if ver:
        print(f"SETTLEMENT basis : losses={l2}  win%={(len(ver)-l2)/len(ver)*100:.2f}%  (of verifiable)")
    print(f"OUTCOME FLIPS    : {flips} ({flips/max(1,len(ver))*100:.2f}% of verifiable)")


if __name__ == "__main__":
    main()
