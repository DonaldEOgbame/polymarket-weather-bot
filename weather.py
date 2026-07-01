import math
import json
import logging
from db import execute_query
import time as _time
from config import (
    OPEN_METEO_URL, BASE_FORECAST_ERROR,
    MIN_MODEL_COUNT, CONVECTIVE_STD_INFLATION, CONVECTIVE_CITIES,
    GFS_BIAS_CORRECTIONS,
)

def _pstdev(data):
    """Calculate the population standard deviation of data (equivalent to np.std(data))."""
    n = len(data)
    if n == 0:
        return 0.0
    mean = sum(data) / n
    variance = sum((x - mean) ** 2 for x in data) / n
    return math.sqrt(variance)

def _norm_cdf(x, loc=0.0, scale=1.0):
    """Calculate the standard normal CDF (equivalent to norm.cdf(x, loc, scale))."""
    if scale <= 0.0:
        scale = 0.5  # safe clamp to match weather.py's minimum std logic
    return 0.5 * (1.0 + math.erf((x - loc) / (scale * math.sqrt(2.0))))

# Cross-scan in-memory forecast cache: {(city, is_high): (fetch_timestamp, result)}
# Avoids re-fetching Open-Meteo on every 10-minute scan cycle.
_FORECAST_CACHE: dict = {}
_FORECAST_TTL_SECONDS = 480  # 8 minutes — safe within a 10-min scan interval
from utils import get_session

STATIONS = {
    # North America
    "NYC": {"lat": 40.7769, "lon": -73.8740, "region": "US"},
    "New York": {"lat": 40.7769, "lon": -73.8740, "region": "US"},
    "Chicago": {"lat": 41.9742, "lon": -87.9073, "region": "US"},
    "Miami": {"lat": 25.7959, "lon": -80.2870, "region": "US"},
    "Dallas": {"lat": 32.8471, "lon": -96.8518, "region": "US"},
    "Los Angeles": {"lat": 34.0522, "lon": -118.2437, "region": "US"},
    "San Francisco": {"lat": 37.6213, "lon": -122.3790, "region": "US"},
    "Austin": {"lat": 30.1944, "lon": -97.6700, "region": "US"},
    "Houston": {"lat": 29.7604, "lon": -95.3698, "region": "US"},
    "Seattle": {"lat": 47.4502, "lon": -122.3088, "region": "US"},
    "Denver": {"lat": 39.8561, "lon": -104.6737, "region": "US"},
    "Atlanta": {"lat": 33.6407, "lon": -84.4277, "region": "US"},
    "Toronto": {"lat": 43.6777, "lon": -79.6248, "region": "US"},
    "Mexico City": {"lat": 19.4363, "lon": -99.0721, "region": "US"},
    "Panama": {"lat": 8.9824, "lon": -79.5199, "region": "US"},
    # South America (GFS unavailable here — GLOBAL blend, no GFS)
    "Buenos Aires": {"lat": -34.8222, "lon": -58.5358, "region": "GLOBAL"},
    "Sao Paulo": {"lat": -23.4356, "lon": -46.4731, "region": "GLOBAL"},
    # Europe
    "London": {"lat": 51.5053, "lon": 0.0553, "region": "EU"},
    "Paris": {"lat": 48.9694, "lon": 2.4414, "region": "EU"},
    "Berlin": {"lat": 52.3667, "lon": 13.5033, "region": "EU"},
    "Amsterdam": {"lat": 52.3105, "lon": 4.7683, "region": "EU"},
    "Helsinki": {"lat": 60.3172, "lon": 24.9633, "region": "EU"},
    "Istanbul": {"lat": 41.2622, "lon": 28.7278, "region": "EU"},
    "Madrid": {"lat": 40.4719, "lon": -3.5626, "region": "EU"},
    "Milan": {"lat": 45.6306, "lon": 8.7231, "region": "EU"},
    "Moscow": {"lat": 55.5915, "lon": 37.2615, "region": "EU"},
    "Munich": {"lat": 48.3537, "lon": 11.7750, "region": "EU"},
    "Warsaw": {"lat": 52.1657, "lon": 20.9671, "region": "EU"},
    # Middle East / Africa (GFS unavailable — GLOBAL blend, no GFS).
    # Ankara stays EU: ecmwf_ifs025 + gfs_global both return data there.
    "Tel Aviv": {"lat": 31.9980, "lon": 34.9067, "region": "GLOBAL"},
    "Ankara": {"lat": 40.1280, "lon": 32.9949, "region": "EU"},
    "Jeddah": {"lat": 21.6796, "lon": 39.1566, "region": "GLOBAL"},
    "Lagos": {"lat": 6.5774, "lon": 3.3212, "region": "GLOBAL"},
    "Cape Town": {"lat": -33.9648, "lon": 18.6017, "region": "GLOBAL"},
    # Asia-Pacific
    "Tokyo": {"lat": 35.5494, "lon": 139.7798, "region": "AP"},
    "Hong Kong": {"lat": 22.3080, "lon": 113.9185, "region": "AP"},
    "Seoul": {"lat": 37.4602, "lon": 126.4407, "region": "AP"},
    "Shanghai": {"lat": 31.1443, "lon": 121.8083, "region": "AP"},
    "Beijing": {"lat": 40.0799, "lon": 116.5847, "region": "AP"},
    "Guangzhou": {"lat": 23.3924, "lon": 113.2988, "region": "AP"},
    "Shenzhen": {"lat": 22.6393, "lon": 113.8107, "region": "AP"},
    "Chengdu": {"lat": 30.5785, "lon": 103.9469, "region": "AP"},
    "Chongqing": {"lat": 29.7192, "lon": 106.6517, "region": "AP"},
    "Wuhan": {"lat": 30.7835, "lon": 114.2084, "region": "AP"},
    "Qingdao": {"lat": 36.3619, "lon": 120.0883, "region": "AP"},
    "Busan": {"lat": 35.1795, "lon": 128.9380, "region": "AP"},
    "Taipei": {"lat": 25.0777, "lon": 121.5737, "region": "AP"},
    "Singapore": {"lat": 1.3644, "lon": 103.9915, "region": "AP"},
    "Kuala Lumpur": {"lat": 2.7456, "lon": 101.7072, "region": "AP"},
    "Jakarta": {"lat": -6.1256, "lon": 106.6559, "region": "AP"},
    "Manila": {"lat": 14.5086, "lon": 121.0197, "region": "AP"},
    "Karachi": {"lat": 24.9056, "lon": 67.1608, "region": "AP"},
    "Lucknow": {"lat": 26.7606, "lon": 80.8893, "region": "AP"},
    "Wellington": {"lat": -41.3272, "lon": 174.8053, "region": "AP"},
}

