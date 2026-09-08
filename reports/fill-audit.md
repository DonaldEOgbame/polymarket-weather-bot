# Fill audit — every live-era trade against its quote

18 trades. **6** had NEGATIVE edge at the fill. **7** filled more than 0.03 from the quote. **1** would be refused by today's depth gate.

Post-fill edge is the number that matters, not P&L: a trade can realise a profit and still have been a losing bet at the moment it was placed. Any trade whose slippage exceeded its modelled edge was one.

| # | city | quoted | fill | slippage | edge@decision | fair | edge@fill | EV | ask depth | size % | depth gate |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | Dallas ⚠️ | — | 0.8900 | — | -0.1324 | 0.7530 | -0.1370 | $-0.46 | — | — | — |
| 2 | Dallas | 0.7350 | 0.7900 | 0.0550 | 0.0613 | 0.8490 | 0.0590 | $+0.15 | $4304.16 | 0.0% | pass |
| 3 | Seattle ⚠️ | — | 0.9500 | — | -0.1506 | 0.7874 | -0.1626 | $-0.34 | — | — | — |
| 4 | Seattle ⚠️ | 0.7650 | 0.7800 | 0.0150 | 0.0004 | 0.7794 | -0.0006 | $-0.00 | $3762.51 | 0.1% | pass |
| 5 | Beijing ⚠️ | — | 0.8500 | — | -0.0007 | 0.8462 | -0.0038 | $-0.01 | — | — | — |
| 6 | Dallas | 0.6900 | 0.7400 | 0.0500 | 0.0376 | 0.7783 | 0.0383 | $+0.10 | $5040.64 | 0.0% | pass |
| 7 | Dallas | 0.6700 | 0.7400 | 0.0700 | 0.0076 | 0.7486 | 0.0086 | $+0.02 | $1160.05 | 0.2% | pass |
| 8 | Dallas ⚠️ | — | 0.7700 | — | -0.0208 | 0.7629 | -0.0071 | $-0.02 | — | — | — |
| 9 | Beijing | 0.7350 | 0.7500 | 0.0150 | 0.0186 | 0.7683 | 0.0183 | $+0.05 | $4608.28 | 0.0% | pass |
| 10 | Seattle | 0.6850 | 0.7300 | 0.0450 | 0.0203 | 0.7510 | 0.0210 | $+0.06 | $2812.06 | 0.1% | pass |
| 11 | Dallas | 0.6250 | 0.7400 | 0.1150 | 0.0587 | 0.8104 | 0.0704 | $+0.19 | $3045.27 | 0.1% | REFUSE |
| 12 | London | 0.7100 | 0.7200 | 0.0100 | 0.0842 | 0.8045 | 0.0845 | $+0.23 | $1609.99 | 0.1% | pass |
| 13 | Dallas | 0.7250 | 0.7300 | 0.0050 | 0.0308 | 0.7658 | 0.0358 | $+0.10 | $4354.21 | 0.0% | pass |
| 14 | Atlanta | 0.6850 | 0.7200 | 0.0350 | 0.1382 | 0.9500 | 0.2300 | $+0.64 | $2418.99 | 0.1% | pass |
| 15 | London ⚠️ | — | 0.7300 | — | -0.2338 | 0.5110 | -0.2190 | $-0.60 | — | — | — |
| 16 | Dallas | 0.6550 | 0.7000 | 0.0450 | 0.0694 | 0.7707 | 0.0707 | $+0.20 | $850.56 | 0.2% | pass |
| 17 | Chicago | 0.6850 | 0.7100 | 0.0250 | 0.0559 | 0.7667 | 0.0567 | $+0.16 | $5558.41 | 0.0% | pass |
| 18 | Wellington | 0.6750 | 0.7000 | 0.0250 | 0.1268 | 0.8278 | 0.1278 | $+0.37 | $498.80 | 0.4% | pass |

## Losing bets at the moment of execution

- **Dallas (#1)** — paid 0.8900 for a token worth 0.7530. The decision claimed -0.1324; execution made it -0.1370, an expected $-0.46 on 3.37 shares. Status: CLOSED, realised P&L $+0.30.
- **Seattle (#3)** — paid 0.9500 for a token worth 0.7874. The decision claimed -0.1506; execution made it -0.1626, an expected $-0.34 on 2.11 shares. Status: CLOSED, realised P&L $+0.08.
- **Seattle (#4)** — paid 0.7800 for a token worth 0.7794. The decision claimed +0.0004; execution made it -0.0006, an expected $-0.00 on 2.56 shares. Status: CLOSED, realised P&L $+0.56.
- **Beijing (#5)** — paid 0.8500 for a token worth 0.8462. The decision claimed -0.0007; execution made it -0.0038, an expected $-0.01 on 2.35 shares. Status: CLOSED, realised P&L $+0.33.
- **Dallas (#8)** — paid 0.7700 for a token worth 0.7629. The decision claimed -0.0208; execution made it -0.0071, an expected $-0.02 on 2.60 shares. Status: CLOSED, realised P&L $+0.54.
- **London (#15)** — paid 0.7300 for a token worth 0.5110. The decision claimed -0.2338; execution made it -0.2190, an expected $-0.60 on 2.74 shares. Status: CLOSED, realised P&L $-2.00.

Realised P&L does not redeem these. A bet that is 86.5% to win and pays 1.9% is still negative expectation, and winning it is the outcome that hides the defect.

## Execution quality

- 0 of 18 trades filled AT the quote.
- 7 exceeded the 0.03 alert threshold.

Clean fills to date were luck rather than design: depth was never gated on, so nothing prevented any of them from walking the book the way the Austin fill did.
