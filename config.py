"""
Centralized configuration for the Polymarket Weather Bot.
All tunable thresholds are loaded from environment variables with safe defaults.

A small set of money/risk knobs (see MANAGED_SETTINGS) can additionally be
overridden at runtime from the `settings` table, written by the dashboard's
Settings tab. Those overrides are read ONCE here at import time: every module
does `from config import X`, binding a copy of the value into its own namespace,
so a settings change only reaches the bot after a process restart. That is why
the Settings tab saves and then restarts rather than pretending to hot-reload.
"""
import os
import sqlite3
from dotenv import load_dotenv

load_dotenv()

# --- Mode ---
# Resolved below, after _tunable() exists: the dashboard can switch this at
# runtime and persists the choice, so the stored value has to beat the env var
# the same way every other managed setting does. See PAPER_MODE further down.

# --- Database ---
DB_PATH = os.getenv("DB_PATH", "data/bot.db")

# --- Runtime-editable overrides (dashboard Settings tab) ---
# Only these keys may be overridden from the database. Everything else stays
# code/env controlled — calibration constants and station tables in particular
# are documented, version-tracked, and must not be editable from a web form.
MANAGED_SETTINGS = (
    # Persisted by /api/trading-mode only — deliberately absent from
    # app.SETTING_SPECS, which is what the bulk Settings save validates against,
    # so this can never ride along in an ordinary save.
    "PAPER_MODE",
    "FIXED_POSITION_SIZE",
    "MAX_CONCURRENT_POSITIONS",
    "DAILY_LOSS_STAKES",
    "MAX_TOTAL_EXPOSURE_FRACTION",
    "ENABLE_STOP_LOSS",
    "STOP_LOSS_PCT",
    "TAKE_PROFIT_PRICE",
)


def _load_overrides():
    """Read managed overrides from the settings table using raw sqlite3.

    Raw sqlite3 rather than db.py because db.py imports THIS module — the
    dependency runs one way only and importing db here would be circular.

    Every failure mode falls back to {}: the file may not exist yet, the table
    is created by init_db() which runs AFTER this import (main.py), the DB may
    be locked by the bot thread, or a row may be malformed. None of those may
    stop the process from booting, so this never raises.
    """
    try:
        if not os.path.exists(DB_PATH):
            return {}
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=2.0)
        try:
            rows = conn.execute(
                "SELECT key, value FROM settings"
            ).fetchall()
        finally:
            conn.close()
        return {k: v for k, v in rows if k in MANAGED_SETTINGS and v is not None}
    except Exception:
        return {}


_DB_OVERRIDES = _load_overrides()


def _tunable(key, default):
    """Resolve a managed setting: DB override > env var > hardcoded default.

    The DB wins over the environment because the Settings tab is the live
    control surface — a stale fly.toml value silently overriding what the user
    just saved in the UI is exactly the confusion this feature removes.
    Returns a string either way, so the caller's float()/int()/== "true"
    coercion is identical for stored and environment values.
    """
    if key in _DB_OVERRIDES:
        return _DB_OVERRIDES[key]
    return os.getenv(key, default)


# --- Mode ---
# Boot default for the runtime store below. A dashboard switch persists here, so
# the mode survives a restart or a deploy: without that, every redeploy would
# silently drop a live bot back to paper. Defaults to paper whenever it has
# never been set — the safe direction for the one flag that spends real money.
PAPER_MODE = str(_tunable("PAPER_MODE", "true")).strip().lower() == "true"

# --- Bankroll ---
STARTING_BANKROLL = float(os.getenv("STARTING_BANKROLL", "40.0"))

# --- Strategy Thresholds ---
EDGE_THRESHOLD = float(os.getenv("EDGE_THRESHOLD", "0.08"))
# Raised 0.6->0.75 by user preference 2026-07-10 as a deliberately stricter stance
# pending more resolved trades; it was never validated by outcome data as better
# than 0.6, and the evidence below suggests it is not measuring what it claims to.
#
# NOW WEIGHT-BASED (2026-07-31), not a raw member count. As an unweighted count
# of members within 2°F of the mean, this silently tightened as the ensemble grew:
# at n=4 it meant "3 of 4", at n=10 it would mean "8 of 10" — a much stricter
# condition that has nothing to do with forecast quality. It is now the fraction
# of ENSEMBLE WEIGHT sitting within 2°F, which means the same thing at any n.
#
# Caveat worth keeping in view: agreement does not actually predict error in this
# bot's record (Spearman -0.29 against |error|, t=-1.52, n=27 — if anything the
# wrong sign). The six largest misses all had agreement between 0.75 and 1.00,
# and the single worst (Seoul 2026-07-25, off by 5.7°F) had agreement 1.00.
# Retained as a conservatism, not as a validated predictor; the spread term in
# sigma is where model disagreement is actually priced.
MIN_MODEL_AGREEMENT = float(os.getenv("MIN_MODEL_AGREEMENT", "0.75"))
# Maximum ensemble disagreement before a trade is skipped, as a WEIGHTED STANDARD
# DEVIATION in °F.
#
# RENAMED from MAX_MODEL_SPREAD (which was 2.7 = 1.5°C) because the UNITS changed
# and a stale env var carrying the old value would have quietly disabled the gate:
# 2.7 as a standard deviation is roughly 6.9°F of range, which nothing in the
# record approaches. The old statistic was max-min, which can only increase as
# members are added, so it was really a cap on ensemble SIZE — expanding from 4
# models to 10 would have driven trade flow to zero and looked like a data bug.
# 1.05 is the equivalent cut on the historical distribution (median range/stdev
# ratio 2.55 across the 59 traded signals).
MAX_MODEL_SPREAD_STD = float(os.getenv("MAX_MODEL_SPREAD_STD", "1.05"))

# --- Transaction costs (subtracted from raw edge before the threshold check) ---
# Polymarket taker fee per share = TAKER_FEE_RATE * p * (1 - p), a bell curve that
# peaks at p=0.50 and ~vanishes near 0.01/0.99. Makers pay $0.
# MEASURED LIVE 2026-07-18, two independent observations:
# get_fee_rate_bps returns 1000 on weather tokens, but the EFFECTIVE charge is
# base/2 per leg — 0.05 * p * (1-p) * shares, charged on BOTH buy and sell.
# Confirmed to the cent by the first live entry (Wuhan NO 4.878sh @ 0.41:
# charged $0.0590 = 0.05*0.41*0.59*4.878) and by re-reconciling the $1 BUY+SELL
# round-trip (both legs at 0.05-formula + spread + dust closes the books
# exactly; the earlier "1000bps on sell only" reading was wrong). Settlement
# redemption pays no fee, so hold-to-settlement pays entry-leg only.
TAKER_FEE_RATE = float(os.getenv("TAKER_FEE_RATE", "0.05"))
# Separate allowance for crossing the bid/ask spread on thin books, as a fraction
# of the entry price. Used as a fallback when the live order book can't be fetched.
SLIPPAGE_FRACTION = float(os.getenv("SLIPPAGE_FRACTION", "0.015"))

# Maximum tolerable bid/ask spread (as a fraction of mid price) to enter a trade.
# Measured live from the order book at evaluation time. Wide spreads mean the real
# cost of crossing is likely to eat most or all of the modeled edge.
MAX_ENTRY_SPREAD_FRACTION = float(os.getenv("MAX_ENTRY_SPREAD_FRACTION", "0.15"))

# Minimum number of weather models required to enter a trade.
# Two models that agree proves nothing — ECMWF dropping out silently leaves only 2.
MIN_MODEL_COUNT = int(os.getenv("MIN_MODEL_COUNT", "3"))

# --- Model families (see families.py) ---
# Most "independent" members are not. icon_global/icon_eu/icon_d2 are one model
# at three resolutions; ecmwf_ifs025 and ecmwf_aifs025_single share initial
# conditions. Counting them separately manufactures agreement, and the two
# gates that decide almost every trade read exactly the statistics that fake
# agreement inflates.
ENABLE_FAMILY_WEIGHTING = os.getenv("ENABLE_FAMILY_WEIGHTING", "true").lower() == "true"
# No forecasting centre may carry more than this share of the blend, however
# many of its resolutions are present.
FAMILY_WEIGHT_CAP = float(os.getenv("FAMILY_WEIGHT_CAP", "0.35"))
# Compute agreement and spread ACROSS FAMILIES rather than across members. Off
# would keep the member-level statistics the current thresholds were fitted
# against; on is correct once a family can contribute several members, which is
# what Phase 2 introduces.
GATE_ACROSS_FAMILIES = os.getenv("GATE_ACROSS_FAMILIES", "true").lower() == "true"

# --- Open-Meteo → METAR resolution-source correction (°F) ---
# SET TO 0 on 2026-07-31. This was +1.3, fitted on the first 19 traded station-days
# when the ensemble genuinely ran cold against the paying ruler. It no longer does,
# and the shift is now the largest single source of bias in the pipeline.
#
# Measured on the 27 settled trades with a trustworthy settlement temperature:
#
#                    MAE     mean bias
#   raw ensemble    2.59°F     -0.23°F     <- essentially unbiased
#   +1.3 shifted    2.82°F     +1.07°F     <- the shift IS the bias
#
# Split by direction the raw mean is -0.51 on highs and +0.45 on lows, i.e. the
# residual cold bias only ever existed on highs, and it is the diurnal-compression
# artifact now handled per-model and per-direction by MODEL_BIAS_CORRECTIONS. A
# flat warm shift applied to both directions double-corrects highs and actively
# inverts lows: on minimum-temp markets it degraded MAE from 1.61°F to 2.30°F.
#
# It was also destroying the one predictor that works. Model spread ranks forecast
# error at Spearman +0.46 (p=0.016) against the RAW mean; against the shifted mean
# the same correlation is -0.05. The blanket shift adds a constant error term that
# swamps the spread signal, which is why the deployed sigma formula never saw it.
#
# If a station correction is warranted at all it is per-station AND per-direction —
# Hong Kong and Seoul want opposite signs — and must be re-fit only after the
# settlement ruler is confirmed for every city. Do not restore a global value.
METAR_WARM_CORRECTION_F = float(os.getenv("METAR_WARM_CORRECTION_F", "0.0"))

# --- Forecast margin gate ("stop cutting it close") ---
# Only enter when the ensemble mean is at least this many °F clear of the NEAREST
# bucket boundary we're betting the temperature will miss. Rationale: the market
# resolves off a Wunderground station reading that runs up to ~1°C (~1.8°F) different
# from our Open-Meteo forecast/verification, and a whole-degree-Celsius bucket is only
# ~1.8°F wide — so a forecast sitting <2°F from a boundary is a coin flip we lose as
# often as we win regardless of model quality. Measured on the first 19 trades, the
# losers all sat within ~2.1°F of a boundary. Requiring real daylight between the
# forecast and the boundary is what separates a defensible bet from a gamble.
# Set to 0 to disable. Applies only to bounded (exact/range) buckets.
FORECAST_MARGIN_F = float(os.getenv("FORECAST_MARGIN_F", "2.5"))

# YES-side margin cap, as a fraction of the padded bucket's half-width. Every real
# bucket here (0.8-2.8°F padded-wide) is narrower than 2*FORECAST_MARGIN_F, so an
# unguarded YES margin check ([lo+margin, hi-margin]) is mathematically empty —
# capping the margin at exactly half-width "fixes" that but collapses the passing
# window to the bucket's exact midpoint, a single float value real forecasts will
# essentially never land on. This fraction (<1.0) keeps a real, non-degenerate
# window instead. Currently moot — YES entries are hard-disabled — but keeps the
# gate meaningful rather than silently impossible if YES is ever re-enabled.
YES_MARGIN_WIDTH_FRACTION = float(os.getenv("YES_MARGIN_WIDTH_FRACTION", "0.6"))

# Narrow-bucket guard: buckets ≤ this width (°F) require higher edge to enter.
# Exact and 1°F-range buckets are structurally disadvantaged vs above/below markets.
NARROW_BUCKET_WIDTH_F = float(os.getenv("NARROW_BUCKET_WIDTH_F", "2.0"))

