# StormEdge — Strategy Briefing for External Review

**Date:** 2026-09-16 · **Repo:** polymarket-weather-bot · **Deployed app:** `stormedgev2` (Fly.io, region `arn`)
**Status:** PAPER mode, live and scanning. Bankroll $100, $7 flat stake, 10 concurrent slots, stop-loss OFF.

This document is self-contained. It exists so a reviewer with no prior exposure to this
codebase can interrogate the strategy's statistics and challenge the conclusions.

---

## 1. What the bot does

Polymarket lists daily temperature markets per city, e.g.
*"Will the highest temperature in Madrid be 26°C or below on September 16?"*
Each city-day is split into ~11 mutually exclusive temperature **buckets**, each its own
binary market with YES/NO tokens settling at $1/$0.

The bot buys **NO** on HIGH-temperature buckets when its weather model says the day's
actual high will land **far outside** that bucket. It holds to settlement.

Concretely: if the forecast high for Madrid is 75°F and a bucket asks "will the high be
82.0–82.8°F?", NO on that bucket should settle at $1. The bot buys NO at $0.90–0.95 and
collects the remaining 5–10¢.

**This is not a mispricing strategy.** Entering at 0.90–0.95 means the market already
mostly agrees. The edge claim is that the bot's forecast gate identifies cases where the
remaining 5-10 cents is still underpriced relative to the true probability.

### Deployed configuration (fly.toml + settings table)

| Parameter | Value | Meaning |
|---|---|---|
| `TRADE_HIGH_MARKETS` / `TRADE_LOW_MARKETS` | true / false | HIGH buckets only |
| `ENABLE_YES_ENTRIES` | false | NO side only |
| `MIN_ENTRY_PRICE` / `MAX_ENTRY_PRICE` | 0.90 / 0.95 | entry band |
| `FORECAST_MARGIN_F` | 5.0 | see note below → effective clearance 4.5°F |
| `BUCKET_EDGE_PAD_F` | 0.5 | subtracted from the config margin |
| `REQUIRE_SAME_DAY` | true | entry must be on the target local day |
| `MAX_HOURS_TO_RESOLUTION` | 16.0 | |
| `ONE_TRADE_PER_CITY_DATE` | true | one bucket per city-day |
| `FIXED_POSITION_SIZE` | **$7.00** | flat stake, no Kelly |
| `MAX_CONCURRENT_POSITIONS` | **10** | |
| `ENABLE_STOP_LOSS` | **false** | changed 2026-09-16, see §6 |
| `ENABLE_PHYSICS_EXIT_GATE` | true | loss exits gated on observations, not price |
| `TAKE_PROFIT_PRICE` | 0.98 | still armed — see §9 open questions |
| `EXCLUDED_CITIES` | 26 cities | derived from 365d forecast-vs-station error |

**The gate:** enter only if the day-before forecast clears the bucket edge by ≥4.5°F in the
NO direction. For a HIGH bucket `[lo, hi]`, NO requires `forecast_high ≤ lo − 4.5`.

---

## 2. How the numbers below were produced

Two backtests exist. **They disagree, and the difference matters.**

### The OLD backtest — `scripts/backtest_forecast_margin.py` (do not trust)
Gates on the **actual realized daily extreme**. Its own docstring calls this a
"perfect forecast proxy" and warns it *"OVERSTATES qualifying trades vs. what a live bot
with a real forecast ensemble would have seen."* It reports 100% win rates because the
gate already knows the answer. It also used a **hardcoded 10-city allowlist** and only ever
covered 21 days.

### The NEW backtest — `scripts/backtest_causal_deployed.py` (used throughout)
Replaces the look-ahead gate with a genuinely causal forecast:

* **Forecast source:** Open-Meteo `previous-runs` API, variable
  `temperature_2m_previous_day1` — the forecast **issued ~1 day before** the target date.
  The gate can be, and often is, wrong.
* **Prices:** the real Polymarket trade tape (`data-api.polymarket.com/trades`), iterated
  in ascending timestamp order. Entry = first print inside the 0.90–0.95 band that also
  satisfies same-day and ≤16h constraints.
* **Settlement:** realized extreme from Open-Meteo archive, used **only for scoring**.
* **Discovery:** Gamma API paginated by date range (its offset cap is ~2000).
* **Fees:** `0.05 × p × (1−p)` per share on entry, matching `executor.py`.
* **Coverage:** 2025-09-17 → 2026-09-14, **1,956 trades, 26 cities**.

