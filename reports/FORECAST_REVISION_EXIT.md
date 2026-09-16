# StormEdge forecast-revision exit investigation

## Conclusion

The requested mechanism cannot be validly backtested from the repository’s historical data. The available ledger has 1,956 causal entries (1,916 winners, 40 losers), and the repository has 137 post-entry station paths, including all 40 losses, but it has neither timestamped forecast revisions as-of each decision time nor historical NO bid/depth snapshots. Therefore no defensible `P_below/P_inside/P_above`, causal sell decision, executable fill, or net-profit result can be produced for this sample.

The mechanism is not shown to have improved net profit or reduced the approximately $280 loss bill. Any claim that it did would be an optimistic, non-causal result. The existing 50% price-stop reference is already known to be unsuitable: it fired on 220 trades, including 205 eventual winners and 15 losers, and reduced the reported hold-to-settlement result from +$315.51 to −$19.02 in the repository’s reference analysis.

## Coverage and limitations

| Input | Available | Usable for the requested replay |
|---|---:|---|
| Entry/settlement ledger | 1,956 | Yes, as the fixed-position baseline |
| Entry forecast | 1,956 scalar day-before values | Entry context only; not revisions/ensembles |
| Timestamped intraday station observations | 137 trades (all 40 losses) | Observation audit; not a forecast distribution |
| Timestamped forecast revisions | 0 | No |
| Historical NO bids/order-book depth | 0 | No |
| Shares/cost as explicit ledger fields | No (stake is inferable only approximately) | No exact execution replay |

The station paths are useful to identify when an observed running maximum entered a bucket or exceeded its upper edge. They do not reveal the forecast information available before that observation and do not supply a sell-side book. Sparse station sampling also means an unobserved crossing between reports cannot be treated as an exact signal time.

## What was tested

`scripts/backtest_forecast_revision_exit.py` performs an audit-only chronological join. For every ledger row it records the entry, bucket, outcome, entry forecast, post-entry observation coverage, first observed inside/path timestamp, first observed upper-edge exceedance, and availability of revision/book fields. It emits [forecast-revision-exit-audit.csv](/Users/macbook/Documents/GitHub/polymarket-weather-bot/reports/forecast-revision-exit-audit.csv).

Observed coverage: 137/1,956 trades had a matching post-entry station path; 35 had an observed running maximum inside a bounded bucket and 31 had an observed upper-edge exceedance. All 40 losers had paths. These are descriptive observations, not valid exit signals.

The existing causal ledger remains the baseline: 40 losses at roughly −$7 each, about −$280 gross loss exposure, against small winner gains. The repository’s prior analysis also shows that a forecast bust is not a sufficient signal: 221 winners had busts of at least 4.5°F, including 182 below-bucket winners and 37 overshoot winners (two unclassified).

## Policies

No policy was selected or claimed successful. Thresholds, calibration buffers, confirmation rules, fees, slippage, latency, partial fills, and bid-depth execution all require historical inputs that are absent. A perfect-information observation rule would leak the eventual path and would not satisfy the requested causal test, so it is intentionally not reported as a backtest.

## Required prospective shadow-replay data

For every position and every decision update, record: condition ID; exact market question and bucket/rounding rules; settlement source and station; local target date; entry and update timestamps; shares and cost; raw forecast payload plus provider issue time, valid time, retrieval time, model/version, ensemble members or quantiles, and publication latency; compatible observations with source timestamps and retrieval timestamps; valid running maximum; complete NO order book (price, size, depth, snapshot timestamp) and trade tape; fees, expected slippage, submitted quantity, partial fills, and processing latency. Persist the exact `P_below`, `P_inside`, `P_above`, calibration version, training cutoff, lower bound, buffers, decision state, and reason for every hold/watch/candidate/execute outcome.

After enough new data accumulates, fit calibration chronologically by city/season with clustered uncertainty, select thresholds only on preceding periods, reserve an untouched evaluation period, and replay the same entered positions against settlement. Report losers caught/missed, below-bucket versus overshoot winner mistakes, executable-price tiers, recovered dollars, sacrificed winner profit, all costs, net change, precision/recall intervals, and capacity-constrained results separately.