# LOWERED 0.20 -> 0.12 BY OWNER DECISION, 2026-07-31. This is a choice, not a fit:
# nothing in the data identifies 0.12 as optimal, and the section below is written
# so a later reader can tell this apart from a calibration constant.
#
# Rationale: more trade flow, so evidence accumulates faster. The bot has 27
# settled trades after four weeks; at 0.20 the recalibrated sigma admits 6 of
# them. That is not a sample anything can be decided from.
#
# Supporting evidence, and what it is worth. Replaying the 27 settled trades
# through the current pipeline (harness: reconcile.py, session scratchpad
# 2026-07-31):
#
#     threshold   survivors   settled P&L   win rate
#       0.20          6         +$6.70       100.0%
#       0.15         10         +$7.58        90.0%
#       0.12         12         +$9.56        91.7%
#       0.10         12         +$9.56        91.7%
#       0.08         12         +$9.56        91.7%
#
# On live trades only: 9 of 20 survive at 0.12 against 3 of 20 at 0.20, adding
# 5 wins and 1 loss (London, Taipei, Hong Kong x2, Shenzhen; Houston #68 -$2.00)
# for +$2.85. LIMIT: this sample was SELECTED BY THE OLD GATES — every row in it
# is a trade that was actually placed. It is in-sample for both thresholds and it
# cannot credit either one for markets that neither configuration ever saw. The
# +$2.86 figure quoted in the change request is +$2.85 on re-run; the difference
# is rounding of the per-trade settled values, not a different result.
#
# The curve is FLAT BELOW 0.12: 0.08, 0.10 and 0.12 select the identical 12
# trades. EDGE_THRESHOLD is 0.08, so choosing 0.12 is much closer to abolishing
# the separate narrow-bucket regime than to tuning it. Read it that way.
#
# THE COUNTER-ARGUMENT, ON RECORD: adverse selection. The gated-slice analysis
# behind PROB_CALIBRATION_INTERCEPT/SLOPE (below) found the market carrying real
# information precisely where the model disagrees enough to trade, and found
# LARGER disagreements more accurate than smaller ones. A lower threshold trades
# smaller disagreements — the less accurate end. That argues for 0.20, and unlike
# the table above it was measured on live data. This decision overrides it
# knowingly, on the grounds that 6 trades cannot settle the question either way.
#
# COUPLING WARNING — NARROW_BUCKET_STD_INFLATION (1.4x, below) and this threshold
# correct the SAME defect, and either lever alone reaches the same place:
#
#     inflation 1.4 + thr 0.12  ->  12 survivors, +$9.56   <- deployed
#     inflation 1.0 + thr 0.20  ->   9 survivors, +$9.58
#     inflation 1.0 + thr 0.12  ->  12 survivors, +$9.56   <- adds nothing
#
# Do NOT now also remove the inflation. It would be the third correction of one
# defect, it buys nothing on this sample, and PROB_CALIBRATION_INTERCEPT/SLOPE
# were fitted on probabilities that had already been through the inflation step
# (see the COUPLING note on those constants) — dropping it silently invalidates them.
NARROW_BUCKET_EDGE_THRESHOLD = float(os.getenv("NARROW_BUCKET_EDGE_THRESHOLD", "0.12"))

# Std inflation multiplier applied to narrow buckets (≤ NARROW_BUCKET_WIDTH_F).
# Makes the probability estimate more conservative on thin windows.
NARROW_BUCKET_STD_INFLATION = float(os.getenv("NARROW_BUCKET_STD_INFLATION", "1.4"))

# Cities with high convective variability where afternoon storms cause large
# unpredictable temperature swings. Std is inflated by this multiplier.
CONVECTIVE_STD_INFLATION = float(os.getenv("CONVECTIVE_STD_INFLATION", "1.3"))
# "Tampa" removed 2026-07-31: it is not in weather.STATIONS, so this entry and
# its -1.3°F GFS correction had never once applied. weather.validate_city_tables()
# now warns at startup if a phantom city is reintroduced here.
CONVECTIVE_CITIES = set(os.getenv("CONVECTIVE_CITIES", "Miami,Houston,Dallas,Atlanta").split(","))

# --- Probability calibration (Platt scaling on the raw Gaussian bucket prob) ---
# The raw normal-CDF bucket probability is systematically OVERCONFIDENT: measured on
# 96,307 resolved signals (2026-07-04), buckets the model called ~15% actually hit
# ~28%, and ~24% hit ~43% — a ~1.9x under-statement of hit rate in exactly the
# low-probability region where the bot places its NO bets. That manufactured fake NO
# edge and is the single biggest driver of the -$20 true loss on the first 19 trades.
# Fix: remap raw prob p through a logistic fitted to the reliability curve:
#   logit(p_cal) = INTERCEPT + SLOPE * logit(p_raw)
# The fitted curve reproduces the observed hit rates to within ~1% per bin. Re-fit from
# calibrate.py's reliability table as more data resolves; set ENABLE_PROB_CALIBRATION=
# false to fall back to the raw (overconfident) Gaussian probability.
# COUPLING: these constants were fit on logged model_prob that ALREADY included the
# NARROW_BUCKET_STD_INFLATION (1.4x) step. Keeping that inflation on + this remap
# reproduces the training condition (they compose, they do not double-count). If you
# ever change or disable NARROW_BUCKET_STD_INFLATION, re-fit these from fresh signals.
ENABLE_PROB_CALIBRATION = os.getenv("ENABLE_PROB_CALIBRATION", "true").lower() == "true"
# Constants RE-FIT 2026-07-28 on the full live-era window (signals Jul 19-29, 749
# gate-passing markets over ~10 days, scored on the markets' ACTUAL rulers — HKO Daily
# Extract for Hong Kong, Vnukovo for Moscow, WU/METAR elsewhere), CONDITIONED on the
# entry gate (raw NO edge >= 0.08) and weighted one-market-one-vote. The conditioning
# is the point: unconditionally the raw Gaussian probs are now nearly calibrated
# (fitted slope ~1.18 ≈ identity, sigma std(z) 1.01/1.15 across the two windows) —
# but on the slice where the model DISAGREES with the market
# enough to trade, the market carries real information and the model must be pulled
# toward it (adverse selection). Measured on that gated slice, the OLD constants said
# 25% where reality was 31%, and 32% where reality was 49.5% — the fake-edge zone that
# produced the July live losses (NY/GZ/TLV: model 17-23%, market 34-40%, market right).
# IMPORTANT: these constants are fit THROUGH the taper below (_calibrate_prob blends
# the logistic toward identity as p→0.5), i.e. chosen so the FINAL pipeline output —
# not the bare logistic — matches observed hit rates. A bare-logistic fit of the same
# data (+0.372/0.683) under-corrects by up to 9pp once the taper dilutes it.
# Deployed mapping: raw 0.15→0.31, 0.20→0.35, 0.30→0.40 (old: 0.28/0.31/0.37) —
# shaves 3-8pp off mid-range NO edges so only genuinely clear disagreements trade.
# On the merged gated slice this lands predicted≈observed in the heavy bins (34.3%
# predicted vs 34.7% observed where the old constants said 32% and reality was 49.5%).
# (History: ERA5 fit 1.1182/1.1619; unconditional METAR fit 2026-07-04 -0.1715/0.4457.)
# Re-fit from a bigger window as live data grows; fit harness: calfit.py session
# scratchpad 2026-07-28 (invert deployed mapping → raw, taper-aware logistic, gated cells).
PROB_CALIBRATION_INTERCEPT = float(os.getenv("PROB_CALIBRATION_INTERCEPT", "0.8000"))
PROB_CALIBRATION_SLOPE = float(os.getenv("PROB_CALIBRATION_SLOPE", "0.7480"))

# Floor on the calibrated bucket probability (bounded AND open-ended). The
# residual overconfidence after Platt lives in the extreme-low tail: on resolved
# trades the model said ~2% where reality was ~7%, and it bet real money on
# 0.01%/0.02% claims. The busts were all OPEN-ENDED buckets (Guangzhou #31, −$2,
# "34°C or higher", P(YES)=0.00008), so the floor must cover open-ended too — a
# bounded-only floor missed every bust. Whole-°C resolution + forecast noise
# means no bucket is truly < ~5% likely; clamping P(YES) up to this floor cuts
# the NO edge on the tail below the entry gate. Set 0 to disable. Verified by
# replaying the 31 closed trades: floor 0.05 drops #28/#31/#32/#35/#38 — true
# settled −$0.78 (a net-losing basket dominated by the −$2 bust), keeps all 26
# others incl the honest ~9% Chicago loss (not overconfidence).
MIN_BUCKET_PROB = float(os.getenv("MIN_BUCKET_PROB", "0.05"))

# Base forecast uncertainty in °F, keyed by hours to resolution.
# Interpolated at runtime.
#
# FLATTENED 2026-07-31. The old ramp (1.0 at 12h → 2.5 at 72h) encoded NWS skill
# decay, which is real in general but is NOT present in this bot's own record:
#
#   Spearman(lead_hours, |error|) = +0.105  (n=27, t=+0.53 — nowhere near
#                                            significant)
#   MAE under 24h lead : 2.76°F  (n=6)
#   MAE at 24h+ lead   : 2.55°F  (n=21)
#
# Error is flat in lead time, or mildly inverted. The bot trades a narrow lead
# window (mostly 18-60h) where the skill curve is genuinely shallow, and the
# short-lead end was the more dangerous one: sigma of ~1.0-1.3°F was being used
# against a real error near 2.8°F. That is a larger contributor to the tail
# overconfidence than the Gaussian kernel itself.
#
# NOTE ON UNITS: this is no longer sigma. It is the INTERCEPT term `a` of the
# regression |error| = a + b*weighted_spread_std, then scaled per direction by
# SIGMA_SCALE_* below. Read it as "the error left when the models agree
# perfectly", not as a standalone forecast error. Fitted value 0.84; the
# effective floor on sigma is a*SIGMA_SCALE (1.26°F on highs) plus MIN_SIGMA_F.
BASE_FORECAST_ERROR = {
    12:  float(os.getenv("BASE_FORECAST_ERROR_12H",  "0.84")),
    24:  float(os.getenv("BASE_FORECAST_ERROR_24H",  "0.84")),
    48:  float(os.getenv("BASE_FORECAST_ERROR_48H",  "0.84")),
    72:  float(os.getenv("BASE_FORECAST_ERROR_72H",  "0.95")),
}

# --- Sigma construction ---
# Replaces sigma = sqrt(base^2 + spread^2), which buried the one variable that
# actually predicts error. Measured on the 27 settled trades, against the RAW
# (unshifted) ensemble mean:
#
#   spread tertile   mean spread   MAE
#   tight                1.12      1.50
#   mid                  1.62      2.41
#   wide                 2.26      3.87
#   Spearman +0.461 (n=27, p≈0.016), monotonic
#
# The coefficient on spread is ~1.0. Under the old quadrature form a spread of
# 2.26 against a base of 2.0 moved sigma only 0.3°F; here it moves it 2.3°F.
#
# IMPORTANT — the tertile fit above is against max-min (range), which is NOT
# invariant to member count: adding models can only push a max-min statistic up,
# so a coefficient fitted at n=4 would silently inflate sigma the moment the
# ensemble grows. Refitted against the POPULATION STANDARD DEVIATION, which is
# member-count invariant and is what the code actually passes in:
#
#   |error| = 0.84 + 2.78 * weighted_stdev    (n=27, same data, same ranking)
#
# Note model_agreement does NOT predict error (Spearman -0.29, t=-1.52) and is
# deliberately not used here. The six largest misses all had agreement between
# 0.75 and 1.00, and the worst of them (Seoul 2026-07-25, 5.7°F) had spread 0.63
# with agreement 1.00. Unanimous models are not accurate models — which is also
# why a floor on sigma matters more than a tight-agreement bonus.
#
#   sigma = SIGMA_SCALE[is_high] * (base_error + SIGMA_SPREAD_COEF * spread_std)
SIGMA_SPREAD_COEF = float(os.getenv("SIGMA_SPREAD_COEF", "2.78"))