Weather data was bulk-fetched via `scripts/prefetch_wx_batch.py`, which batches
multi-location + date-range into ~12 API calls. Batched values were validated against
per-city calls: **6/6 samples matched to 0.00°F**.

---

## 3. Headline results (causal, full year)

| Window | n | Wins | Losses | Win % | P&L ($3 stake) | Return/trade |
|---|---|---|---|---|---|---|
| 7d | 41 | 41 | 0 | 100.00% | $9.27 | 7.54% |
| 14d | 87 | 87 | 0 | 100.00% | $19.26 | 7.38% |
| 30d | 211 | 209 | 2 | 99.05% | $39.04 | 6.17% |
| 90d | 728 | 707 | 21 | 97.12% | $88.98 | 4.07% |
| **365d** | **1956** | **1916** | **40** | **97.96%** | **$294.89** | **5.03%** |

Short windows flatter the strategy. **Treat ~98% as the true rate.**

**Market disposition** (why the other 51,714 eligible markets were not traded):
`margin_gate` 41,747 · `no_in_band_print` 9,967. Only ~2.5% of markets become trades.

### Economics
| | |
|---|---|
| Avg win | **+$0.217** (on $3) |
| Avg loss | **−$3.012** |
| Loss:win ratio | **13.9 : 1** |
| Breakeven win rate | **~93.4%** |
| Actual | **97.96%** |
| 95% CI | **~97.2% – 98.6%** |

The whole CI is profitable, but the margin over breakeven is only ~4.6pp.

---

## 4. Loss distribution — the central finding

**All 40 losses share one cause: the forecast under-predicted the high.**

```
ALL 40 losses had forecast bust >= +4.5F : TRUE
```

Sorted bust magnitudes (actual − forecast, °F):
```
4.5 4.5 4.8 4.9 5.0 5.1 5.1 5.1 5.2 5.2 5.2 5.3 5.4 5.4 5.7 5.7 5.7 5.8 5.9 5.9
5.9 5.9 5.9 6.1 6.2 6.2 6.4 6.5 6.5 6.6 6.7 6.7 6.9 7.4 7.6 7.7 8.0 9.8 10.5 20.6
```

**But a bust is not sufficient for a loss.** 221 of 1,916 winners (11.5%) also busted ≥4.5°F
and still won, because the bucket happened to sit elsewhere:

> **P(loss | bust ≥ 4.5°F) = 40 / 261 = 15.3%**

So the causal chain is: *forecast busts warm* → *if the bucket happens to lie in the path
of the error* → *loss*. The second step is close to geometric luck.

### Systematic warm bias
| | Value |
|---|---|
| Mean error (actual − forecast) | **+2.03°F** |
| Median | +1.70°F |
| Std dev | 2.42°F |
| Busts ≥ +4.5°F | **13.3% of all trades** |

The forecast runs **2°F cold on average**. The 4.5°F gate is absorbing this bias rather
than budgeting for it — only ~2.5°F of genuine margin remains.

### Per-city forecast bias (this is where the structure is)

| City | n | Mean bias | SD | Losses |
|---|---|---|---|---|
| Toronto | 96 | **+3.25** | 3.69 | 4 |
| Busan | 200 | +2.82 | 2.79 | **9** |
| Warsaw | 23 | +2.80 | 2.25 | 0 |
| Seattle | 83 | +2.67 | 2.34 | 2 |
| Miami | 65 | +2.55 | 3.02 | 3 |
| Tokyo | 215 | +2.40 | 2.27 | 7 |
| Helsinki | 8 | +2.39 | 2.32 | **2** |
| Moscow | 66 | +2.33 | 1.92 | 4 |
| … | | | | |
| Qingdao | 60 | +0.62 | 1.98 | 0 |
| Wellington | 82 | **+0.62** | **1.04** | 0 |

**Bias varies 5× across cities but the gate is flat.** A 5.5°F margin in Wellington
(bias +0.6, sd 1.0) is safe; the same 5.5°F in Toronto (bias +3.3, sd 3.7) is near a
coin flip. This is the strongest candidate explanation for the loss set.

### Seasonality
| Month | n | Losses | Win % |
|---|---|---|---|
| 2025-12 | 19 | 0 | 100.0% |
| 2026-03 | 196 | 5 | 97.4% |
| 2026-04 | 400 | 6 | 98.5% |
| 2026-06 | 273 | 8 | 97.1% |
| **2026-07** | **276** | **12** | **95.7%** |
| 2026-08 | 243 | 4 | 98.4% |

July is worst (95.7%). Winter months look perfect only because volume was tiny
(Dec–Feb: 131 trades total — Polymarket barely listed these markets before 2026).

