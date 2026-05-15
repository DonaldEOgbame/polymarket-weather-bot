# Strategy

stormedge finds weather markets on Polymarket where the crowd-implied probability is meaningfully wrong relative to what a multi-model meteorological ensemble says. When the gap is wide enough, it bets against the mispricing.

---

## The core idea

Polymarket weather markets ask binary questions like:

> "Will the high temperature in Amsterdam be 13°C on May 16?"

Each market has a YES price and a NO price (they sum to ~$1). Those prices are the crowd's implied probability. If YES trades at $0.20, the market believes there's a 20% chance the high hits 13°C.

The bot independently estimates that probability using real weather forecast data. If the models say there's only a 2% chance — but the market is pricing it at 20% — that's an 18-cent edge on the NO side. The bot bets NO.

---

## Step 1 — Market discovery

The bot fetches active weather events from Polymarket's Gamma API using the weather tag (`tag_id=84`). Each weather event contains 10–15 individual bucket markets (e.g., "will the high be 10°C?", "will it be 11°C?", "will it be 12°C?", etc.).

**Filters applied before any forecast fetch:**
- Must resolve within 72 hours
- Must have ≥ $500 in liquidity
- Must map to a known city station
- Must have a parseable temperature bucket

Markets are scored by a combination of liquidity (60%) and price uncertainty (40% — markets near 50/50 have the most edge potential) and the top 150 are selected for evaluation.

---

## Step 2 — Weather forecast ensemble

For each city, the bot calls [Open-Meteo](https://open-meteo.com) to pull temperature forecasts from multiple NWP (Numerical Weather Prediction) models simultaneously. One API call returns all available forecast dates, so a city with 5 active markets only costs 1 HTTP request.

**Model weights by region:**

| Region | Models used | Rationale |
|---|---|---|
| US | ECMWF IFS 0.4° (35%), best_match (35%), GFS 0.25° (20%), ICON Global (10%) | ECMWF and GFS are the dominant US operational models |
| EU | ECMWF IFS 0.4° (40%), ICON Global (30%), GFS 0.25° (20%), JMA GSM (10%) | ECMWF is highest-skill for Europe; ICON is DWD's European model |
| AP | JMA GSM (35%), ECMWF IFS 0.25° (35%), ICON Global (20%), GEM Global (10%) | ECMWF 0.4° returns nulls for many AP coordinates; IFS 0.25° is used instead |

**Bias corrections applied:**
- GFS has a known warm bias in humid US cities: −1.5°F for Miami and Houston, −1.2°F for Dallas

---

## Step 3 — Probability estimation

Given N model forecasts for a city/date, the bot builds a probability distribution for the actual temperature and integrates it over the market's bucket.

**Ensemble mean:**
```
μ = Σ(weight_i × temp_i) / Σ(weight_i)
```

**Combined standard deviation:**
The uncertainty has two sources:
1. **Base forecast error** — intrinsic NWP skill decay with lead time, interpolated from a calibration table:
   - 12h out: ±1.0°F, 24h: ±1.5°F, 48h: ±2.0°F, 72h: ±2.5°F
2. **Model spread** — std dev across the individual model forecasts

```
σ = sqrt(base_error² + model_spread_std²)
```

This σ is intentionally conservative — it gets wider as lead time increases and as models disagree more.

**Bucket probability:**
The temperature is modelled as normally distributed with mean μ and std σ. The probability of landing inside the bucket [L, H] is:

```
P = Φ((H + 0.5 - μ) / σ) - Φ((L - 0.5 - μ) / σ)
```

The ±0.5°F padding accounts for the discrete nature of temperature bucket boundaries.

---

## Step 4 — Edge calculation

```
YES edge = P(model) - P(market YES price)
NO edge  = (1 - P(model)) - P(market NO price)
```

A positive edge means the bot thinks the outcome is more likely (or less likely) than what the market is pricing.

---

## Step 5 — Quality gates

Before placing a trade, two additional checks protect against entering on noisy or uncertain forecasts:

| Gate | Default | Purpose |
|---|---|---|
| Model agreement | ≥ 60% | Fraction of models within 2°F of the ensemble mean. Low agreement means the models are genuinely split — the signal is noise, not edge. |
| Model spread | < 2.7°F | Max − min across all model forecasts. A spread wider than 1.5°C means the models are telling materially different stories. |

Markets that pass edge threshold but fail these gates are logged as `SHADOW` signals — they're tracked for research but not traded.

---

## Step 6 — Position sizing

Trade size is determined by fractional Kelly criterion:

```
Full Kelly = edge / (1 - entry_price)
Fraction used = min(Full Kelly, KELLY_CAP)
Size = total_equity × fraction
```

Additional hard limits:
- Maximum $2.00 per position
- Maximum 10% of equity in any single trade
- Maximum 30% of total equity locked across all open positions
- Minimum $0.50 (below this, fees eat the edge)

---

## Step 7 — Exit logic

Open positions are checked every 5 minutes. An exit fires if:

1. **Stop-loss** — the mid-price (ask + bid / 2) has dropped more than 15% below entry. Not checked until the position has been open at least 30 minutes (avoids triggering on entry spread noise).
2. **Edge decay** — the latest model re-evaluation shows the edge has fallen below 5%. The market may have moved to reflect the forecast, eliminating the original thesis.
3. **Market expired** — if the orderbook disappears on restart, the position is closed at $0 P&L and marked EXPIRED.

---

## Known limitations

- **Small bankroll** — at $20, most position sizes ($1–$2) are close to Polymarket's minimum fill size. Fees and spread eat a meaningful fraction of edge on each trade.
- **No live resolution tracking** — the bot doesn't subscribe to market settlement. A winning position that resolves YES=1.0 is only closed when the monitor cycle detects edge decay or the market disappears.
- **Model latency** — Open-Meteo updates its ensemble runs every 6–12 hours. Between runs, the forecast is stale. Markets can move faster than the models update near resolution.
- **Temperature markets only** — precipitation, wind, and hurricane markets exist on Polymarket but are not currently traded (bucket parsing is not implemented for non-temperature questions).