# Direction scale. This is the plainest finding in the whole record: highs and
# lows are two different forecasting problems, and the deployed sigma was sized
# BACKWARDS between them — wider on the accurate segment, narrower on the noisy one.
#
#              n   win rate   settled P&L   per trade   MAE    std(z)   median sigma
#   LOW       13     92.3%      +$13.27      +$1.02    1.61     0.83       2.28
#   HIGH      30     73.3%       +$3.08      +$0.10    3.01     2.58       1.79
#
# std(z) is the diagnostic: 1.0 means sigma is right-sized. Highs were running
# 2.58, i.e. errors two and a half times larger than the distribution admitted;
# lows were at 0.83, marginally too wide. Overnight minima are set by radiative
# cooling, which NWP handles well; daytime maxima depend on convection and cloud
# timing, which it does not. The gap is physical and should persist.
#
# LOW is n=13 on its own, which proves little — but four independent measurements
# (win rate, MAE, std(z), and the per-model bias sign flip) agree, and one
# mechanism predicts all four.
#
# Values are fitted, not chosen: k is set so that RMS(z) = 1.0 within each
# direction under the formula above.
#
# HIGH is fitted on the LIVE ERA ONLY (n=16), for the same reason as
# MODEL_BIAS_CORRECTIONS: the three paper rows carrying a HIGH settlement
# temperature all predate the 2026-07-23 premature-resolution fix and record
# morning readings as daily maxima, so they carry 6-8°F of fictitious error.
# Pooling them inflated k_high to 1.50, which over-widened sigma on live data
# (std(z) fell to 0.63 — a distribution 1.6x wider than the errors justify,
# which suppresses edge and costs trades for nothing).
#
#   k_high fitted on   paper only (n=3)  2.48   <- the poisoned rows
#                      pooled    (n=19)  1.36
#                      LIVE      (n=16)  1.02   <- deployed
#
# LOW stays at 0.80. The live era has only n=4 low trades with a settlement
# temperature; their fit says 0.71, but tightening sigma on n=4 is exactly the
# "fitting rules to noise faster than data arrives" failure, and 0.80 errs wide.
# Effect on the median sigma used, live era: HIGH 1.79°F -> ~2.9°F.
SIGMA_SCALE_HIGH = float(os.getenv("SIGMA_SCALE_HIGH", "1.02"))
SIGMA_SCALE_LOW = float(os.getenv("SIGMA_SCALE_LOW", "0.80"))

# Hard floor on sigma (°F), replacing the old max(std, 0.5) clamp. Tight model
# agreement is not evidence of accuracy — Seoul 2026-07-25 had the tightest
# ensemble in the record (pstdev 0.24) and missed by 5.70°F. Without a floor the
# formula would have priced that day at sigma 1.26.
MIN_SIGMA_F = float(os.getenv("MIN_SIGMA_F", "1.0"))

# Ceiling on sigma (°F), guarding the linear spread term against extrapolation.
# The coefficient is fitted over weighted-spread 0.25-1.08; a market where the
# models disagree by a weighted 3.4°F (they exist — Tokyo highs on 2026-08-01)
# would extrapolate to sigma 15°F, which is not a credible temperature forecast.
#
# MUST STAY ABOVE the gate-implied maximum. MAX_MODEL_SPREAD_STD = 1.05 caps any
# TRADEABLE market at sigma 5.64, so at 8.0 this never binds on a trade — it only
# tidies the skipped-signal log. That ordering matters: a wide sigma makes a
# narrow bucket look unlikely, which INFLATES the NO edge rather than suppressing
# it, so a cap that bit on tradeable markets would manufacture confidence, not
# remove it. If MAX_MODEL_SPREAD_STD is ever raised, re-check this bound first.
MAX_SIGMA_F = float(os.getenv("MAX_SIGMA_F", "8.0"))

# Student-t degrees of freedom for the bucket-probability kernel. 0 = Gaussian.
# Variance-matched: the scale is divided by sqrt(nu/(nu-2)) so switching kernels
# changes tail SHAPE without changing the fitted sigma's meaning.
#
# Honest note on strength of evidence: the "6 of 27 errors exceed 2 sigma" that
# motivated this was mostly a symptom of sigma being undersized on highs, not of
# genuinely fat tails. Once sigma is fitted per direction, only 1 of 27 exceeds
# 2 sigma (Seoul, |z|=2.90) where a Gaussian expects 1.2. So this is insurance,
# not a fix for a measured excess: at nu=4 a 3-sigma event is priced 4.9x higher
# than Gaussian, which is cheap protection against the regime breaks that caused
# every tail bust (Seoul -5.7°F, Chengdu -6.9°F, Lucknow +6.0°F). Set 0 to
# restore the Gaussian kernel.
SIGMA_STUDENT_T_DF = float(os.getenv("SIGMA_STUDENT_T_DF", "4"))

# --- Risk / Sizing (measurement-week mode) ---
# Goal of this profile: maximise the NUMBER of small resolved trades per week so
# execution-cost and calibration estimates converge fast — NOT to deploy more
# capital per bet. Keep positions small; widen concurrency/exposure instead.
#
# FIXED_POSITION_SIZE: every non-shadow trade stakes exactly this much, or is
# skipped. Kelly/fraction sizing is bypassed entirely — deliberately, because the
# traded slice is overconfident on its own tail (see calibration notes), so sizing
# UP on model conviction sizes up precisely where the model is least trustworthy.
# Flat stakes also make the trade log directly comparable: win rate needs no size
# weighting, so calibrate.py measures the model rather than the sizing rule.
# Set FIXED_POSITION_SIZE=0 to fall back to the old Kelly path.
#
# The stake is the SINGLE authority on per-trade size (user decision
# 2026-07-30). There used to be a second dashboard field, HARD_MAX_POSITION_SIZE,
# that clamped it via min(); with flat staking that could only ever silently
# shrink a stake the user had just raised, so it is no longer a setting. It
# survives below purely as the dollar cap on the dormant Kelly path.
FIXED_POSITION_SIZE = float(_tunable("FIXED_POSITION_SIZE", "2.0"))
# Daily loss budget, expressed in FULL STAKES rather than dollars, so it scales
# automatically when the stake changes (user decision 2026-07-29: "the daily loss
# limit is supposed to be dynamic — based off the position size"). The dollar
# limit the circuit breaker enforces is derived at CHECK TIME:
#     limit = -(effective_stake * DAILY_LOSS_STAKES)
# via daily_loss_limit() below. Default 4 stakes × the $2 default stake = -$8,
# identical to the old fixed DAILY_LOSS_LIMIT default, so behavior is continuous.
DAILY_LOSS_STAKES = float(_tunable("DAILY_LOSS_STAKES", "4"))
# Code-level backstop for the Kelly path ONLY (reached when FIXED_POSITION_SIZE
# is 0). Not runtime-tunable and absent from the dashboard: in flat-stake mode
# nothing reads it. Env-overridable so the cap can still be raised on a deploy.
HARD_MAX_POSITION_SIZE = float(os.getenv("HARD_MAX_POSITION_SIZE", "2.0"))
MAX_POSITION_FRACTION = float(os.getenv("MAX_POSITION_FRACTION", "0.10"))
MAX_TOTAL_EXPOSURE_FRACTION = float(_tunable("MAX_TOTAL_EXPOSURE_FRACTION", "0.70"))
# Default lowered 10 -> 4 by user decision 2026-07-29. Editable in the dashboard
# Settings tab. Rationale: 10 slots was never reachable on a ~$19 bankroll (the
# 70% exposure cap bound first), so it was a dead knob; on a funded account it
# would be live and 10 correlated same-day weather bets is real concentration.
# Four is a deliberate diversification floor, not a capital constraint.
MAX_CONCURRENT_POSITIONS = int(float(_tunable("MAX_CONCURRENT_POSITIONS", "4")))
BASE_POSITION_FRACTION = float(os.getenv("BASE_POSITION_FRACTION", "0.05"))
KELLY_CAP = float(os.getenv("KELLY_CAP", "0.08"))
# Polymarket's real CLOB minimum order is ~$1; below this, live orders won't fill.
MIN_POSITION_SIZE = float(os.getenv("MIN_POSITION_SIZE", "1.00"))

# Refuse entries at or above this price.
#
# RE-ARMED at 0.80 on 2026-07-31, reversing the 2026-07-28 decision to disable it.
# That decision was made on BOOKED P&L from the seven >=0.75 live entries (+$1.00,
# no busts). Booked P&L is the wrong measure here: most of those positions were
# closed early at take-profit, so it scores the scalp, not the bet. On SETTLED
# P&L — what the bet was actually worth — the whole 43-trade record says:
#
#   fill band     n   settled P&L   per trade   win rate
#   <=0.50        2      +$0.88       +$0.44       50%
#   0.50-0.60    15      +$7.47       +$0.50       73%
#   0.60-0.70    12      +$9.17       +$0.76       92%
#   0.70-0.80     4      +$2.38       +$0.59      100%
#   0.80-0.90    10      -$3.55       -$0.36       70%
#
# Everything below 0.80 is profitable: +$19.89 over 33 trades. The 0.80-0.90 band
# is the only losing band in the book. The arithmetic is unforgiving — paying 85c
# to win 15c needs ~85% accuracy, and measured accuracy at that confidence is 70%.
#
# --- StormEdge Entry Filters (Owner Decision 2026-08-06: Moderate Gate) ---
# Four constraints for trade entry, all of which must pass:
#  1. Model confidence: p_side > 0.60 (MIN_MODEL_CONFIDENCE = 0.60)
#  2. Entry price floor: fill >= 0.65 (MIN_ENTRY_PRICE = 0.65)
#  3. Entry price cap: fill <= 0.85 (MAX_ENTRY_PRICE = 0.85)
#  4. Time to resolution: < 36h (MAX_HOURS_TO_RESOLUTION = 36)
#
# Documented per owner decision on 2026-08-07. All four gates must pass for entry.
MIN_MODEL_CONFIDENCE = float(os.getenv("MIN_MODEL_CONFIDENCE", "0.60"))
MAX_MODEL_CONFIDENCE = float(os.getenv("MAX_MODEL_CONFIDENCE", "0.85"))
MIN_ENTRY_PRICE = float(os.getenv("MIN_ENTRY_PRICE", "0.65"))
MAX_ENTRY_PRICE = float(os.getenv("MAX_ENTRY_PRICE", "0.85"))

# One trade per city per target day (user rule 2026-07-28). The live log shows repeat
# same-city/same-day entries stacking correlated risk on one weather outcome: two Hong
# Kong entries on 07-26 (one lost), two Shenzhen on 07-27, two Sao Paulo on 07-29, and
# the paper-era Guangzhou market took 9 entries. Different buckets on the same
# city/date are NOT independent bets — they share one realized temperature — so a
# second "opportunity" there is mostly the same exposure at worse aggregate odds.
# Checked against the trades table (any prior entry for the city/date blocks, open or
# closed, so a stopped-out city can't be re-entered via a sibling bucket either).
ONE_TRADE_PER_CITY_DATE = os.getenv("ONE_TRADE_PER_CITY_DATE", "true").lower() == "true"

# --- Execution safety (Phase 0.3) ---------------------------------------
# 2026-08-06: a $6 market order went into a book holding $26.49 of ask depth and
# filled at 0.9818 against a 0.64 quote. Every gate passed on the quote; the
# execution then inverted the trade from a modelled +$0.78 to an actual -$0.71.
# The position will most likely still WIN (86.5%), which is what makes it
# dangerous: nothing in the ledger would ever have flagged it.
#
# Required resting ask depth, as a multiple of the stake, measured only over
# levels at or below MAX_ENTRY_PRICE. Expressed as a MULTIPLE and not as
# dollars on purpose: the requirement has to scale with the stake, and the
# $2 -> $6 stake change is precisely what made this bug reachable. A fixed
# dollar threshold would have let it happen again at the next raise.
MIN_DEPTH_MULTIPLE = float(os.getenv("MIN_DEPTH_MULTIPLE", "10.0"))
# Unknown depth REFUSES the trade. Deliberately the opposite of the veto gate's
# fail-open: an unreadable book is exactly the condition under which a taker
# order does the most damage, so "we could not check" must not mean "proceed".
REQUIRE_DEPTH_TO_TRADE = os.getenv("REQUIRE_DEPTH_TO_TRADE", "true").lower() == "true"
# Marketable LIMIT orders instead of market orders. On a $0-$1 instrument in a
# thin book a market order has no floor on execution quality — it walks until
# the size is filled at whatever the book charges.
USE_MARKETABLE_LIMIT = os.getenv("USE_MARKETABLE_LIMIT", "true").lower() == "true"
# Fill this far from the quote raises a dashboard alert. In price units, NOT
# °F — every other _F constant in this file is a temperature, so the suffix is
# deliberately absent here.
MAX_FILL_SLIPPAGE_ALERT = float(os.getenv("MAX_FILL_SLIPPAGE_ALERT", "0.03"))

