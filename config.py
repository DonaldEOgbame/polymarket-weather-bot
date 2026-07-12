"""
Centralized configuration for the Polymarket Weather Bot.
All tunable thresholds are loaded from environment variables with safe defaults.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# --- Mode ---
PAPER_MODE = os.getenv("PAPER_MODE", "true").lower() == "true"

# --- Database ---
DB_PATH = os.getenv("DB_PATH", "data/bot.db")

# --- Bankroll ---
STARTING_BANKROLL = float(os.getenv("STARTING_BANKROLL", "40.0"))

# --- Strategy Thresholds ---
EDGE_THRESHOLD = float(os.getenv("EDGE_THRESHOLD", "0.08"))
# Raised 0.6->0.75 by user preference 2026-07-10: with 3-4 models this only takes
# discrete values (e.g. 0.50/0.67/0.75/1.00 at n=4), so 0.75 means "at least 3 of 4
# models agree." Historical note: 9/12 trades so far sat at exactly 0.75 and the
# one loss was also at 0.75 (spread, not agreement, was the actual gap on that
# trade — see forecast_direction_agrees), so this isn't validated by outcome data
# as better than 0.6, it's a deliberate stricter stance pending more resolved trades.
MIN_MODEL_AGREEMENT = float(os.getenv("MIN_MODEL_AGREEMENT", "0.75"))
# 2.7°F = 1.5°C — maximum spread between model forecasts before trade is skipped
MAX_MODEL_SPREAD = float(os.getenv("MAX_MODEL_SPREAD", "2.7"))

# --- Transaction costs (subtracted from raw edge before the threshold check) ---
# Polymarket taker fee per share = TAKER_FEE_RATE * p * (1 - p), a bell curve that
# peaks at p=0.50 and ~vanishes near 0.01/0.99. Makers pay $0; Geopolitics/World
# markets are fee-free. Weather is the "Economics/Culture/Weather/Other" category
# at 0.05. As a fraction of notional this is feeRate*(1-p) — cheap on the high-priced
# NO tails the bot favours (~0.5-1%), expensive on cheap YES longshots (up to ~4%).
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

# --- Open-Meteo → METAR resolution-source correction (°F) ---
# Polymarket resolves off the airport METAR feed (via Wunderground), which was measured
# to run ~+0.72°C (~+1.3°F) WARMER than Open-Meteo's forecast at these stations across
# the first 19 traded station-days (14/19 positive; biggest gaps on the trades we lost:
# Hong Kong +2.6, Tokyo +2.5). Our forecasts were systematically too cold vs the ruler
# that actually pays out. Shift the ensemble mean warmer by this amount so probabilities
# target the METAR reading, not the ERA5 reanalysis. Re-fit from calibrate.py --source
# metar as more trades resolve; per-station corrections can later replace this global one.
METAR_WARM_CORRECTION_F = float(os.getenv("METAR_WARM_CORRECTION_F", "1.3"))

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
NARROW_BUCKET_EDGE_THRESHOLD = float(os.getenv("NARROW_BUCKET_EDGE_THRESHOLD", "0.20"))

# Std inflation multiplier applied to narrow buckets (≤ NARROW_BUCKET_WIDTH_F).
# Makes the probability estimate more conservative on thin windows.
NARROW_BUCKET_STD_INFLATION = float(os.getenv("NARROW_BUCKET_STD_INFLATION", "1.4"))

# Cities with high convective variability where afternoon storms cause large
# unpredictable temperature swings. Std is inflated by this multiplier.
CONVECTIVE_STD_INFLATION = float(os.getenv("CONVECTIVE_STD_INFLATION", "1.3"))
CONVECTIVE_CITIES = set(os.getenv("CONVECTIVE_CITIES", "Miami,Houston,Dallas,Atlanta,Tampa").split(","))

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
# Constants RE-FIT against the METAR resolution ruler (373 forecasts, 2026-07-04) — the
# feed Polymarket actually settles on. Against METAR the raw model is even more
# overconfident than it looked against ERA5 (predicted 5% → observed 19%; 15% → 29%),
# so the correcting curve is stronger: low slope compresses probabilities toward the
# realized base rate, reflecting that our forecast has less discriminating power against
# noisy whole-°C observations than the smooth reanalysis suggested. (Old ERA5 fit was
# INTERCEPT=1.1182 SLOPE=1.1619.) Re-fit from `calibrate.py --source metar` as data grows.
PROB_CALIBRATION_INTERCEPT = float(os.getenv("PROB_CALIBRATION_INTERCEPT", "-0.1715"))
PROB_CALIBRATION_SLOPE = float(os.getenv("PROB_CALIBRATION_SLOPE", "0.4457"))

# Base forecast uncertainty in °F, keyed by hours to resolution.
# Interpolated at runtime; values reflect NWS skill decay with lead time.
BASE_FORECAST_ERROR = {
    12:  float(os.getenv("BASE_FORECAST_ERROR_12H",  "1.0")),
    24:  float(os.getenv("BASE_FORECAST_ERROR_24H",  "1.5")),
    48:  float(os.getenv("BASE_FORECAST_ERROR_48H",  "2.0")),
    72:  float(os.getenv("BASE_FORECAST_ERROR_72H",  "2.5")),
}

# --- Risk / Sizing (measurement-week mode) ---
# Goal of this profile: maximise the NUMBER of small resolved trades per week so
# execution-cost and calibration estimates converge fast — NOT to deploy more
# capital per bet. Keep positions small; widen concurrency/exposure instead.
#
# HARD_MAX_POSITION_SIZE is a flat $2 while paper-testing on the current ~$20-30
# bankroll — this IS the binding constraint, not Kelly/fraction (every real trade
# has sized exactly $2). ONLY raise this (to ~$10, with DAILY_LOSS_LIMIT to ~-$40)
# once actually going LIVE with a $100-funded bankroll — do not scale it up while
# still in PAPER_MODE on the smaller balance, or paper positions size as if $100
# were already deployed when it isn't.
DAILY_LOSS_LIMIT = float(os.getenv("DAILY_LOSS_LIMIT", "-8.00"))
HARD_MAX_POSITION_SIZE = float(os.getenv("HARD_MAX_POSITION_SIZE", "2.0"))
MAX_POSITION_FRACTION = float(os.getenv("MAX_POSITION_FRACTION", "0.10"))
MAX_TOTAL_EXPOSURE_FRACTION = float(os.getenv("MAX_TOTAL_EXPOSURE_FRACTION", "0.70"))
MAX_CONCURRENT_POSITIONS = int(os.getenv("MAX_CONCURRENT_POSITIONS", "10"))
BASE_POSITION_FRACTION = float(os.getenv("BASE_POSITION_FRACTION", "0.05"))
KELLY_CAP = float(os.getenv("KELLY_CAP", "0.08"))
# Polymarket's real CLOB minimum order is ~$1; below this, live orders won't fill.
MIN_POSITION_SIZE = float(os.getenv("MIN_POSITION_SIZE", "1.00"))

STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "0.15"))
ENABLE_STOP_LOSS = os.getenv("ENABLE_STOP_LOSS", "false").lower() == "true"
EXIT_EDGE_FLOOR = float(os.getenv("EXIT_EDGE_FLOOR", "0.05"))
TAKE_PROFIT_PRICE = float(os.getenv("TAKE_PROFIT_PRICE", "0.98"))
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
MAX_HOURS_TO_RESOLUTION = float(os.getenv("MAX_HOURS_TO_RESOLUTION", "72"))

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
SHADOW_MAX_SPREAD = float(os.getenv("SHADOW_MAX_SPREAD", "5.0"))
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

# --- GFS warm-bias corrections (°F) per city ---
# GFS consistently runs warm in humid/coastal cities. Values derived from
# NWS MOS verification studies. Keyed by station name matching STATIONS dict.
GFS_BIAS_CORRECTIONS = {
    k: float(v) for k, v in (
        pair.split(":") for pair in
        os.getenv("GFS_BIAS_CORRECTIONS", "Miami:-1.5,Houston:-1.5,Dallas:-1.2,Atlanta:-1.0,Tampa:-1.3").split(",")
        if ":" in pair
    )
}

# --- Global per-model cold-bias corrections (°F), applied to every city ---
# Derived from calibrate.py's per-model signed bias (mean(model - actual) across
# resolved forecasts, n=38 as of 2026-07-01). Unlike GFS_BIAS_CORRECTIONS above,
# these run cold everywhere in the sample, not just specific cities, so they're
# applied globally rather than city-keyed. Re-run calibrate.py periodically and
# update these as more forecasts resolve — n=38 is a first read, not a settled value.
MODEL_BIAS_CORRECTIONS = {
    k: float(v) for k, v in (
        pair.split(":") for pair in
        os.getenv(
            "MODEL_BIAS_CORRECTIONS",
            "ecmwf_ifs025:0.29,icon_global:0.03,gem_global:1.32,jma_gsm:1.55"
        ).split(",")
        if ":" in pair
    )
}

# --- Data retention (days) ---
SIGNAL_RETENTION_DAYS = int(os.getenv("SIGNAL_RETENTION_DAYS", "60"))
SCAN_LOG_RETENTION_DAYS = int(os.getenv("SCAN_LOG_RETENTION_DAYS", "14"))
NOTIFICATION_RETENTION_DAYS = int(os.getenv("NOTIFICATION_RETENTION_DAYS", "30"))

# --- External APIs ---
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
GAMMA_API_URL = "https://gamma-api.polymarket.com/markets"
GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
CLOB_BASE_URL = "https://clob.polymarket.com"
