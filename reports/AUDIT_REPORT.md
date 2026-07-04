# Forensic Audit — stormedge Polymarket Weather Bot

**Date:** 2026-07-04  **Scope:** full scientific audit, repair, and revalidation
**Primary evidence DB:** `data/deployed_bot.db` (production paper record: 44,879 signals, 20 trades, 6,236 scan rows)
**Method:** every historical trade reconstructed from first principles against the Open-Meteo ERA5 archive; every reported number re-derived from source.

---

## Bottom line

The bot's **forecasting direction is real** — reconstructed against realized temperatures, its NO bets were correct on **9 of 10 closed trades** (and 17 of 20 overall). But the **reported P&L was inflated by two execution bugs**, and the **probability model is materially overconfident**, so the *magnitude* of the edge is not trustworthy and would not reproduce live as booked.

| Metric | As reported (DB) | Corrected | Why it changed |
|---|---|---|---|
| Closed-trade P&L | **+$17.42** | **+$13.80** | Trade 120 flips win→loss under the fixed bucket parser |
| Open trades (mark-to-settlement) | (unrealized) | **+$6.48** | 7 win / 3 lose (126, 137, 138 will settle NO-loss) |
| Total corrected | — | **+$20.28** | on a $40 starting bankroll, n=20, **all NO** |
| Closed win rate (corrected buckets) | 10/10 | **9/10** | |
| Brier score (44,879 signals) | — | **0.167** | better than coin-flip (0.25), but see calibration |

**Verdict on edge:** *Directionally defensible, statistically unproven, and economically overstated.* n=20 all-NO trades over 3 days is far too small to claim a Sharpe or a real edge, and the reliability curve shows the model's probabilities are ~2–2.5× too confident. Do **not** scale capital. The success metric remains calibration, not P&L (consistent with prior finding).

---

## Confirmed bugs (each has a regression test)

### BUG 1 — Inverted order-book best bid/ask read *(SEVERITY: CRITICAL, live-money)*
`scanner.get_realtime_price_status` read `asks[0]` as best ask and `bids[0]` as best bid.
**Verified live against 6 markets (2026-07-04):** the CLOB `/book` endpoint returns **asks DESCENDING** (worst price at index 0) and **bids ASCENDING** (worst at index 0). So the code read the *worst* prices as best on every market.

- Impact: fake ~98% spread on every market → the live-spread entry gate (`MAX_ENTRY_SPREAD_FRACTION=0.15`) would **block all entries**; every mid, stop-loss and exit price was corrupted. The bug survived undetected only because `(0.99+0.01)/2 = 0.50` coincidentally approximates a mid by symmetry.
- Fix: `_best_ask_bid_from_book()` computes `best_ask=min(asks)`, `best_bid=max(bids)` — never trusts array order. Verified live: spreads now 0.001–0.02, mids realistic (0.30/0.65/0.03).
- Tests: `tests/test_scanner.py::TestOrderBookBestPriceSelection` (4 cases incl. one-sided, empty, crossed book).

### BUG 2 — Phantom $0.999 edge-decay exit fills *(SEVERITY: HIGH, P&L integrity)*
All 5 "Edge decayed" exits in the DB (trades 120, 121, 124, 127, 130) were booked at a NO bid of **~$0.999** — a price **never once observed with real size**: the maximum NO price across all 44,879 logged signals was **0.81**.

- Mechanism: each fired 10–39 h *after* the market's target date had passed and after it left the scan window. On a resolving book the only resting quotes are extreme and shallow; `exit_fill = bid_price` booked a fill there. The resolution-settlement path (which pays the true $1/$0) never fired for these (0 resolution rows).
- These 5 were genuine economic wins (verified: all 5 actual temps outside the bucket), but booking them as $0.999 taker fills is not a reproducible execution model — live, that bid has no depth.
- Fix: `_check_exit_for_position` now **holds for resolution** once `target_date` has passed instead of taking the paper market-exit path. Settlement pays the true $1/$0.
- Tests: `tests/test_executor.py::TestTargetDatePassedGuard`.

### BUG 3 — Celsius zero-width bucket *(SEVERITY: HIGH — already fixed in v2; now guarded)*
Under parser v1, exact Celsius questions (e.g. "be 33°C") parsed to a **zero-width** bucket `(91.4, 91.4)` instead of a rounding-tolerant band. A 1°C resolution window ≈ 1.8°F, but the ±0.5°F padding on a zero-width bucket covers only 1°F → P(YES) collapsed → fake NO edge.

