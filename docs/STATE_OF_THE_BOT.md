# State of the Bot — as of 2026-07-05

A single-page snapshot of what this bot is, what was wrong with it, what was fixed,
and what's genuinely known vs. still unproven. Written so the July 2026 forensic
investigation isn't lost in chat history. For strategy mechanics see `STRATEGY.md`;
for the original forensic detail see `reports/AUDIT_REPORT.md`.

---

## What the bot does

Finds Polymarket weather markets ("Will the high in Tokyo be 31°C on July 1?") where
the crowd-implied probability disagrees with a multi-model weather ensemble, and bets
the mispricing. In practice it **only ever bets NO** — see "Why NO-only" below. Runs in
**paper mode** on Fly.io (app `stormedge`), $20 bankroll, scans every 10 min, monitors
every 5 min.

---

## The investigation (2026-07-04 → 07-05): what was broken

The bot's dashboard claimed **+$34 profit**. Verified against Polymarket's real
resolution, the truth was **−$20** — 14 of 19 "winning" trades had actually lost. Root
causes, all now fixed and deployed:

| # | Bug | Effect | Fix |
|---|---|---|---|
| 1 | **Inverted order-book read** | `asks[0]`/`bids[0]` are the *worst* prices on Polymarket's CLOB (asks sorted desc, bids asc). Produced a fake ~98% spread that (a) fabricated phantom exit fills and (b) silently froze all new entries after a spread gate was added. | `_best_ask_bid_from_book()` uses `min(asks)`/`max(bids)`. |
| 2 | **Phantom $0.999 exits** | Losing NO positions were booked as ~$1.00 "wins" — edge-decay exits fired on a resolving book's lone extreme quote (max NO ever seen in 44,879 signals was 0.81). | Hold for $1/$0 resolution once `target_date` passes, don't market-exit a resolving book. |
| 3 | **Celsius zero-width bucket** | "33°C" parsed as a zero-width band; ±0.5°F padding under-covered the true 1°C (~1.8°F) window → fake NO edge. | Celsius exact buckets parse to a rounding-tolerant band (parser v2). |
| 4 | **Wrong resolution source** | Bot forecast/verified against Open-Meteo ERA5; Polymarket resolves off **Wunderground airport METAR, whole °C**, which runs **~+0.7°C warmer**. On 1°C buckets this flipped outcomes constantly — the biggest single cause of losses. | New `metar.py` reads the free Iowa Mesonet METAR archive for the exact ICAO station Polymarket names; all 46 city coordinates corrected to the resolution airports; +1.3°F warm-bias correction on the ensemble mean. |
| 5 | **Overconfident probabilities** | Raw Gaussian bucket probs were ~2–3.5× too confident vs. real hit rate (predicted 5% → observed 19% against METAR). Manufactured most of the "edge". | Platt calibration (`_calibrate_prob`) re-fit against the METAR reliability curve. |
| 6 | **Date/timezone mis-alignment** | Target date read from a drifting endDate timestamp; mis-dated far-offset stations (flipped a real Wellington trade). | `parse_target_date()` reads the date from each market's own "on `<DATE>`" resolution text. |
| 7 | **Edge decay scalped winners** | Fired whenever a NO bet was *winning* (price converging toward $1 shrinks remaining edge), banking pennies (or a small loss) instead of holding to full settlement. | `_thesis_broken()` gate: edge decay only exits on a genuine thesis break (forecast turned against us, or real loss), not on a converging winner. `HOLD_WINNERS_TO_RESOLUTION` default on. |

Test coverage grew 48 → 80; each bug has a regression test.

---

## Why NO-only (not a config choice — structural)

After honest calibration, across 1,058 distinct historical markets there were **160
NO-edge signals and 0 YES-edge signals**. A single narrow (1°C) bucket almost always
has true probability *below* its YES price — the temperature usually lands in some
*other* bucket — so the only rational side is NO ("this specific bucket will be missed").
Allowing YES trades changes nothing because none qualify. Attempted and rejected
2026-07-05.

---

## What is actually known vs. unproven

**Known (high confidence):**
- The bot's *forecast* is good — ensemble MAE ~2°F, well-centred (std(z)≈0.64).
- The bugs above were real and are fixed & deployed (verified on the live host).
- The current live ledger is honest: $20.00 → ~$20.48 (real, not fabricated).

**Unproven (the open questions — do NOT treat as settled):**
- **Is the edge real?** Full-history backtest vs. METAR: NO wins ~59% (fresher sample)
  to ~70% (full, in-sample). Breakeven at these prices is ~53%. So *probably* a thin
  positive edge — but it's in-sample simulation with ~16% outcome-inference error, and
  the only real resolved trades so far (n≈2–3) are statistically meaningless.
- **Does it survive real execution?** Everything to date is **paper mode**, which
  assumes fills at the quoted bid. On thin weather books that's optimistic. Confidence
  the paper profit would survive as real money: ~55%.

---

## Structural limits (won't change without new capital / sources)

- **$20 bankroll** → $1–2 positions where fees + spread dominate. The bot structurally
  can't earn meaningfully at this size; the current goal is to *prove calibration*, not
  to make money.
- **ERA5 vs. METAR residual** — even matched to the right station, Open-Meteo (a
  forecast) can't perfectly equal the observed METAR that resolves the market. The
  margin gate + calibration manage this but can't eliminate it.

---

## What's next (in priority order)

1. **Wait ~1 week, then run `python calibrate.py --source metar`.** Free — the bot is
   already gathering data. This is the only thing that answers "is the edge real"
   against the true ruler with no in-sample cheating. **Do this before anything else.**
2. **Then, only if calibration confirms an edge:** a tiny **real-money** run ($30–50)
   to measure true slippage — the one thing paper mode can never show. Requires funding
   a Polygon wallet + setting CLOB secrets as Fly secrets (a manual gate; see
   `live-trading-gate` memory). Do NOT do this on an unproven edge.
3. **Resist further tuning until (1) has data.** The model is honestly calibrated now;
   more tweaking before resolved data arrives is just overfitting to noise.

---

## Key config knobs (defaults)

- `PAPER_MODE=true`; `STARTING_BANKROLL` code default is 40, but `fly.toml` overrides it to **20** (the live run seeds at $20)
- `EDGE_THRESHOLD=0.08`, `NARROW_BUCKET_EDGE_THRESHOLD=0.20`
- `ENABLE_PROB_CALIBRATION=true` (Platt; INTERCEPT −0.1715, SLOPE 0.4457, METAR-fit)
- `METAR_WARM_CORRECTION_F=1.3`, `FORECAST_MARGIN_F=2.5`
- `HOLD_WINNERS_TO_RESOLUTION=true`, `THESIS_BREAK_PROB_DELTA=0.10`
- `ENABLE_STOP_LOSS=false`

Verify against `config.py` before relying on any value — it is the source of truth.