# --- Correlated-exposure caps (see risk.py) ---
# ONE_TRADE_PER_CITY_DATE stops two buckets of one city-day being counted as two
# bets; MAX_CONCURRENT_POSITIONS caps how many positions exist. Neither limits
# the correlation that actually matters: one synoptic system covering several
# cities at once. Dallas and Austin, same target date, both high-bucket NO, is
# one bet on one Texas ridge sized twice — and it is what the open book held on
# 2026-08-05.
#
# In STAKES, not dollars, so raising the stake cannot silently loosen them
# (the same reasoning as DAILY_LOSS_STAKES).
ENABLE_CORRELATION_LIMITS = os.getenv("ENABLE_CORRELATION_LIMITS", "true").lower() == "true"
# 3 stakes in one synoptic group on one target date. Chosen to sit just above
# current flow: the largest same-group same-date exposure in the live book is
# 2 stakes (Dallas + Austin), so this does not bind today and does bind on the
# 4th correlated entry — which, at MAX_CONCURRENT_POSITIONS=4, is the case where
# the ENTIRE portfolio is one weather event.
MAX_GROUP_STAKES_PER_DATE = float(os.getenv("MAX_GROUP_STAKES_PER_DATE", "3"))
# 6 stakes exposed to the same SIGN of temperature surprise on one target date,
# across every group — the hemispheric case, where cities sharing no synoptic
# system still bust together. Deliberately inert at today's settings (4
# concurrent x 1 stake = 4 stakes maximum, so it cannot bind) and becomes the
# operative ceiling as soon as concurrency is raised. That is the intent: the
# limit exists before it is needed, not after.
MAX_DIRECTION_STAKES_PER_DATE = float(os.getenv("MAX_DIRECTION_STAKES_PER_DATE", "6"))

# Exit when the mid has fallen this far below entry. Set from a replay of all 52
# closed trades against Polymarket's own tick history (2026-07-26).
#
# LIVE era (16 trades, real fills): a 50% stop fires 4x — saves $0.90/$0.97/$0.56 on
# the three collapses (Guangzhou 07-23, New York 07-26, Hong Kong 07-26, all of which
# slid to zero over hours) and costs $2.29 on Chongqing 07-25, which dipped to -56.2%
# and recovered. Net +$0.14. A 60% stop clears Chongqing and nets +$1.96.
#
# PAPER era (36 trades, modeled fills): NEGATIVE at both levels (-$2.96 at 50%,
# -$3.67 at 60%) because two paper winners fell past -60% and recovered (New York
# 07-07 to 0.310, Hong Kong 07-17 to 0.358).
#
# So this is a judgement that live data supersedes paper, on a base of 3 losses.
# Deep-dip-then-recover is real and documented 3x — expect this stop to cut a winner
# eventually. 60% is the live-optimal level: it is the first threshold that catches
# all three collapses and clears Chongqing's -56.2% dip, so it fires 3/3 correct with
# no false positives. The margin above Chongqing is only ~4pp, so a future winner
# dipping past -60% would flip this — revisit once the loss sample grows past 3.
#
# SET TO 0.50 by user decision 2026-07-29, overriding the 0.60 replay result. The
# tradeoff is explicit: 50% cuts losses ~$0.20/trade sooner than 60%, but it would
# have stopped out Chongqing 07-25 at its -56.2% dip, which then recovered to a win
# (-$2.29 vs the +$1.27 it actually made). Editable in the dashboard Settings tab.
STOP_LOSS_PCT = float(_tunable("STOP_LOSS_PCT", "0.50"))
ENABLE_STOP_LOSS = str(_tunable("ENABLE_STOP_LOSS", "true")).lower() == "true"
EXIT_EDGE_FLOOR = float(os.getenv("EXIT_EDGE_FLOOR", "0.05"))
TAKE_PROFIT_PRICE = float(_tunable("TAKE_PROFIT_PRICE", "0.98"))
# Number of consecutive monitor cycles (each 5 min) the mid-price must sit materially
# BELOW the entry price before the position is force-exited, regardless of what the edge
# formula computes. This catches cases where the forecast probability is stale/wrong and
# inflates the apparent edge while the market is telling a different story. Default 3 = 15 min.
SUSTAINED_LOSS_POLLS = int(os.getenv("SUSTAINED_LOSS_POLLS", "3"))
# Minimum fractional drawdown (mid below entry) that COUNTS as a sustained loss. Without
# this, the guard fired on 1-2¢ book noise (the Guangzhou churn: 8 exits at −2.3%, each a
# winning position dumped then re-bought). On a $0-$1 instrument the max loss is the stake,
# so a stop only makes sense once the move is real. Default 0.10 = mid must be ≥10% under
# entry for the streak to accrue. Set to 0 to restore the old any-dip behaviour.
SUSTAINED_LOSS_MIN_DROP = float(os.getenv("SUSTAINED_LOSS_MIN_DROP", "0.10"))
# MASTER SWITCH for the sustained-loss guard. Turned OFF after a backtest on the first 22
# trades: even at a 10% floor the guard would have fired on 5 positions, and 4 of them were
# eventual WINNERS whose price merely dipped mid-life before recovering to a $1 settlement
# (id2/5/10/14/15). Honoring it would have forfeited ~$5.00 of winning settlements to avoid
# ~$1.60 of loss — net −$3.40. Same-day weather books wobble 15-25% intraday and recover;
# on a $0-$1 instrument the max loss is the stake anyway, so pure hold-to-resolution wins.
# Flip back to true (and re-tune the floor / add a time-to-resolution gate) once a larger
# sample shows a real thesis-break signature worth cutting on.
ENABLE_SUSTAINED_LOSS_GUARD = os.getenv("ENABLE_SUSTAINED_LOSS_GUARD", "false").lower() == "true"
# Cooldown (hours) before re-entering a market we previously EXITED. Blocks the exit-churn
# loop where a position is force-closed on noise and immediately re-opened on the next scan,
# paying spread+fee each round-trip. Default 24h ≈ don't re-touch the same market same day.
REENTRY_COOLDOWN_HOURS = float(os.getenv("REENTRY_COOLDOWN_HOURS", "24"))

# Edge-decay exit gating. The raw edge = (1 - model_prob) - price for a NO bet drops
# below EXIT_EDGE_FLOOR for TWO opposite reasons, and only one is a reason to sell:
#   (a) the PRICE converged in our favour (NO rose toward 1.0) — the bet is WINNING and
#       the thesis is intact. Exiting here caps a winner for pennies (the bug: three
#       live NO trades bailed at +$0.05 instead of holding to a ~$1.00 settlement).
#   (b) the FORECAST turned against us (model_prob rose vs entry) — the thesis is broken.
# HOLD_WINNERS_TO_RESOLUTION makes edge decay fire only on case (b): the model's own
# probability for the bet must have deteriorated by more than THESIS_BREAK_PROB_DELTA
# from entry, OR the position must be in a real loss. A position that's simply converged
# in our favour is held for the full $1/$0 settlement instead of scalped early.
HOLD_WINNERS_TO_RESOLUTION = os.getenv("HOLD_WINNERS_TO_RESOLUTION", "true").lower() == "true"
# How much the model's probability-for-our-side must worsen vs entry before edge decay
# counts the thesis as broken (in probability units). 0.10 = the bucket we bet AGAINST
# became 10 percentage points more likely than when we entered.
THESIS_BREAK_PROB_DELTA = float(os.getenv("THESIS_BREAK_PROB_DELTA", "0.10"))
# MASTER SWITCH for the edge-decay / thesis-break early exit. Turned OFF alongside the
# sustained-loss guard: the backtest showed a 10-point thesis-break would have fired on 5
# of the first 22 trades, and 4 were eventual WINNERS (id2/6/14/15) — intraday forecast
# runs swing 15-20 points before settling, so the "break" was just mid-life noise. With
# this off the bot is PURE hold-to-resolution: no position exits early except Take Profit
# (selling at $0.98+, which only captures near-certain wins) and true $1/$0 settlement.
# Re-enable once a bigger sample distinguishes a real reversal from forecast wobble.
ENABLE_THESIS_BREAK_EXIT = os.getenv("ENABLE_THESIS_BREAK_EXIT", "false").lower() == "true"

# --- Market Filters ---
MIN_VOLUME = float(os.getenv("MIN_VOLUME", "500"))
MAX_HOURS_TO_RESOLUTION = float(os.getenv("MAX_HOURS_TO_RESOLUTION", "36"))

# --- Market Discovery ---
# Markets per API page (Gamma API max is 100)
MARKET_DISCOVERY_LIMIT = int(os.getenv("MARKET_DISCOVERY_LIMIT", "100"))
# Maximum pages to fetch per scan cycle before giving up. Measured live 2026-07-09:
# the real pool of active, within-72h weather events is ~1,400+ bucket markets, which
# needs ~20 pages at LIMIT=100 to fully enumerate — 5 pages (500 events) was silently
# truncating discovery to well under half the live universe before Phase 1.5 ever ran.
MARKET_DISCOVERY_MAX_PAGES = int(os.getenv("MARKET_DISCOVERY_MAX_PAGES", "20"))
# Kept for backwards-compat but no longer used as primary stop — expiry detection stops pagination
MARKET_DISCOVERY_STOP_AFTER_WEATHER = int(os.getenv("MARKET_DISCOVERY_STOP_AFTER_WEATHER", "500"))
# Max markets sent to CLOB orderbook API per scan (scored by liquidity + price uncertainty).
# Raised from 150: measured live 2026-07-09 that the real prefiltered candidate pool is
# ~1,377 markets/cycle, so 150 (10.9%) was leaving ~89% of live weather markets unseen
# on every scan. MIN_VOLUME is now enforced in Phase 1.5 (before this cap), so raising
# this only adds real, sufficiently-liquid candidates — not more low-volume noise.
MAX_CLOB_CANDIDATES = int(os.getenv("MAX_CLOB_CANDIDATES", "1200"))
# Max bucket markets evaluated per city/date pair (prevents one city dominating the cap)
MAX_BUCKETS_PER_CITY_DATE = int(os.getenv("MAX_BUCKETS_PER_CITY_DATE", "5"))

# --- Shadow / Diagnostic Mode ---
# Shadow logging is always active when strict evaluation fails — helps tune thresholds.
# Exploration trades are placed only when ENABLE_SHADOW_EXPLORATION=true AND PAPER_MODE=true.
SHADOW_MIN_AGREEMENT = float(os.getenv("SHADOW_MIN_AGREEMENT", "0.50"))
# RENAMED from SHADOW_MAX_SPREAD and RESCALED 2026-07-31, for the same reason
# MAX_MODEL_SPREAD became MAX_MODEL_SPREAD_STD: the statistic it is compared
# against changed from max-min to weighted standard deviation, and the constant
# did not. At 5.0 against a weighted sd whose observed maximum is 2.78, the
# shadow spread gate could not bind on any market that has ever been seen — the
# relaxed-gate diagnostic was reporting "spread ok" unconditionally.
#
# 2.0 keeps the original intent: roughly twice the strict gate, so the shadow
# universe is meaningfully wider than the live one without being unbounded.
# (Strict is MAX_MODEL_SPREAD_STD = 1.05; on the 59-signal trail, 2.0 admits
# ~95% of signals against the strict gate's ~80%.)
SHADOW_MAX_SPREAD_STD = float(os.getenv("SHADOW_MAX_SPREAD_STD", "2.0"))
SHADOW_MAX_SIZE_USDC = float(os.getenv("SHADOW_MAX_SIZE_USDC", "0.25"))
ENABLE_SHADOW_EXPLORATION = os.getenv("ENABLE_SHADOW_EXPLORATION", "false").lower() == "true"

