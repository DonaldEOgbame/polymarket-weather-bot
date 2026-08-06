# Fill audit — live-era trades against their quotes

Extracted from production 2026-08-06, before the machine was stopped. Computed
by `audit_fills.py`, which should be re-run on the box once Phase 0.3 deploys
(the local copy cannot be used: `flyctl ssh sftp get` truncated the 588 MB DB
into a malformed file, as the runbook warns).

**8 live trades. 1 had negative edge at the fill. 1 would be refused by the new
depth gate.** They are the same trade.

| # | city | quote | fill | slippage | fair | edge@fill | EV | ask depth | depth gate |
|---|---|---|---|---|---|---|---|---|---|
| 9 | Sao Paulo | 0.7350 | 0.7600 | +0.0250 | 0.9500 | +0.1900 | +$1.50 | $3,943.29 | pass |
| 8 | Austin ⚠️ | 0.6400 | **0.9818** | **+0.3418** | 0.8645 | **−0.1173** | **−$0.72** | **$26.49** | **REFUSE** |
| 7 | Munich *(adopted)* | — | 0.8400 | — | 0.6977 | −0.1423 | −$0.34 | — | — |
| 6 | Beijing | — | 0.6400 | — | 0.7305 | +0.0905 | +$0.85 | — | — |
| 5 | Austin | — | 0.5500 | — | 0.7239 | +0.1739 | +$1.90 | — | — |
| 4 | Tel Aviv *(adopted)* | — | 0.4200 | — | 0.7476 | +0.3276 | +$6.24 | — | — |
| 3 | Dallas | — | 0.4700 | — | 0.7170 | +0.2470 | +$3.15 | — | — |
| 2 | Tel Aviv *(adopted)* | — | 0.4200 | — | 0.7476 | +0.3276 | +$1.56 | — | — |

## The one losing bet

**Austin #8** — paid 0.9818 for a token worth 0.8645. The decision claimed
+0.1279; execution made it **−0.1173**, an expected **−$0.72** on 6.11 shares.
$6.00 went into $26.49 of ask depth, taking ~23% of everything resting.

Realised P&L will not redeem it. The position is 86.5% to win and pays 1.9%:

```
EV = 0.865 × (+0.0182) − 0.135 × (−0.9818) = −0.117/share
```

Winning it is the outcome that hides the defect, which is why post-fill edge and
not P&L is the column that matters here.

## Coverage limits — stated rather than papered over

**Quotes exist for 2 of 8 trades.** `replay_signals` only carries `BUY` decision
rows from 2026-08-05 onward, so trades 2–6 have a recorded fill and no recorded
quote. Their slippage is unknowable after the fact; the live book they crossed
is gone. Their post-fill edge is still computable (it needs only fill and
model_prob) and all four are positive.

**Depth exists for 2 of 8.** So "1 trade would be refused" is a lower bound over
the whole book, not a complete count — five trades cannot be scored against the
gate at all.

**Three trades are adopted, not decisions.** Tel Aviv #2/#4 and Munich #7 came
from the retired ghost-bot app trading the same wallet. Munich's recorded edge is
−0.1423 *at decision*, which is not an execution failure — it was never a
decision this bot made. They are excluded from execution-quality conclusions.

Of the five trades this bot actually decided and can be scored: **four filled
well, one inverted.**

## Execution quality

- **1 of 2** trades with a recorded quote filled at it. Sao Paulo slipped
  +0.0250 — just under the 0.03 alert threshold — against $3,943 of depth. That
  is ordinary spread-crossing and the post-fill edge survived it comfortably.
- Austin slipped **+0.3418**, 13.7× Sao Paulo's, against 0.7% of its depth.

The clean fills were **luck, not design**. Nothing gated on depth, so nothing
prevented any of them from walking the book the way Austin did. The difference
between trade #9 and trade #8 was the book they happened to meet.
