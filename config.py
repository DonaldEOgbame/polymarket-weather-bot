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
MIN_MODEL_AGREEMENT = float(os.getenv("MIN_MODEL_AGREEMENT", "0.6"))
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
# of the entry price. The measurement-week run exists to replace this estimate with
# the real slippage logged on actual fills.
SLIPPAGE_FRACTION = float(os.getenv("SLIPPAGE_FRACTION", "0.015"))

# Minimum number of weather models required to enter a trade.
# Two models that agree proves nothing — ECMWF dropping out silently leaves only 2.
MIN_MODEL_COUNT = int(os.getenv("MIN_MODEL_COUNT", "3"))

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

# --- Market Filters ---
MIN_VOLUME = float(os.getenv("MIN_VOLUME", "500"))
MAX_HOURS_TO_RESOLUTION = float(os.getenv("MAX_HOURS_TO_RESOLUTION", "72"))

# --- Market Discovery ---
# Markets per API page (Gamma API max is 100)
MARKET_DISCOVERY_LIMIT = int(os.getenv("MARKET_DISCOVERY_LIMIT", "100"))
# Maximum pages to fetch per scan cycle before giving up
MARKET_DISCOVERY_MAX_PAGES = int(os.getenv("MARKET_DISCOVERY_MAX_PAGES", "5"))
# Kept for backwards-compat but no longer used as primary stop — expiry detection stops pagination
MARKET_DISCOVERY_STOP_AFTER_WEATHER = int(os.getenv("MARKET_DISCOVERY_STOP_AFTER_WEATHER", "500"))
# Max markets sent to CLOB orderbook API per scan (scored by liquidity + price uncertainty)
MAX_CLOB_CANDIDATES = int(os.getenv("MAX_CLOB_CANDIDATES", "150"))
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

# --- Data retention (days) ---
SIGNAL_RETENTION_DAYS = int(os.getenv("SIGNAL_RETENTION_DAYS", "60"))
SCAN_LOG_RETENTION_DAYS = int(os.getenv("SCAN_LOG_RETENTION_DAYS", "14"))
NOTIFICATION_RETENTION_DAYS = int(os.getenv("NOTIFICATION_RETENTION_DAYS", "30"))

# --- External APIs ---
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
GAMMA_API_URL = "https://gamma-api.polymarket.com/markets"
GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
CLOB_BASE_URL = "https://clob.polymarket.com"