# --- Debugging ---
DEBUG_MARKET_SCAN = os.getenv("DEBUG_MARKET_SCAN", "false").lower() == "true"
DEBUG_MARKET_SCAN_VERBOSE = os.getenv("DEBUG_MARKET_SCAN_VERBOSE", "false").lower() == "true"
# When true, prints only weather-classified markets and exits without trading
DEBUG_WEATHER_DISCOVERY = os.getenv("DEBUG_WEATHER_DISCOVERY", "false").lower() == "true"

# --- Scheduling (minutes) ---
SCAN_INTERVAL_MINUTES = int(os.getenv("SCAN_INTERVAL_MINUTES", "10"))
MONITOR_INTERVAL_MINUTES = int(os.getenv("MONITOR_INTERVAL_MINUTES", "5"))

# --- Polymarket Credentials ---
POLYMARKET_PK = os.getenv("POLYMARKET_PK", "")
CLOB_API_KEY = os.getenv("CLOB_API_KEY", "")
CLOB_SECRET = os.getenv("CLOB_SECRET", "")
CLOB_PASS_PHRASE = os.getenv("CLOB_PASS_PHRASE", "")
# Wallet type wiring for the CLOB client. If the Polymarket account was created
# through the website, funds live in a smart-contract wallet (NOT the raw EOA of
# POLYMARKET_PK) and orders must carry the matching signature_type plus that
# wallet's address as funder. Accounts created since ~2026 use the DEPOSIT-WALLET
# architecture: funds are held as pUSD in a deposit-wallet contract and orders
# need signature_type 3 (POLY_1271, ERC-1271 contract signatures — requires the
# V2 client). The funder is the address shown on the Polymarket profile page
# ("Your Polymarket Wallet Address"). Older accounts: 1=Magic/email proxy,
# 2=browser-wallet proxy. Leave unset only for a raw EOA trading for itself.
POLYMARKET_FUNDER = os.getenv("POLYMARKET_FUNDER", "")           # proxy/deposit wallet address
POLYMARKET_SIG_TYPE = int(os.getenv("POLYMARKET_SIG_TYPE", "0"))  # 0=EOA, 1=Magic, 2=browser, 3=deposit wallet

# --- GFS warm-bias corrections (°F) per city ---
# GFS consistently runs warm in humid/coastal cities. Values derived from
# NWS MOS verification studies. Keyed by station name matching STATIONS dict.
GFS_BIAS_CORRECTIONS = {
    k: float(v) for k, v in (
        pair.split(":") for pair in
        os.getenv("GFS_BIAS_CORRECTIONS", "Miami:-1.5,Houston:-1.5,Dallas:-1.2,Atlanta:-1.0").split(",")
        if ":" in pair
    )
}

# --- Global per-model bias corrections (°F), keyed by (model, is_high) ---
# SIGN CONVENTION: the value is ADDED to the model's forecast, so it is the
# NEGATIVE of the model's measured bias (mean(forecast - actual)).
#
# These used to be one signed offset per model, applied in both directions and
# all positive. That was wrong, and measurably so. Re-measured 2026-07-31 on the
# 108 model-forecast rows in the full export (mean(forecast - actual), split on
# is_high):
#
#   model          bias on HIGH   bias on LOW   swing   native timestep
#   icon_global       -0.43         +0.36        0.79   hourly
#   ecmwf_ifs025      -0.73         +0.36        1.10   3-hourly
#   gem_global        +0.09         +0.29        0.20   3-hourly
#   jma_gsm           -1.32         +1.71        3.03   6-hourly
#
# Every model except GEM flips sign between directions, and the size of the flip
# scales with how coarse the model's native output timestep is. That is the
# signature of interpolation, not of model bias: Open-Meteo resamples coarse
# model output onto an hourly grid, and smoothing a diurnal curve clips the
# afternoon peak and lifts the overnight trough. Forecasts therefore come out
# too COLD on highs and too WARM on lows, in proportion to the timestep.
#
# Verified 2026-07-31 that `daily=temperature_2m_max` is exactly the max of
# `hourly=temperature_2m` over the local day (checked on jma_gsm/ecmwf/icon,
# identical to 0.1°F). So this artifact cannot be dodged by computing the daily
# aggregate locally — the interpolation happens upstream of both. Correcting it
# per direction here is the fix; preferring natively-hourly models is the other.
#
# CRITICAL: the biases above are RESIDUALS, not raw model biases. The stored
# forecasts they were measured on already had the previous corrections applied
# (weather.py corrects inside fetch_forecasts, before raw_models is persisted;
# those corrections shipped 2026-07-01, ahead of the 07-04..07-28 sample). So
# the new correction is
#       c_new = c_old - B         NOT   c_new = -B
# Using -B would have thrown away the correction already baked into the numbers
# and left jma_gsm 1.55°F too cold on every high.
#
#   model          c_old   B_high   -> HIGH    B_low   -> LOW
#   ecmwf_ifs025   +0.29   -0.73     +1.02     +0.36    (held)
#   icon_global    +0.03   -0.43     +0.46     +0.36    (held)
#   gem_global     +1.32   +0.09     +1.23     +0.29    (held)
#   jma_gsm        +1.55   -1.32     +2.87     +1.71    (held)
#
# NOTE: the HIGH column above is SUPERSEDED — B_high there is the pooled
# paper+live measurement, and the deployed values come from the live-era-only
# re-fit further down (+2.29/+1.74/+2.46/+3.99). The arithmetic shown is still
# the arithmetic used; only the sample changed. The LOW column is current.
#
# WHY THE LOW COLUMN IS HELD AT ITS OLD VALUE. The measured low-side biases are
# not robust: they rest on n=5-8 rows, and dropping Hong Kong — the one city with
# a confirmed INVERTED settlement ruler — flips their sign.
#
#   low-side B      with HK                without HK
#   ecmwf_ifs025    +0.36                  -0.19
#   icon_global     +0.36                  -0.14
#   gem_global      +0.29                  -0.31
#   jma_gsm         +1.71 (n=5)            +0.95 (n=3)
#
# So "forecasts run warm on lows" is largely two Hong Kong trades scored against
# the wrong station. The HIGH column is stable to the same test (-0.73 -> -0.70,
# -0.43 -> -0.47, -1.32 -> -1.40, n=18) and is deployed. Lows are also the
# profitable segment (92% win rate, +$1.02/trade) — not the place to act on a
# sign that flips when one city is removed. Re-fit lows after the ruler is fixed.
#
# The SWING is unaffected by this correction (adding a constant to both
# directions cancels), so the diurnal-compression mechanism above stands as
# measured; only the levels needed the c_old term restored.
#
# Net effect at the ensemble level on the 27 settled trades, with
# METAR_WARM_CORRECTION_F also going to 0:
#
#                                   HIGH bias   HIGH MAE   LOW bias   LOW MAE
#   deployed (c_old + METAR 1.3)      +0.83       3.01       +1.70      2.29
#   this commit                       +0.07       2.95       +0.40      1.62
#
# n=108 forecast rows over 27 city-days: a first read, not a settled value.
# Re-fit once model_accuracy has direction-split rows (see db.py migration).
# gfs_global is deliberately absent — it already carries per-city corrections in
# GFS_BIAS_CORRECTIONS, and its n=3 residual would double-count them.
def _parse_dir_bias(raw):
    """Parse "model:high:low,..." into {(model, is_high): correction}.

    RAISES on anything that is not exactly `model:high:low` with two parseable
    floats. It used to `continue` past a malformed entry, which meant the OLD
    two-field format ("model:value") parsed to an EMPTY dict — silently, with no
    log line. Paired with the timestep-prior fallback that used to sit below,
    that produced a fully populated and entirely wrong correction table from one
    stale environment variable. An env var here is either wholly valid or the
    process refuses to boot; a partial dict is never acceptable, because the
    models that fall out of it are exactly the ones that then go uncorrected."""
    out = {}
    for part in raw.split(","):
        token = part.strip()
        if not token:
            raise ValueError(
                f"MODEL_BIAS_CORRECTIONS: empty entry in {raw!r} (trailing or "
                f"doubled comma). Expected 'model:high:low' per entry."
            )
        bits = token.split(":")
        if len(bits) != 3:
            raise ValueError(
                f"MODEL_BIAS_CORRECTIONS: entry {token!r} has {len(bits)} "
                f"field(s), expected 3 ('model:high:low'). The two-field form "
                f"'model:value' is the pre-2026-07-31 format and is no longer "
                f"valid — corrections are now keyed by direction."
            )
        model, hi, lo = bits[0].strip(), bits[1].strip(), bits[2].strip()
        if not model:
            raise ValueError(
                f"MODEL_BIAS_CORRECTIONS: entry {token!r} has an empty model id."
            )
        try:
            out[(model, True)] = float(hi)
            out[(model, False)] = float(lo)
        except ValueError:
            raise ValueError(
                f"MODEL_BIAS_CORRECTIONS: entry {token!r} has non-numeric "
                f"correction(s) (high={hi!r}, low={lo!r})."
            ) from None
    if not out:
        raise ValueError("MODEL_BIAS_CORRECTIONS parsed to an empty table.")
    return out


# FITTED ON THE LIVE ERA ONLY (2026-07-18 onward, n=14-16 per model). The
# paper-era rows must be excluded, not merely down-weighted: every paper trade
# that carries a settlement temperature targets 2026-07-18, BEFORE the
# premature-resolution fix shipped on 2026-07-23, so its "actual" is a mid-day
# reading recorded as a daily maximum. Their measured bias is +6.0 to +6.6°F
# against -1.1 to -2.4°F on live, and the implausibility is visible by eye —
# Taipei 86.0°F and Lucknow 84.2°F as July daily highs are morning temperatures.
#
# Pooling the two eras dragged every HIGH constant. Measured on the live era:
#
#   correction set                    bias    MAE   std(z)
#   raw API, no correction           -2.45   2.73    0.94
#   previous deployed (c_old+METAR)  -0.47   2.20    1.03
#   pooled paper+live fit            -1.09   2.24    0.63   <- worse than before
#   live-only fit (this)             +0.10   2.09    0.98
#
# The pooled fit was a REGRESSION on the only regime with real fills. Honest
# caveat: bias going to ~0 is tautological (c_new is defined to zero it), so the
# real evidence here is MAE 2.20 -> 2.09 and std(z) 0.63 -> 0.98.
#
# gfs_global CARRIES AN EXPLICIT ZERO, and must never be merely absent from this
# table. It already has per-city corrections in GFS_BIAS_CORRECTIONS, so a model
# -level correction on top would double-count them. Until 2026-07-31 it was
# simply omitted, and the lookup below fell through to a timestep prior that
# handed it +0.7°F on highs and -0.4°F on lows — a correction it had never had,
# on a member carrying 20-30% of ensemble weight in every US/EU city. The
# comment asserting it was "deliberately absent" was true and the code ignored
# it, which is the whole argument for stating the zero rather than implying it.
#
# Its own measured residual is +3.17°F on highs, and that is NOT used: n=3, and
# those three rows straddle cities where GFS_BIAS_CORRECTIONS applied and cities
# where it did not, so the figure mixes two correction states and measures
# neither. Re-measure per city, or not at all.
MODEL_BIAS_CORRECTIONS = _parse_dir_bias(os.getenv(
    "MODEL_BIAS_CORRECTIONS",
    "ecmwf_ifs025:2.29:0.29,icon_global:1.74:0.03,"
    "gem_global:2.46:1.32,jma_gsm:3.99:1.55,gfs_global:0:0",
))