# Two rules for this table:
#   1. Every model id must actually return data from Open-Meteo. The old ids
#      `ecmwf_ifs04` and `gfs025` return null for ALL coordinates (verified
#      2026-06-28) — they silently dropped to <3 models everywhere except AP,
#      so US/EU never traded. Valid global ids: ecmwf_ifs025, gfs_global,
#      icon_global, gem_global, jma_gsm.
#   2. Never use Open-Meteo's "best_match" — it is not an independent model
#      (it's the auto-selected best available, usually ECMWF) so it double-counts
#      and corrupts the model_spread / model_agreement gates.
# GFS is unavailable in the Southern Hemisphere / Africa / Middle East, so those
# cities use the GLOBAL blend (no GFS). ECMWF leads everywhere — it's the
# highest-skill operational global model.
WEIGHTS = {
    "US":     {"ecmwf_ifs025": 0.40, "gfs_global": 0.30, "icon_global": 0.20, "gem_global": 0.10},
    "EU":     {"ecmwf_ifs025": 0.40, "icon_global": 0.30, "gfs_global": 0.20, "gem_global": 0.10},
    "AP":     {"ecmwf_ifs025": 0.35, "jma_gsm": 0.30, "icon_global": 0.20, "gem_global": 0.15},
    "GLOBAL": {"ecmwf_ifs025": 0.40, "icon_global": 0.25, "gem_global": 0.20, "jma_gsm": 0.15},
}

def get_station_coords(city_name):
    name_lower = city_name.lower()
    for k in sorted(STATIONS.keys(), key=len, reverse=True):
        if k.lower() in name_lower:
            return k, STATIONS[k]
    return None, None


def _interpolate_base_error(lead_hours):
    """Linear-interpolate base forecast error from BASE_FORECAST_ERROR table.
    Falls back to the worst (longest-lead) value if the table is empty or
    interpolation can't bracket the lead time — defensive but safe."""
    breakpoints = sorted(BASE_FORECAST_ERROR.keys())
    if not breakpoints:
        return 2.5  # conservative fallback
    if lead_hours <= breakpoints[0]:
        return BASE_FORECAST_ERROR[breakpoints[0]]
    if lead_hours >= breakpoints[-1]:
        return BASE_FORECAST_ERROR[breakpoints[-1]]
    for i in range(len(breakpoints) - 1):
        lo, hi = breakpoints[i], breakpoints[i + 1]
        if lo <= lead_hours <= hi:
            t = (lead_hours - lo) / (hi - lo)
            return BASE_FORECAST_ERROR[lo] + t * (BASE_FORECAST_ERROR[hi] - BASE_FORECAST_ERROR[lo])
    return BASE_FORECAST_ERROR[breakpoints[-1]]