### Win rate by entry price — note the skew
| Band | n | Losses | Win % |
|---|---|---|---|
| 0.90–0.91 | 372 | 12 | 96.77% |
| 0.91–0.92 | 208 | 8 | 96.15% |
| 0.92–0.93 | 211 | 4 | 98.10% |
| 0.93–0.94 | 278 | 8 | 97.12% |
| **0.94–0.95** | **887** | **8** | **99.10%** |

The cheapest entries are the *worst*, not the best. 0.94–0.95 carries 45% of volume at
99.10%. Market price appears to contain real information the gate does not.

### Win rate by time-to-resolution — no signal
| hours_left | n | Losses | Win % |
|---|---|---|---|
| 0–4h | 254 | 4 | 98.43% |
| 4–8h | 627 | 16 | 97.45% |
| 8–12h | 435 | 11 | 97.47% |
| 12–16h | 640 | 9 | 98.59% |

---

## 5. What does NOT separate wins from losses

At entry, on every knob currently exposed:

| Feature | Win median | Loss median |
|---|---|---|
| `forecast_margin_f` | 5.80 | **5.65** |
| `entry_price` | 0.94 | 0.92 |
| `hours_left` | 8.86 | 8.13 |

**Raising `FORECAST_MARGIN_F` destroys more profit than it saves:**

| Gate | n | Losses | Win % | P&L |
|---|---|---|---|---|
| 4.5°F (current) | 1956 | 40 | 97.96% | **$294.89** |
| 6.0°F | 882 | 14 | 98.41% | $138.51 |
| 7.0°F | 473 | 5 | 98.94% | $80.80 |
| 10.0°F | 58 | 1 | 98.28% | $8.46 |

−65% losses for −53% P&L. Bad trade.

### The one thing that DOES work: a per-city z-score gate
Require `(margin − city_bias) / city_sd ≥ k`.
**Walk-forward validated** — bias/sd fitted only on data strictly preceding each month:

| k | n | Losses | Win % | P&L | Volume kept | $/trade |
|---|---|---|---|---|---|---|
| baseline | 1956 | 40 | 97.96% | $294.89 | 100% | $0.1508 |
| 1.00 | 1658 | 28 | 98.31% | $266.71 | 85% | $0.1609 |
| 1.50 | 1219 | 19 | 98.44% | $196.26 | 62% | $0.1610 |
| **2.00** | **815** | **8** | **99.02%** | $145.80 | 42% | **$0.1789** |

At k=2.0: **−80% losses, +19% profit per trade**, but −51% total P&L (fewer trades).
Worth shipping only when capacity-bound rather than signal-bound. NOT currently deployed.

---

## 6. Stop-loss: measured, and disabled

The live settings table had `ENABLE_STOP_LOSS=true` at `STOP_LOSS_PCT=0.50`. Replaying
the real post-entry tape for all 1,956 trades (0 missing):

| Stop | Fired | % of all | Of winners | Of losers |
|---|---|---|---|---|
| 30% | 362 | 18.51% | 345 (18.0%) | 17 (42.5%) |
| 40% | 275 | 14.06% | 259 (13.5%) | 16 (40.0%) |
| **50%** | **220** | **11.25%** | **205 (10.7%)** | **15 (37.5%)** |
| 60% | 189 | 9.66% | 177 (9.2%) | 12 (30.0%) |

**93% of stop-outs would have been winners.**

```
hold to settlement : +$315.51
with 50% stop      :  −$19.02
stop costs         : −$334.53
```

Drawdown carries almost no outcome information:
| | Median DD | p90 | Max |
|---|---|---|---|
| Winners (1916) | 6.4% | 54.5% | 99.9% |
| Losers (40) | 21.8% | 99.9% | 99.9% |

Verified these are real liquidity, not stray ticks: London 2025-11-21 had **92 prints** at
≤50% of entry, sizes up to $550 — and still settled a winner.

This independently reproduces a comment already in `config.py:860-872` written after the
"Qingdao" incident, which retired the percentage stop on 3 observations. **Now confirmed
at n=1,956.** Stop-loss was set to `false` on 2026-09-16.

---

## 7. Capacity modelling (the real constraint)

Raw backtest P&L ignores live constraints. Applying `ONE_TRADE_PER_CITY_DATE`
(1,956 → 1,419 trades), concurrency caps, cash limits, and hold-to-settlement:

| Bankroll | Stake | Max pos | Taken | P&L/yr | /month | /week | Max DD |
|---|---|---|---|---|---|---|---|
| $10.80 | $3 | 4 | 764 | +$189 | +$16 | +$3.6 | 13.8% |
| $100 | $5 | 20 | **1419 (all)** | +$542 | +$45 | +$10.4 | 5.13% |
| **$100** | **$7** | **10** | **1279** | **+$685** | **+$57** | **+$13.2** | **5.95%** |
| $1000 | $40 | 20 | 1419 | +$4,335 | +$361 | +$83 | — |

Note $100/$3/4pos and $500/$3/4pos give **identical** results — with a fixed stake,
extra bankroll does nothing. **Slots unlock volume; stake unlocks magnitude.**

**Liquidity check** (traded volume within ±30min at or below entry price):
$3 fits 97.8% · $5 fits 96.0% · **$7 fits 94.4%** · $10 fits 90.1%.

Deployed config takes 1,279 of 1,419 eligible (140 cap-blocked), 98.36% win rate,
$100 → $585.91 over the year, **2 down weeks out of 52**.

---

## 8. Known weaknesses of this analysis

1. **Entry = first in-band print.** No slippage, no partial fills, no queue position.
   Real fills are worse. This repo has a documented history of execution bugs
   (`entry-limit-walked-vwap-fix`).
2. **One climate year.** 2025-09 → 2026-09. A single seasonal cycle. July already shows
   95.7% vs 97.96% annual.
3. **Early period is sparse.** Only 24 trades before 2025-12; Polymarket coverage expanded
   through 2026. Annualized figures lean on recent months.
4. **`ONE_TRADE_PER_CITY_DATE` not modelled in the raw ledger** — it is applied only in the
   capacity sim. The raw 1,956 includes up to 3 trades on one busted city-day (Paris
   2026-08-19: same +6.4°F bust, 3 buckets, 2 won 1 lost). Correlated exposure.
5. **Per-city bias/sd are in-sample** in §4's table (the §5 z-score table is walk-forward).
   Cities with n<8 fall back to the global bias.
6. **No modelling of the physics exit gate or take-profit** — the backtest holds to
   settlement. Live behaviour differs.
7. **Bankroll ledger quirk:** trades 1–4 were deleted but their `bankroll` rows remain,
   offset by an `ADJUSTMENT` of −$0.3342. Anything computing capital from that table alone
   must account for orphans.

---

## 9. Open questions for review

1. **Is the 0.94–0.95 entry band strictly better?** It carries 45% of volume at 99.10% vs
   96.15–96.77% at 0.90–0.92. Should `MIN_ENTRY_PRICE` rise to 0.93? What information is
   in the market price that the forecast gate lacks?
2. **Should the warm bias be corrected at the source** (subtract ~2°F from the forecast)
   rather than absorbed by the margin gate? A constant shift is mathematically identical
   to raising the gate; a **per-city** shift is not.
3. **Is the z-score gate worth shipping?** It cuts losses 80% but total P&L 51%. It only
   dominates when capacity-bound. At $100/$7/10-slots, 140 trades are already cap-blocked.
4. **Take-profit at 0.98 is still armed** while the backtest assumes hold-to-settlement.
   Measured leak on 4 live trades was 5.3% of profit. Should it be disabled for consistency?
5. **Helsinki (75.0%, n=8, −$4.82) and Moscow (93.9%, n=66, +$0.07)** — exclude?
   Helsinki's sample is tiny; Moscow is 66 trades to break exactly even.
6. **Is 97.96% robust, or regime-dependent?** Breakeven is 93.4%. July hit 95.7%.
   What happens in a winter with real volume (none observed)?
7. **Does the +20.6°F outlier bust indicate a data problem** rather than weather?
   (Toronto 2026-03-10, fc 46.4 → actual 67.0, open-ended bucket `[64.0, None]`.)

---

## 10. Reproduction

```bash
# 1. Bulk-fetch weather (batched; ~16s for a full year)
python3 scripts/prefetch_wx_batch.py 2025-09-15 2026-09-14

# 2. Run the causal backtest
python3 scripts/backtest_causal_deployed.py \
    --start 2025-09-15 --end 2026-09-14 --workers 16 \
    --out scripts/_causal_cache/ledger_365d.csv
```

Ledger columns: `target_date, city, is_high, entry_price, entry_utc, hours_left,
forecast_margin_f, won, net_pnl, bucket_low, bucket_high, question, condition_id, fc, actual`

`fc` = day-before forecast extreme (causal). `actual` = realized extreme (scoring only).

**Note:** Polymarket's Gamma/Data APIs return HTTP 403 to `urllib` but 200 to `requests`
with a browser User-Agent. Not a geoblock — the repo's own `utils.safe_get` uses `requests`.