# ADVISORY ONLY — nothing reads these at runtime. They are the starting point a
# HUMAN should use when adding a model to WEIGHTS, quoted back in the error
# message below so the decision is made once, in this file, on purpose.
#
# These were a runtime FALLBACK until 2026-07-31, and that is precisely how
# gfs_global acquired a wrong-signed correction nobody chose. A prior good enough
# to reason from is not good enough to apply silently: the difference between
# "our best guess for an unmeasured model" and "the number we ship" is exactly
# the review step that was missing.
MODEL_TIMESTEP_HOURS = {
    "icon_global": 1, "icon_eu": 1, "icon_d2": 1, "gfs_hrrr": 1, "jma_msm": 1,
    "ecmwf_ifs025": 3, "gem_global": 3, "gfs_global": 3, "ukmo_global_deterministic_10km": 3,
    "meteofrance_arpege_world": 3, "cma_grapes_global": 3, "bom_access_global": 3,
    "jma_gsm": 6, "ecmwf_aifs025_single": 6, "gfs_graphcast025": 6, "ncep_aigfs025": 6,
}
TIMESTEP_BIAS_PRIOR = {           # (suggested HIGH, suggested LOW) — see above
    1: (0.4, -0.4),
    3: (0.7, -0.4),
    6: (1.3, -1.7),
}


def model_bias_correction(model, is_high):
    """°F to ADD to `model`'s forecast for this direction.

    RAISES KeyError for a model with no entry in MODEL_BIAS_CORRECTIONS. There
    is deliberately no fallback: every model in WEIGHTS must carry an explicit,
    reviewed correction — zero included (see gfs_global). WEIGHTS is a
    code-controlled table, so the only way to reach this error is to add a model
    without deciding its correction, which is the decision this refuses to make
    on your behalf. validate_model_tables() surfaces it at boot rather than
    mid-scan."""
    try:
        return MODEL_BIAS_CORRECTIONS[(model, bool(is_high))]
    except KeyError:
        step = MODEL_TIMESTEP_HOURS.get(model)
        hint = ""
        if step is not None:
            hi, lo = TIMESTEP_BIAS_PRIOR[step]
            hint = (f" Its native timestep is {step}h; the advisory prior for "
                    f"that class is high={hi:+.1f} low={lo:+.1f}, but measure it "
                    f"if you can.")
        raise KeyError(
            f"No bias correction for model {model!r} (is_high={bool(is_high)}). "
            f"Every model in weather.WEIGHTS needs an explicit entry in "
            f"MODEL_BIAS_CORRECTIONS, including an explicit 0 for models "
            f"corrected elsewhere.{hint}"
        ) from None

# --- Data retention (days) ---
# 14d default (was 60): at ~2,100 signal rows/day × ~2.5KB the 60-day steady
# state is ~320MB and filled the original 1GB volume; 14d is plenty because
# calibration scores resolved rows within days.
# --- Intraday observation conditioning (see intraday.py) ---
# Nothing conditioned on observations already in hand before 2026-08-05: at
# 15:00 local with the station reading 91°F, the bot was still pricing
# "will the max be below 91?" off a 00Z forecast.
ENABLE_INTRADAY_CONDITIONING = os.getenv(
    "ENABLE_INTRADAY_CONDITIONING", "true").lower() == "true"
# Before this local hour the running extreme carries almost no information about
# the day's peak — pre-dawn the "max so far" is just the overnight low. 6 is
# roughly sunrise across the traded latitudes in either season.
INTRADAY_MIN_HOURS_ELAPSED = float(os.getenv("INTRADAY_MIN_HOURS_ELAPSED", "6"))
# Floor on the conditioned sigma. Late in the day the fitted remaining-rise sd
# approaches zero, and a sigma of literally zero would price every bucket at 0
# or 1 — betting the farm on a single METAR reading and on the assumption that
# no correction will ever be issued. 0.5°F is under the °C settlement
# quantisation, so it does not blunt the edge that conditioning creates.
INTRADAY_SIGMA_FLOOR_F = float(os.getenv("INTRADAY_SIGMA_FLOOR_F", "0.5"))

# Fitted fraction of the day's diurnal range still to come at each local hour.
# f = still to RISE (conditions a daily max), g = still to FALL (a daily min).
# Dimensionless, so it travels across climates and hemispheres; multiplied at
# runtime by the diurnal range the ensemble forecasts for that day.
#
# Fitted over 12 months of METAR from ten stations spanning the traded climate
# zones (fit_remaining_rise.py, now in git history only — restore it to refit).
# Do not hand-edit.
REMAINING_RISE_TABLE = {
     0: {"f_mean": 0.6974, "f_sd": 0.2425, "g_mean": 0.2789, "g_sd": 0.2361},
     1: {"f_mean": 0.6863, "f_sd": 0.2421, "g_mean": 0.2268, "g_sd": 0.2229},
     2: {"f_mean": 0.6786, "f_sd": 0.2414, "g_mean": 0.1848, "g_sd": 0.2095},
     3: {"f_mean": 0.6742, "f_sd": 0.2401, "g_mean": 0.1446, "g_sd": 0.1939},
     4: {"f_mean": 0.6699, "f_sd": 0.2396, "g_mean": 0.1124, "g_sd": 0.1841},
     5: {"f_mean": 0.6660, "f_sd": 0.2391, "g_mean": 0.0864, "g_sd": 0.1718},
     6: {"f_mean": 0.6577, "f_sd": 0.2370, "g_mean": 0.0678, "g_sd": 0.1617},
     7: {"f_mean": 0.6286, "f_sd": 0.2286, "g_mean": 0.0595, "g_sd": 0.1531},
     8: {"f_mean": 0.5430, "f_sd": 0.2143, "g_mean": 0.0555, "g_sd": 0.1465},
     9: {"f_mean": 0.4151, "f_sd": 0.1980, "g_mean": 0.0531, "g_sd": 0.1419},
    10: {"f_mean": 0.2848, "f_sd": 0.1759, "g_mean": 0.0511, "g_sd": 0.1379},
    11: {"f_mean": 0.1793, "f_sd": 0.1537, "g_mean": 0.0489, "g_sd": 0.1337},
    12: {"f_mean": 0.1046, "f_sd": 0.1255, "g_mean": 0.0467, "g_sd": 0.1298},
    13: {"f_mean": 0.0539, "f_sd": 0.0962, "g_mean": 0.0452, "g_sd": 0.1264},
    14: {"f_mean": 0.0258, "f_sd": 0.0723, "g_mean": 0.0432, "g_sd": 0.1233},
    15: {"f_mean": 0.0123, "f_sd": 0.0560, "g_mean": 0.0413, "g_sd": 0.1187},
    16: {"f_mean": 0.0065, "f_sd": 0.0430, "g_mean": 0.0393, "g_sd": 0.1150},
    17: {"f_mean": 0.0043, "f_sd": 0.0357, "g_mean": 0.0371, "g_sd": 0.1099},
    18: {"f_mean": 0.0030, "f_sd": 0.0301, "g_mean": 0.0331, "g_sd": 0.1011},
    19: {"f_mean": 0.0024, "f_sd": 0.0271, "g_mean": 0.0285, "g_sd": 0.0915},
    20: {"f_mean": 0.0019, "f_sd": 0.0241, "g_mean": 0.0229, "g_sd": 0.0777},
    21: {"f_mean": 0.0012, "f_sd": 0.0183, "g_mean": 0.0157, "g_sd": 0.0593},
    22: {"f_mean": 0.0006, "f_sd": 0.0107, "g_mean": 0.0092, "g_sd": 0.0428},
    23: {"f_mean": 0.0000, "f_sd": 0.0000, "g_mean": 0.0000, "g_sd": 0.0000},
}

SIGNAL_RETENTION_DAYS = int(os.getenv("SIGNAL_RETENTION_DAYS", "14"))
SKIP_SIGNAL_RETENTION_DAYS = int(os.getenv("SKIP_SIGNAL_RETENTION_DAYS", "3"))
# Two carve-outs from the SKIP purge above, kept indefinitely. The skip trail is
# the largest calibration sample this system produces (~40,000 scored markets a
# day against 27 settled trades in total), and deleting it wholesale to save disk
# is why every constant in this file is fitted on a sample too small to support
# it. The volume has since been extended, so the original reason is gone.
#
# Retained rows survive SIGNAL_RETENTION_DAYS too, but shed their raw_models
# JSON once past the skip window — that JSON is ~2.5KB of a ~2.7KB row and only
# helps debug a recent scan, while the scalars the calibration fits on are
# columns. Cost at 5%: ~2,000 rows/day, ~150MB/year rather than ~1.8GB/year.
SKIP_SIGNAL_SAMPLE_PCT = int(os.getenv("SKIP_SIGNAL_SAMPLE_PCT", "5"))
# Keep every skip that cleared the edge bar but was stopped by another gate —
# the counterfactual set that says whether the gates are earning their keep.
# None/blank disables. Defaults to the entry threshold.
_nm = os.getenv("SKIP_SIGNAL_NEAR_MISS_EDGE", "").strip()
SKIP_SIGNAL_NEAR_MISS_EDGE = float(_nm) if _nm else EDGE_THRESHOLD
# Replay recorder retention. replay_signals + replay_gates log EVERY evaluated
# market every cycle (~120k signal rows and ~1M gate rows a day at 31 cities)
# and filled the 1GB volume in under three days after the 2026-08-04 account
# move — the volume extension that justified indefinite skip retention above
# did not survive the migration, and these tables had no purge at all.
#
# Same policy as the skip purge: a short full-fidelity window for debugging
# recent scans, plus two carve-outs kept past it for the harness's
# out-of-sample fitting — a deterministic id-cohort sample and every row whose
# edge cleared the entry threshold (the counterfactuals with information in
# them). Retained rows shed raw_models_pre_correction once past the window;
# the scalar stage columns the harness fits on are unaffected. Gate rows are
# deleted with their parent signal row.
REPLAY_RETENTION_DAYS = int(os.getenv("REPLAY_RETENTION_DAYS", "2"))
REPLAY_SAMPLE_PCT = int(os.getenv("REPLAY_SAMPLE_PCT", "5"))
SCAN_LOG_RETENTION_DAYS = int(os.getenv("SCAN_LOG_RETENTION_DAYS", "14"))
NOTIFICATION_RETENTION_DAYS = int(os.getenv("NOTIFICATION_RETENTION_DAYS", "30"))
# Monitor-cycle position trail: life of the position plus 90 days MINIMUM.
#
# The floor is enforced, not advisory — max()'d rather than range-checked,
# because the failure this table exists to fix was caused by deletion. The
# skip-signal purge shortened a calibration sample to save disk and left every
# constant in this file fitted on a sample too small to support it; the same
# reflex applied here would re-blind the exact window the stop-loss question
# needs. There is no disk argument to weigh against it: one row per open
# position per 5 minutes is ~1,150 rows for four positions held a full day,
# against ~2,100 signal rows PER DAY.
POSITION_TRAIL_RETENTION_DAYS = max(
    90, int(os.getenv("POSITION_TRAIL_RETENTION_DAYS", "90")))

# --- External APIs ---
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
GAMMA_API_URL = "https://gamma-api.polymarket.com/markets"
GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
CLOB_BASE_URL = "https://clob.polymarket.com"
DATA_API_URL = "https://data-api.polymarket.com"

# --- External-close sync ---
# A position sold manually on the Polymarket website leaves the bot's DB row
# open until resolution, and resolution then books $1/$0 instead of the price
# actually received (first live case: Guangzhou NO sold at $0.87, resolution
# would have credited $1.00). The sync compares DB positions against the
# wallet's real token balances each monitor cycle and books manual sales at
# their actual fill price. Positions younger than this many minutes are skipped
# so Data-API indexing lag on a fresh entry can't be mistaken for a sale.
EXTERNAL_CLOSE_SYNC_MIN_AGE_MIN = int(os.getenv("EXTERNAL_CLOSE_SYNC_MIN_AGE_MIN", "15"))