def fetch_forecasts(city_name, is_high=True, force_refresh=False):
    city_key, station = get_station_coords(city_name)
    if not station:
        logging.warning(f"No station mapping found for {city_name}")
        return None

    cache_key = (city_key, is_high)
    if not force_refresh:
        cached = _FORECAST_CACHE.get(cache_key)
        if cached:
            age = _time.monotonic() - cached[0]
            if age < _FORECAST_TTL_SECONDS:
                logging.debug(f"Forecast cache hit for {city_key} is_high={is_high} (age={age:.0f}s)")
                return cached[1]

    region = station["region"]
    models = list(WEIGHTS[region].keys())
    
    params = {
        "latitude": station["lat"],
        "longitude": station["lon"],
        "daily": "temperature_2m_max" if is_high else "temperature_2m_min",
        "models": ",".join(models),
        "timezone": "auto",
        "temperature_unit": "fahrenheit",
        "forecast_days": 4
    }
    
    try:
        resp = get_session().get(OPEN_METEO_URL, params=params, timeout=6)
        if resp.status_code != 200:
            logging.error(f"Open-Meteo error ({resp.status_code}): {resp.text}")
            return None
    except Exception as e:
        logging.error(f"Open-Meteo request failed: {e}")
        return None
        
    data = resp.json()
    daily = data.get("daily", {})
    times = daily.get("time", [])
    
    forecasts_by_date = {}
    for i, date_str in enumerate(times):
        model_temps = {}
        for model in models:
            key = f"temperature_2m_{'max' if is_high else 'min'}_{model}"
            vals = daily.get(key, [])
            val = vals[i] if i < len(vals) else None
            
            if val is not None:
                # GFS warm bias correction (city-keyed, configured in config.py)
                if model == "gfs_global" and city_key in GFS_BIAS_CORRECTIONS:
                    correction = GFS_BIAS_CORRECTIONS[city_key]
                    val += correction
                    logging.debug(f"GFS bias correction ({correction:+.1f}°F) for {city_key}")
                
                model_temps[model] = val
        forecasts_by_date[date_str] = model_temps
        
    result = forecasts_by_date, city_key, region
    _FORECAST_CACHE[cache_key] = (_time.monotonic(), result)
    return result

def get_signal_engine(city_name, target_date, is_high=True, hours_to_resolution=48.0):
    res = fetch_forecasts(city_name, is_high)
    if not res:
        return None
    forecasts_by_date, city_key, region = res
    
    if target_date not in forecasts_by_date:
        logging.warning(f"Target date {target_date} not in forecast range")
        return None
        
    model_temps = forecasts_by_date[target_date]
    if not model_temps:
        return None
    
    if len(model_temps) < MIN_MODEL_COUNT:
        logging.warning(
            f"Only {len(model_temps)} model(s) available for {city_key} on {target_date}, "
            f"need >= {MIN_MODEL_COUNT} — skipping"
        )
        return None

    weights = WEIGHTS[region]
    total_weight = sum(weights[m] for m in model_temps if m in weights)
    if total_weight == 0:
        return None

    weighted_mean = sum(
        temp * (weights[m] / total_weight)
        for m, temp in model_temps.items()
        if m in weights
    )

    model_spread_std = float(_pstdev(list(model_temps.values())))

    base_error = _interpolate_base_error(hours_to_resolution)

    combined_std = math.sqrt(base_error ** 2 + model_spread_std ** 2)

    # Convective city inflation: afternoon storms in tropical/continental cities
    # introduce temperature swings that NWS-calibrated base errors don't capture.
    convective_inflated = False
    if city_key in CONVECTIVE_CITIES:
        combined_std *= CONVECTIVE_STD_INFLATION
        convective_inflated = True
        logging.debug(f"Convective std inflation x{CONVECTIVE_STD_INFLATION} applied for {city_key}")

    within_threshold = sum(
        1 for t in model_temps.values()
        if abs(t - weighted_mean) < 2.0
    )
    model_agreement = within_threshold / len(model_temps)
    model_spread = max(model_temps.values()) - min(model_temps.values())

    return {
        "ensemble_mean": weighted_mean,
        "ensemble_std": combined_std,
        "model_spread_std": model_spread_std,
        "base_error": base_error,
        "model_agreement": model_agreement,
        "raw_models": model_temps,
        "city_key": city_key,
        "model_spread": model_spread,
        "lead_time_hours": hours_to_resolution,
        "model_count": len(model_temps),
        "convective_inflated": convective_inflated,
    }

def get_bucket_probability(engine_result, bucket_lower, bucket_upper):
    mean = engine_result["ensemble_mean"]
    std = engine_result["ensemble_std"]
    
    std = max(std, 0.5)
    
    lb = bucket_lower if bucket_lower is not None else -1000.0
    ub = bucket_upper if bucket_upper is not None else 1000.0
    
    if bucket_lower is not None and bucket_upper is not None:
        if bucket_lower == bucket_upper:
            lb -= 0.5
            ub += 0.5
        else:
            lb -= 0.5
            ub += 0.5
    elif bucket_lower is not None and bucket_upper is None:
        lb -= 0.5
    elif bucket_upper is not None and bucket_lower is None:
        ub += 0.5

    prob = _norm_cdf(ub, loc=mean, scale=std) - _norm_cdf(lb, loc=mean, scale=std)
    return max(0.0, min(1.0, float(prob)))