- Independently confirmed: **32.8% of all signal rows** carry a stale bucket differing from the market's canonical value (matches the prior ~29–33% estimate).
- Corrected-parser impact: **trade 120 (Hong Kong 31°C, actual 87.1°F) flips from a booked +$1.63 win to a −$2.00 loss** — the −$3.62 that reduces closed P&L from $17.42 to $13.80.
- Fahrenheit exact buckets `(90,90)` are *correct* — the ±0.5°F padding matches 1°F granularity. The fix was Celsius-specific and is already live (v2).
- Tests: `tests/test_scanner.py::TestExactBucketResolutionWidth` (asserts effective padded window covers resolution granularity for both units).

---

## Calibration — the decisive finding

Reliability table over 44,879 signals (predicted bucket prob vs realized hit rate):

| Predicted | Observed | Overconfidence |
|---|---|---|
| 5.3% | **11.7%** | 2.2× |
| 13.9% | **30.4%** | 2.2× |
| 23.3% | **35.3%** | 1.5× |
| 33.7% | **54.5%** | 1.6× |
| 43.9% | **79.2%** | 1.8× |

The model **systematically understates** how often the narrow buckets it bets against actually come true — by roughly **2×** in exactly the low-probability region where the bot places all its NO bets. The claimed NO edge is therefore partly illusory: the bot thinks a bucket is a 5% longshot when it hits ~12% of the time. (Note: `std(z)=0.69` on the ensemble mean says "sigma too wide," but the decision-relevant *bucket-probability* reliability says overconfident — the ±0.5°F padding and bucket-width handling are where the overconfidence enters. Rebuild sigma against the reliability curve, not std(z).)

Per-model accuracy (n=213): ecmwf MAE 2.03°F, icon 2.05, gem 2.47, gfs 2.72, **jma 3.44°F (bias −1.90°F)** — JMA is the weakest and coldest; consider demoting further.

---

## What was verified and refuted

- **CONFIRMED:** inverted order-book read; phantom 0.999 exits; Celsius zero-width bucket; ~33% mutable-bucket contamination; probability overconfidence.
- **REFUTED / benign:** resolution settlement PnL formula (correct: pays $1/share on win, −size on loss); YES/NO outcome mapping (`outcome.upper() == side`, consistent); exit re-evaluation is live-forecast, not look-ahead; bankroll ledger is internally consistent (Σamounts = final balance); MODEL_BIAS_CORRECTIONS sign is correct (magnitudes are stale, not wrong).
- **RISK (unresolved):** date/timezone convention — a market "on June 29" has `endDate=2026-06-29T00:00:00Z`; the reconstruction is internally consistent with how the bot trades but the "which local day" convention was not independently confirmed against a Polymarket resolution source. Flagged in the risk register.

---

## Machine-readable outputs (in `reports/`)
- `corrected_trades.csv` — per-trade: v1 vs corrected bucket, actual temp, NO-win, booked vs corrected P&L, note.
- `corrected_pnl.csv` — summary metrics.
- `trade_reconstruction.csv` — full first-principles reconstruction of all 20 trades.
- `calibration_results.csv` — reliability table + overconfidence ratios + Brier.
- `forecast_failure_dataset.csv` — per-trade ensemble mean, actual, z-score, per-model error, failure category.

## Engineering changes
- `scanner.py`: `_best_ask_bid_from_book()` (min/max book read).
- `executor.py`: `_target_date_passed()` guard — hold for resolution after target date.
- `tests/`: 48 → **61 passing** (added 13 regression tests across 3 bug classes).

## Recommendations (evidence-based, in priority order)
1. **Do not scale capital.** n=20 all-NO over 3 days is not a validated edge.
2. **Recalibrate sigma against the reliability curve**, not std(z) — inflate low-p bucket probabilities ~2× before trusting any NO edge.
3. **Backfill immutable `markets` bucket metadata** into the deployed DB (the table was added after this history — hence 33% mutable rows).
4. Re-verify the **first real live fill** parser (`_read_fill`) per the standing live-trading gate before any real money.
5. Add depth-aware exit modeling (size-weighted book walk) if paper exits are ever re-enabled pre-resolution.