# --- Independent forecast veto gate (see independent.py) -------------------
# A second opinion from OUTSIDE the Open-Meteo ensemble, used only to REFUSE
# trades. Never to adjust a probability: blending an unweighted external
# forecast into ensemble_mean or sigma would make it a fifth model with no
# measured bias correction and no place in the family caps.
#
# It is a blunder detector, not a forecast improvement. It catches wrong
# station, stale run, misparsed bucket, correlated-ensemble collapse and
# wrong-signed bias correction — the failures that live UPSTREAM of the
# probability calculation and are therefore invisible to it. Every one of those
# has happened in this project at least once, and each loses the full stake.
#
# ARMED AT DEPLOY — OWNER DECISION 2026-08-05, no shadow period.
#
# The trade-off, on record. Arming without a shadow period means the fire rate
# is UNKNOWN at deploy. If a station is mismatched or a provider returns a
# different window than expected, the gate fires constantly and trade flow
# stops. That is the same class of failure as MAX_MODEL_SPREAD_STD, which now
# rejects 78% of evaluations — a gate correct in intent and wrong in units.
#
# Two mitigations replace the shadow period, and both are required:
#   (a) loose thresholds, below;
#   (b) the auto-disable tripwire, INDEPENDENT_VETO_MAX_FIRE_RATE.
# Neither is optional and neither should be removed without the other.
INDEPENDENT_VETO_ENABLED = os.getenv("INDEPENDENT_VETO_ENABLED", "true").lower() == "true"

# ~2x the measured MAE of 2.59°F, and deliberately loose. Sized to catch a wrong
# station or a stale run, NOT a marginal forecast difference: a 5°F disagreement
# between two competent forecasts at a 24-48h horizon is not a difference of
# opinion, it is a bug. Do not tighten without evidence from the 14-day review.
DISAGREEMENT_VETO_F = float(os.getenv("DISAGREEMENT_VETO_F", "5.0"))

# Half-width of the band around the independent forecast in which the outcome
# being bet AGAINST is considered live. If a NO bet's bucket overlaps this band,
# the second opinion thinks the bucket can happen — refuse.
PLAUSIBLE_BAND_F = float(os.getenv("PLAUSIBLE_BAND_F", "2.0"))

# 6h. Ceiling of 51 cities x 3 refreshes = 153 calls/day, inside DataHub's 360.
# At the 10-minute scan interval an uncached gate would be ~7,300 calls/day.
INDEPENDENT_CACHE_TTL_SECONDS = int(os.getenv("INDEPENDENT_CACHE_TTL_SECONDS", "21600"))

# Short, and NOT the 10s used for Open-Meteo. This runs inside trade evaluation
# for a signal that is allowed to be missing, so the design is "answer fast or
# don't answer" — a slow provider must not slow the book down.
INDEPENDENT_TIMEOUT_SECONDS = float(os.getenv("INDEPENDENT_TIMEOUT_SECONDS", "3.0"))

# Auto-disable tripwire, evaluated over a rolling 24h window. A gate meant to
# catch RARE blunders that is firing on a quarter of the signals it sees is
# itself the blunder — so it disables itself, logs at ERROR and notifies, rather
# than continuing to stop the book on what is almost certainly an artefact.
INDEPENDENT_VETO_MAX_FIRE_RATE = float(os.getenv("INDEPENDENT_VETO_MAX_FIRE_RATE", "0.25"))

# Don't evaluate the tripwire on a handful of signals: 1 veto out of 2 is 50%
# and means nothing. This is the denominator below which the rate is noise.
INDEPENDENT_VETO_MIN_SAMPLE = int(os.getenv("INDEPENDENT_VETO_MIN_SAMPLE", "20"))

# Share of all vetoes in a single city that triggers an ERROR naming it. That
# is the station-mismatch signature, and it is exactly what this gate exists to
# surface — the response is to fix the station, never to loosen the threshold.
INDEPENDENT_VETO_CITY_CONCENTRATION = float(
    os.getenv("INDEPENDENT_VETO_CITY_CONCENTRATION", "0.50"))

# Circuit breaker: consecutive failures before a provider is left alone, and for
# how long. Per provider, so DataHub being down does not blind the NWS cities.
INDEPENDENT_BREAKER_FAILURES = int(os.getenv("INDEPENDENT_BREAKER_FAILURES", "5"))
INDEPENDENT_BREAKER_COOLDOWN_SECONDS = int(
    os.getenv("INDEPENDENT_BREAKER_COOLDOWN_SECONDS", "3600"))

# api.weather.gov REQUIRES a User-Agent identifying the application with a
# contact address, and rejects requests without one. DataHub does not require it
# but is sent the same string.
INDEPENDENT_USER_AGENT = os.getenv(
    "INDEPENDENT_USER_AGENT",
    "stormedge-weather-bot (frederickrowley7@gmail.com)")

# UK Met Office DataHub key, for the 40 non-US cities. Free plan, 360 calls/day.
# ABSENT IS NOT ZERO: with no key those cities resolve to INCONCLUSIVE, never to
# NO_DATA, so an unset key can never be misread as "UKMO covers nothing there".
METOFFICE_DATAHUB_KEY = os.getenv("METOFFICE_DATAHUB_KEY", "").strip()


# --- Runtime settings store (hot-reload, no restart) ---
# The managed money/risk knobs are read through setting() at their CALL SITES
# (strategy sizing, executor exits/concurrency, the circuit breaker) instead of
# being frozen by `from config import X`. The dashboard's settings POST calls
# apply_runtime_overrides() after persisting, so a change takes effect on the
# very next decision the bot makes — no process restart (user decision
# 2026-07-29, replacing the earlier save-and-restart design).
#
# Everything OUTSIDE MANAGED_SETTINGS keeps the plain import-time constant
# behavior on purpose: calibration constants and strategy gates are
# code-controlled and should require a deploy to change.
import threading as _threading

_RUNTIME_LOCK = _threading.RLock()
_RUNTIME = {
    # Whether trades are simulated. Deliberately NOT in app.SETTING_SPECS: it is
    # not an ordinary knob and must never ride along in a bulk settings save.
    # The dashboard changes it only through /api/trading-mode, which gates
    # paper -> live behind the readiness preflight.
    "PAPER_MODE": PAPER_MODE,
    "FIXED_POSITION_SIZE": FIXED_POSITION_SIZE,
    "MAX_CONCURRENT_POSITIONS": MAX_CONCURRENT_POSITIONS,
    "DAILY_LOSS_STAKES": DAILY_LOSS_STAKES,
    "MAX_TOTAL_EXPOSURE_FRACTION": MAX_TOTAL_EXPOSURE_FRACTION,
    "ENABLE_STOP_LOSS": ENABLE_STOP_LOSS,
    "STOP_LOSS_PCT": STOP_LOSS_PCT,
    "TAKE_PROFIT_PRICE": TAKE_PROFIT_PRICE,
}


def setting(key):
    """Current value of a managed setting. Thread-safe: the Flask thread writes
    via apply_runtime_overrides while the bot thread reads mid-cycle. A single
    decision always sees one consistent value; the next decision sees the new
    one — that is the intended semantics of a live-tunable knob."""
    with _RUNTIME_LOCK:
        return _RUNTIME[key]


def paper_mode():
    """Whether trades are simulated right now.

    Every site that decides whether REAL money moves must read this, never
    `from config import PAPER_MODE` — an import-time copy would keep a running
    process trading live after the dashboard switched it back to paper (and
    vice versa), which is the worst possible way for this particular flag to
    go stale. The module-level PAPER_MODE constant survives only as the boot
    default that seeds this store."""
    with _RUNTIME_LOCK:
        return _RUNTIME["PAPER_MODE"]


def apply_runtime_overrides(values):
    """Swap new typed values into the live store. Called by the settings POST
    after the DB write succeeds, so memory and disk cannot disagree for longer
    than the gap between the two calls. Unknown keys are refused loudly —
    silently accepting one would fake success for a knob that nothing reads."""
    bad = [k for k in values if k not in _RUNTIME]
    if bad:
        raise KeyError(f"not runtime-tunable: {', '.join(sorted(bad))}")
    with _RUNTIME_LOCK:
        _RUNTIME.update(values)


def effective_stake():
    """What a trade will actually stake. In flat-stake mode this IS the stake:
    there is no second knob that can quietly reduce it."""
    with _RUNTIME_LOCK:
        return _RUNTIME["FIXED_POSITION_SIZE"]


def daily_loss_limit():
    """The circuit breaker's dollar threshold, DERIVED at check time:
    -(effective stake × DAILY_LOSS_STAKES). Change the stake and the limit
    scales with it — a fixed dollar limit tuned for $2 stakes would otherwise
    halt a $6-stake day after barely one loss."""
    with _RUNTIME_LOCK:
        stake = _RUNTIME["FIXED_POSITION_SIZE"]
        return -(stake * _RUNTIME["DAILY_LOSS_STAKES"])