def prefetch_signal_engines(opportunities) -> dict:
    """Fetch weather forecasts for all opportunities, minimising API calls.

    fetch_forecasts() already returns all dates in one HTTP request (forecast_days=4).
    So we deduplicate on (city, is_high) — one call per city — then fan the results
    back out to every (city, date, is_high) key the eval loop needs.

    Returns a dict keyed by (city_key, date, is_high) → engine_result (or None).
    """
    # Build the minimal set of HTTP calls needed
    city_is_high_keys = {(opp.city, opp.is_high) for opp in opportunities}

    # hours_to_resolution lookup: use the smallest value per city (most urgent)
    hours_map = {}
    for opp in opportunities:
        k = (opp.city, opp.is_high)
        if k not in hours_map or opp.hours_to_resolution < hours_map[k]:
            hours_map[k] = opp.hours_to_resolution

    def _fetch_city(city, is_high):
        res = fetch_forecasts(city, is_high)
        return (city, is_high), res

    # Fetch sequentially — Open-Meteo uses HTTP keep-alive so after the first
    # connection (~1-2s), each subsequent city takes ~0.5s. Parallel connections
    # from the same IP trigger timeouts and are slower overall.
    forecast_cache: dict[tuple, object] = {}
    for city, is_high in city_is_high_keys:
        try:
            _, res = _fetch_city(city, is_high)
            forecast_cache[(city, is_high)] = res
        except Exception as e:
            logging.error(f"prefetch failed for {city} is_high={is_high}: {e}")
            forecast_cache[(city, is_high)] = None

    # Now build the (city, date, is_high) engine cache the eval loop expects
    engine_cache: dict[tuple, object] = {}
    for opp in opportunities:
        key = (opp.city, opp.date, opp.is_high)
        if key in engine_cache:
            continue
        raw = forecast_cache.get((opp.city, opp.is_high))
        if raw is None:
            engine_cache[key] = None
            continue
        forecasts_by_date, city_key, region = raw

        if opp.date not in forecasts_by_date:
            engine_cache[key] = None
            continue

        model_temps = forecasts_by_date[opp.date]
        if not model_temps or len(model_temps) < MIN_MODEL_COUNT:
            if model_temps:
                logging.warning(
                    f"Only {len(model_temps)} model(s) available for {city_key} on {opp.date}, "
                    f"need >= {MIN_MODEL_COUNT} — skipping"
                )
            engine_cache[key] = None
            continue

        weights = WEIGHTS[region]
        total_weight = sum(weights[m] for m in model_temps if m in weights)
        if total_weight == 0:
            engine_cache[key] = None
            continue

        weighted_mean = sum(
            temp * (weights[m] / total_weight)
            for m, temp in model_temps.items() if m in weights
        )
        model_spread_std = float(_pstdev(list(model_temps.values())))

        base_error = _interpolate_base_error(opp.hours_to_resolution)

        combined_std = math.sqrt(base_error ** 2 + model_spread_std ** 2)

        convective_inflated = False
        if city_key in CONVECTIVE_CITIES:
            combined_std *= CONVECTIVE_STD_INFLATION
            convective_inflated = True

        within_threshold = sum(1 for t in model_temps.values() if abs(t - weighted_mean) < 2.0)
        model_agreement = within_threshold / len(model_temps)
        model_spread = max(model_temps.values()) - min(model_temps.values())

        engine_cache[key] = {
            "ensemble_mean": weighted_mean,
            "ensemble_std": combined_std,
            "model_spread_std": model_spread_std,
            "base_error": base_error,
            "model_agreement": model_agreement,
            "raw_models": model_temps,
            "city_key": city_key,
            "model_spread": model_spread,
            "lead_time_hours": opp.hours_to_resolution,
            "model_count": len(model_temps),
            "convective_inflated": convective_inflated,
        }

    hits = sum(1 for v in engine_cache.values() if v is not None)
    logging.info(
        f"Weather prefetch: {len(city_is_high_keys)} city fetches → "
        f"{hits}/{len(engine_cache)} opportunity keys populated"
    )
    return engine_cache


def log_model_accuracy(city, target_date, model, forecast_temp, actual_temp):
    execute_query('''
        INSERT INTO model_accuracy (city, target_date, model, forecast_temp, actual_temp)
        VALUES (?, ?, ?, ?, ?)
    ''', (city, target_date, model, forecast_temp, actual_temp))