# --- Stale-environment guard ---------------------------------------------
# Three constants changed MEANING on 2026-07-31 without changing NAME, so a
# stale value from an older .env or Fly secret parses fine and silently
# reinstates the behaviour the change removed. Renaming was the alternative and
# was used where the units changed (MAX_MODEL_SPREAD -> MAX_MODEL_SPREAD_STD,
# SHADOW_MAX_SPREAD -> SHADOW_MAX_SPREAD_STD); for these three the name is still
# right and only the fitted range moved, so they get a boot-time range check
# instead. Checked at startup via weather.validate_config_tables().
#
# The ranges are deliberately loose. They exist to catch the SPECIFIC previous
# value, not to police tuning.
def validate_env_ranges():
    """Return a list of stale-looking config values; empty means clean."""
    problems = []

    if not -0.5 <= METAR_WARM_CORRECTION_F <= 0.5:
        problems.append(
            f"METAR_WARM_CORRECTION_F={METAR_WARM_CORRECTION_F} is outside "
            f"[-0.5, 0.5]. The pre-2026-07-31 value was +1.3, fitted when the "
            f"ensemble ran cold against the paying ruler; it no longer does, and "
            f"restoring it reinstates the largest single bias in the pipeline "
            f"(it degraded low-side MAE from 1.61°F to 2.30°F). A per-station, "
            f"per-direction correction is the supported replacement — not a "
            f"global shift."
        )

    for hours, value in sorted(BASE_FORECAST_ERROR.items()):
        if not 0.3 <= value <= 1.5:
            problems.append(
                f"BASE_FORECAST_ERROR[{hours}h]={value} is outside [0.3, 1.5]. "
                f"This is no longer a standalone sigma — since 2026-07-31 it is "
                f"the INTERCEPT of |error| = a + b*weighted_spread_std, scaled "
                f"per direction by SIGMA_SCALE_*. The old sigma-valued ramp "
                f"(1.0/1.5/2.0/2.5) parses fine here and silently inflates every "
                f"forecast's uncertainty."
            )
    if BASE_FORECAST_ERROR:
        lo, hi = min(BASE_FORECAST_ERROR.values()), max(BASE_FORECAST_ERROR.values())
        if lo > 0 and hi / lo > 1.5:
            problems.append(
                f"BASE_FORECAST_ERROR ramps {lo}->{hi} ({hi/lo:.1f}x across the "
                f"lead-time table). It was flattened on 2026-07-31 because error "
                f"is flat in lead time in this bot's own record "
                f"(Spearman +0.105, n=27); a steep ramp is the old NWS-skill-decay "
                f"shape and should not be restored without a re-fit."
            )

    if NARROW_BUCKET_EDGE_THRESHOLD < EDGE_THRESHOLD:
        problems.append(
            f"NARROW_BUCKET_EDGE_THRESHOLD={NARROW_BUCKET_EDGE_THRESHOLD} is BELOW "
            f"EDGE_THRESHOLD={EDGE_THRESHOLD}. The narrow-bucket gate exists to be "
            f"STRICTER than the general one — inverting it makes thin, structurally "
            f"disadvantaged buckets the easiest markets to enter, which is the "
            f"opposite of the guard's purpose. Lowering it to equality effectively "
            f"abolishes the separate regime; going below it is almost certainly a typo."
        )

    if not 0.5 <= MIN_MODEL_CONFIDENCE <= 0.99:
        problems.append(
            f"MIN_MODEL_CONFIDENCE={MIN_MODEL_CONFIDENCE} is outside [0.5, 0.99]."
        )

    if not 0.5 <= MAX_MODEL_CONFIDENCE <= 0.99:
        problems.append(
            f"MAX_MODEL_CONFIDENCE={MAX_MODEL_CONFIDENCE} is outside [0.5, 0.99]."
        )

    if MIN_MODEL_CONFIDENCE >= MAX_MODEL_CONFIDENCE:
        problems.append(
            f"MIN_MODEL_CONFIDENCE={MIN_MODEL_CONFIDENCE} is >= MAX_MODEL_CONFIDENCE={MAX_MODEL_CONFIDENCE}."
        )

    if not 0.1 <= MIN_ENTRY_PRICE <= 0.95:
        problems.append(
            f"MIN_ENTRY_PRICE={MIN_ENTRY_PRICE} is outside [0.1, 0.95]."
        )

    if not 0.5 < MAX_ENTRY_PRICE <= 0.95:
        problems.append(
            f"MAX_ENTRY_PRICE={MAX_ENTRY_PRICE} is outside (0.5, 0.95]. 1.00 is "
            f"the DISABLED sentinel used between 2026-07-28 and 2026-07-31, and "
            f"it disarms the gate entirely — the 0.80-0.90 fill band is the only "
            f"losing band in the whole book (-$3.55 over 10 trades). Set it "
            f"deliberately or leave it at its default."
        )

    if MIN_ENTRY_PRICE >= MAX_ENTRY_PRICE:
        problems.append(
            f"MIN_ENTRY_PRICE={MIN_ENTRY_PRICE} is >= MAX_ENTRY_PRICE={MAX_ENTRY_PRICE}."
        )

    if not 1.0 <= MAX_HOURS_TO_RESOLUTION <= 168.0:
        problems.append(
            f"MAX_HOURS_TO_RESOLUTION={MAX_HOURS_TO_RESOLUTION} is outside [1.0, 168.0]."
        )

    # --- Independent veto gate ---
    # The gate ships ARMED by owner decision. A stale env value must not be able
    # to silently disarm it, or to tighten it into the MAX_MODEL_SPREAD_STD
    # failure mode where a correct-in-intent gate rejects most of the book.
    if not INDEPENDENT_VETO_ENABLED:
        problems.append(
            "INDEPENDENT_VETO_ENABLED is false. The gate was armed at deploy by "
            "owner decision 2026-08-05 and there is no shadow period, so a false "
            "here is either a deliberate re-decision or a stale env value that "
            "has silently removed the only check on wrong-station, stale-run and "
            "misparsed-bucket blunders. Note the automatic tripwire disables the "
            "gate at RUNTIME without touching this constant — if the gate turned "
            "itself off, fix the cause, don't record it here."
        )

    if not 3.0 <= DISAGREEMENT_VETO_F <= 15.0:
        problems.append(
            f"DISAGREEMENT_VETO_F={DISAGREEMENT_VETO_F} is outside [3.0, 15.0]. "
            f"It is sized at ~2x the measured MAE of 2.59°F to catch a wrong "
            f"station or a stale run, not a marginal forecast difference. Below "
            f"3.0 it starts refusing ordinary forecast disagreement and will "
            f"stop the book; above 15.0 nothing short of a coordinate typo can "
            f"trip it and the gate is decorative."
        )

    if not 0.5 <= PLAUSIBLE_BAND_F <= 6.0:
        problems.append(
            f"PLAUSIBLE_BAND_F={PLAUSIBLE_BAND_F} is outside [0.5, 6.0]. This is "
            f"a half-width: 6.0 makes a 12°F-wide band live on every bucket and "
            f"refuses nearly every NO bet, which is indistinguishable from "
            f"switching the bot off."
        )

    if not 0.05 <= INDEPENDENT_VETO_MAX_FIRE_RATE <= 1.0:
        problems.append(
            f"INDEPENDENT_VETO_MAX_FIRE_RATE={INDEPENDENT_VETO_MAX_FIRE_RATE} is "
            f"outside [0.05, 1.0]. This is the auto-disable tripwire that stands "
            f"in for the shadow period the owner decision skipped; a value of 1.0 "
            f"can never fire and removes that mitigation entirely."
        )

    if MIN_DEPTH_MULTIPLE < 1.0:
        problems.append(
            f"MIN_DEPTH_MULTIPLE={MIN_DEPTH_MULTIPLE} is below 1.0, which permits "
            f"an order larger than the entire usable book. The 2026-08-06 Austin "
            f"fill took ~23% of all resting depth and paid 0.9818 on a 0.64 quote; "
            f"the default 10.0 requires the stake to be a tenth of what is resting."
        )
    if not 0.0 < MAX_FILL_SLIPPAGE_ALERT <= 0.5:
        problems.append(
            f"MAX_FILL_SLIPPAGE_ALERT={MAX_FILL_SLIPPAGE_ALERT} is outside (0, 0.5]. "
            f"It is a PRICE difference, not a temperature — 0.03 means three cents."
        )
    if not REQUIRE_DEPTH_TO_TRADE:
        problems.append(
            "REQUIRE_DEPTH_TO_TRADE is false: an unreadable book will no longer "
            "refuse entry. That is the exact condition under which a taker order "
            "does the most damage. Set it deliberately or leave it on."
        )
    if not USE_MARKETABLE_LIMIT:
        problems.append(
            "USE_MARKETABLE_LIMIT is false: entries will use MARKET orders, which "
            "have no floor on execution quality in a thin book and cannot be "
            "constrained by MAX_ENTRY_PRICE. This is how the 2026-08-06 Austin "
            "fill happened."
        )

    return problems


# --- Replay-log identity --------------------------------------------------
# Bump when the replay_signals / replay_gates column set changes meaning, so a
# replay can refuse rows it cannot interpret rather than misreading them.
#   1  through 2026-08-05
#   2  independent-veto columns (independent_source/_state/_value/_fetched_at/
#      _detail, disagreement_f, veto_gross, veto_band, vetoed) and the two
#      independent_* gate rows. Rows at version 1 have no veto counterfactual —
#      which is NOT the same as a veto that did not fire, and a replay must not
#      read the NULLs as zeros.
REPLAY_SCHEMA_VERSION = 2

# --- Forecast-pipeline identity -------------------------------------------
# Bump whenever what the models are ASKED FOR changes: the endpoint, the
# aggregation, the member list, or any conditioning applied to the distribution
# before a bucket is priced.
#
# The fingerprint below covers constants, and constants alone. Every phase in
# the current rollout changes forecast INPUTS while touching no constant at all
# — the hourly migration, the added ensemble members, the intraday conditioning
# — so without this the replay log would carry an identical fingerprint across a
# boundary where the inputs changed completely, and a later calibration would
# silently pool the two. That is precisely the failure the fingerprint column
# was added to prevent, arriving through a door it did not cover.
#
#   1  daily=temperature_2m_max / _min, 4-member regional blends (through
#      2026-08-05)
#   2  hourly=temperature_2m aggregated locally to the audited settlement
#      window; both directions from one request (Phase 1.2)
#   3  intraday observation conditioning: hard bound at the observed extreme
#      plus a fitted remaining-rise term (Phase 1.3). The largest behavioural
#      change in this rollout — probabilities move materially, so no
#      calibration may pool across this boundary.
#   4  family weight caps, and agreement/spread computed across families
#      rather than across members (Phase 2.1)
FORECAST_PIPELINE_VERSION = 4

# --- Settlement-ruler identity --------------------------------------------
# Bump when what counts as the SETTLED VALUE changes. Distinct from the pipeline
# version above, which is about what is believed: this is about the ruler the
# belief is scored against, and a change here invalidates comparisons in a
# different and arguably worse way. A forecast change splits the history into
# "before" and "after"; a ruler change makes the OUTCOMES themselves
# incomparable, so a Brier score pooled across the boundary is measuring two
# different questions.
#
#   1  every city rounded to a whole °C (through 2026-08-05)
#   2  per-city settlement lattice — whole °F for the eleven North American
#      cities whose stations report °F, whole °C elsewhere (Phase 1.4). US
#      resolutions before and after this differ by up to 0.9°F.
SETTLEMENT_RULER_VERSION = 2

# Every constant that can change a probability or a gate outcome. The
# fingerprint over these is stored on each logged signal, because a replay that
# cannot tell which configuration produced a row is a replay that will silently
# mix two. MODEL_BIAS_CORRECTIONS changed twice on 2026-07-31 with no record;
# the whole point of this column is that such a change can never again be
# invisible after the fact.
#
# Deliberately NOT included: sizing, risk, retention, discovery, alerting. Those
# change what is bought, not what is believed, and including them would churn
# the fingerprint for changes a replay does not care about.
_FINGERPRINT_KEYS = (
    "FORECAST_PIPELINE_VERSION", "SETTLEMENT_RULER_VERSION",
    "EDGE_THRESHOLD", "MIN_MODEL_AGREEMENT", "MAX_MODEL_SPREAD_STD",
    "MIN_MODEL_COUNT", "MAX_ENTRY_SPREAD_FRACTION", "MAX_ENTRY_PRICE",
    "FORECAST_MARGIN_F", "YES_MARGIN_WIDTH_FRACTION",
    "NARROW_BUCKET_WIDTH_F", "NARROW_BUCKET_EDGE_THRESHOLD",
    "NARROW_BUCKET_STD_INFLATION",
    "CONVECTIVE_STD_INFLATION", "CONVECTIVE_CITIES",
    "ENABLE_PROB_CALIBRATION", "PROB_CALIBRATION_INTERCEPT",
    "PROB_CALIBRATION_SLOPE", "MIN_BUCKET_PROB",
    "BASE_FORECAST_ERROR", "SIGMA_SPREAD_COEF", "SIGMA_SCALE_HIGH",
    "SIGMA_SCALE_LOW", "MIN_SIGMA_F", "MAX_SIGMA_F", "SIGMA_STUDENT_T_DF",
    "METAR_WARM_CORRECTION_F", "GFS_BIAS_CORRECTIONS", "MODEL_BIAS_CORRECTIONS",
    "TAKER_FEE_RATE", "SLIPPAGE_FRACTION",
    # Family grouping changes both gated statistics and the blend weights, so
    # it changes what is believed — the fingerprint's stated criterion.
    "ENABLE_FAMILY_WEIGHTING", "FAMILY_WEIGHT_CAP", "GATE_ACROSS_FAMILIES",
    # Intraday conditioning moves probabilities materially.
    "ENABLE_INTRADAY_CONDITIONING", "INTRADAY_MIN_HOURS_ELAPSED",
    "INTRADAY_SIGMA_FLOOR_F",
    # The independent veto changes what is BOUGHT (it refuses trades) and what
    # is believed about a bucket (the band condition is a plausibility claim).
    # The fingerprint changes on the deploy that arms it, which SPLITS SIGNAL
    # HISTORY at that boundary — intended, and recorded in the report, because a
    # calibration must never pool vetoed and un-vetoed regimes.
    "INDEPENDENT_VETO_ENABLED", "DISAGREEMENT_VETO_F", "PLAUSIBLE_BAND_F",
    "INDEPENDENT_VETO_MAX_FIRE_RATE",
    # Execution safety & entry filters. These change which trades are ENTERABLE
    # and what the edge is net of real execution cost.
    "MIN_DEPTH_MULTIPLE", "REQUIRE_DEPTH_TO_TRADE", "USE_MARKETABLE_LIMIT",
    "MIN_MODEL_CONFIDENCE", "MAX_MODEL_CONFIDENCE", "MIN_ENTRY_PRICE", "MAX_HOURS_TO_RESOLUTION",
)


def config_fingerprint():
    """Stable short hash over every probability- and gate-affecting constant.

    Sorted and canonicalised so it depends on VALUES, not on dict ordering or
    float repr drift. Returned as 16 hex chars — long enough that a collision
    between two real configurations is not a practical concern, short enough to
    eyeball in a query result."""
    import hashlib
    g = globals()

    def canon(v):
        if isinstance(v, dict):
            return sorted((canon(k), canon(x)) for k, x in v.items())
        if isinstance(v, (set, frozenset)):
            return sorted(canon(x) for x in v)
        if isinstance(v, tuple):
            return [canon(x) for x in v]
        if isinstance(v, float):
            return f"{v:.10g}"
        return str(v)

    payload = repr([(k, canon(g[k])) for k in sorted(_FINGERPRINT_KEYS) if k in g])
    return hashlib.sha256(payload.encode()).hexdigest()[:16]
