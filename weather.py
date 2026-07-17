import math
import json
import logging
from db import execute_query
import time as _time
from config import (
    OPEN_METEO_URL, BASE_FORECAST_ERROR,
    MIN_MODEL_COUNT, CONVECTIVE_STD_INFLATION, CONVECTIVE_CITIES,
    GFS_BIAS_CORRECTIONS, MODEL_BIAS_CORRECTIONS,
    ENABLE_PROB_CALIBRATION, PROB_CALIBRATION_INTERCEPT, PROB_CALIBRATION_SLOPE,
    METAR_WARM_CORRECTION_F, MIN_BUCKET_PROB,
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

# Coordinates are the EXACT airport/station Polymarket names as each market's
# resolution source (verified 2026-07-04 from every live market's description text:
# "recorded at the <STATION>"). Matching the resolver's station is critical — a
# wrong station is a systematic forecast error no model quality can fix. Two prior
# "fixes" were WRONG and are reverted here: Seoul resolves on INCHEON (not the city
# centre) and London on LONDON CITY AIRPORT (not Heathrow). "region" selects the
# model-weight blend and is independent of the exact coordinate.
STATIONS = {
    # North America
    "NYC": {"lat": 40.7772, "lon": -73.8726, "region": "US"},          # LaGuardia (KLGA)
    "New York": {"lat": 40.7772, "lon": -73.8726, "region": "US"},     # LaGuardia (KLGA)
    "Chicago": {"lat": 41.9742, "lon": -87.9073, "region": "US"},      # O'Hare (KORD)
    "Miami": {"lat": 25.7932, "lon": -80.2906, "region": "US"},        # Miami Intl (KMIA)
    "Dallas": {"lat": 32.8471, "lon": -96.8518, "region": "US"},       # Love Field (KDAL)
    "Los Angeles": {"lat": 33.9416, "lon": -118.4085, "region": "US"}, # LAX (KLAX)
    "San Francisco": {"lat": 37.6213, "lon": -122.3790, "region": "US"}, # SFO (KSFO)
    "Austin": {"lat": 30.1975, "lon": -97.6664, "region": "US"},       # Austin-Bergstrom (KAUS)
    "Houston": {"lat": 29.6454, "lon": -95.2789, "region": "US"},      # Hobby (KHOU)
    "Seattle": {"lat": 47.4502, "lon": -122.3088, "region": "US"},     # Sea-Tac (KSEA)
    "Denver": {"lat": 39.7017, "lon": -104.7527, "region": "US"},      # Buckley SFB (KBKF)
    "Atlanta": {"lat": 33.6407, "lon": -84.4277, "region": "US"},      # Hartsfield (KATL)
    "Toronto": {"lat": 43.6777, "lon": -79.6248, "region": "US"},      # Pearson (CYYZ)
    "Mexico City": {"lat": 19.4363, "lon": -99.0721, "region": "US"},  # Benito Juárez (MMMX)
    "Panama": {"lat": 8.9733, "lon": -79.5556, "region": "US"},        # Marcos A. Gelabert (MPMG)
    # South America (GFS unavailable here — GLOBAL blend, no GFS)
    "Buenos Aires": {"lat": -34.8222, "lon": -58.5358, "region": "GLOBAL"}, # Ezeiza/Pistarini (SAEZ)
    "Sao Paulo": {"lat": -23.4356, "lon": -46.4731, "region": "GLOBAL"},    # Guarulhos (SBGR)
    # Europe
    "London": {"lat": 51.5048, "lon": 0.0495, "region": "EU"},         # London City Airport (EGLC) — NOT Heathrow
    "Paris": {"lat": 48.9694, "lon": 2.4414, "region": "EU"},          # Le Bourget (LFPB)
    "Berlin": {"lat": 52.3667, "lon": 13.5033, "region": "EU"},        # BER (EDDB)
    "Amsterdam": {"lat": 52.3105, "lon": 4.7683, "region": "EU"},      # Schiphol (EHAM)
    "Helsinki": {"lat": 60.3172, "lon": 24.9633, "region": "EU"},      # Vantaa (EFHK)
    "Istanbul": {"lat": 41.2753, "lon": 28.7519, "region": "EU"},      # Istanbul Airport (LTFM)
    "Madrid": {"lat": 40.4936, "lon": -3.5668, "region": "EU"},        # Barajas (LEMD)
    "Milan": {"lat": 45.6306, "lon": 8.7231, "region": "EU"},          # Malpensa (LIMC)
    "Moscow": {"lat": 55.4088, "lon": 37.9063, "region": "EU"},        # Domodedovo (UUDD)
    "Munich": {"lat": 48.3537, "lon": 11.7750, "region": "EU"},        # Munich (EDDM)
    "Warsaw": {"lat": 52.1657, "lon": 20.9671, "region": "EU"},        # Chopin (EPWA)
    # Middle East / Africa (GFS unavailable — GLOBAL blend, no GFS).
    # Ankara stays EU: ecmwf_ifs025 + gfs_global both return data there.
    "Tel Aviv": {"lat": 32.0114, "lon": 34.8867, "region": "GLOBAL"},  # Ben Gurion (LLBG)
    "Ankara": {"lat": 40.1281, "lon": 32.9951, "region": "EU"},        # Esenboğa (LTAC)
    "Jeddah": {"lat": 21.6796, "lon": 39.1566, "region": "GLOBAL"},    # King Abdulaziz (OEJN)
    "Lagos": {"lat": 6.5774, "lon": 3.3212, "region": "GLOBAL"},       # Murtala Muhammed (DNMM)
    "Cape Town": {"lat": -33.9648, "lon": 18.6017, "region": "GLOBAL"}, # Cape Town Intl (FACT)
    # Asia-Pacific
    "Tokyo": {"lat": 35.5523, "lon": 139.7798, "region": "AP"},        # Haneda (RJTT)
    "Hong Kong": {"lat": 22.3080, "lon": 113.9185, "region": "AP"},    # HKG (VHHH)
    "Seoul": {"lat": 37.4602, "lon": 126.4407, "region": "AP"},        # INCHEON (RKSI) — NOT the city centre
    "Shanghai": {"lat": 31.1443, "lon": 121.8083, "region": "AP"},     # Pudong (ZSPD)
    "Beijing": {"lat": 40.0799, "lon": 116.5847, "region": "AP"},      # Capital (ZBAA)
    "Guangzhou": {"lat": 23.3924, "lon": 113.2988, "region": "AP"},    # Baiyun (ZGGG)
    "Shenzhen": {"lat": 22.6393, "lon": 113.8107, "region": "AP"},     # Bao'an (ZGSZ)
    "Chengdu": {"lat": 30.5785, "lon": 103.9469, "region": "AP"},      # Shuangliu (ZUUU)
    "Chongqing": {"lat": 29.7192, "lon": 106.6417, "region": "AP"},    # Jiangbei (ZUCK)
    "Wuhan": {"lat": 30.7838, "lon": 114.2081, "region": "AP"},        # Tianhe (ZHHH)
    "Qingdao": {"lat": 36.3319, "lon": 120.3742, "region": "AP"},      # Jiaodong (ZSQD)
    "Busan": {"lat": 35.1795, "lon": 128.9380, "region": "AP"},        # Gimhae (RKPK)
    "Taipei": {"lat": 25.0694, "lon": 121.5525, "region": "AP"},       # Songshan (RCSS)
    "Singapore": {"lat": 1.3644, "lon": 103.9915, "region": "AP"},     # Changi (WSSS)
    "Kuala Lumpur": {"lat": 2.7456, "lon": 101.7072, "region": "AP"},  # KLIA (WMKK)
    "Jakarta": {"lat": -6.1256, "lon": 106.6559, "region": "AP"},      # Soekarno-Hatta (WIII)
    "Manila": {"lat": 14.5086, "lon": 121.0197, "region": "AP"},       # NAIA (RPLL)
    "Karachi": {"lat": 24.8936, "lon": 66.9385, "region": "AP"},       # Masroor Airbase (OPMR)
    "Lucknow": {"lat": 26.7606, "lon": 80.8893, "region": "AP"},       # CCS Intl (VILK)
    "Wellington": {"lat": -41.3272, "lon": 174.8053, "region": "AP"},  # Wellington Intl (NZWN)
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

                # Global per-model cold-bias correction (from calibrate.py verification,
                # not city-specific). MODEL_BIAS_CORRECTIONS values are the model's
                # measured cold bias magnitude, so adding it shifts the forecast warmer.
                if model in MODEL_BIAS_CORRECTIONS:
                    model_correction = MODEL_BIAS_CORRECTIONS[model]
                    val += model_correction
                    logging.debug(f"Model bias correction ({model_correction:+.1f}°F) for {model}")

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

    raw_weighted_mean = sum(
        temp * (weights[m] / total_weight)
        for m, temp in model_temps.items()
        if m in weights
    )
    # Shift toward the METAR resolution source Polymarket settles on (runs warmer
    # than Open-Meteo). Applied to the mean only — it moves where the distribution
    # is centred without touching model spread/agreement.
    weighted_mean = raw_weighted_mean + METAR_WARM_CORRECTION_F

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

    # Agreement is measured against the RAW consensus, not the bias-shifted mean:
    # no model temp contains METAR_WARM_CORRECTION_F, so comparing to the shifted
    # mean made the band asymmetric (only 2.0-correction °F of warm-side tolerance)
    # and could fail a perfectly agreeing ensemble.
    within_threshold = sum(
        1 for t in model_temps.values()
        if abs(t - raw_weighted_mean) < 2.0
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
        "raw_weighted_mean": raw_weighted_mean,
        "city_key": city_key,
        "model_spread": model_spread,
        "lead_time_hours": hours_to_resolution,
        "model_count": len(model_temps),
        "convective_inflated": convective_inflated,
    }

def _calibrate_prob(p):
    """Platt-scale the raw Gaussian bucket probability onto the empirically observed
    hit-rate curve. The raw normal-CDF prob is ~1.9x overconfident in the low-p region
    where the bot bets (measured on 96,307 resolved signals); this remap pulls it back
    onto the reliability curve so the edge calculation is honest. Monotonic, so it never
    reorders opportunities — it only rescales confidence. Identity if disabled."""
    if not ENABLE_PROB_CALIBRATION:
        return p
    # clamp away from 0/1 for the logit; degenerate probs pass through
    if p <= 0.0 or p >= 1.0:
        return p
    # Only correct the low-probability region (p < 0.5). That is where the reliability
    # data is dense (tens of thousands of narrow-bucket signals) and where every NO bet
    # lives; the high bins are sparse (n<100) and calibrating a wide, already-likely
    # bucket up toward 1.0 on that thin evidence would over-inflate it. Blend smoothly
    # to identity as p approaches 0.5 so there's no discontinuity at the boundary.
    if p >= 0.5:
        return p
    eps = 1e-4
    pc = min(max(p, eps), 1.0 - eps)
    logit = math.log(pc / (1.0 - pc))
    z = PROB_CALIBRATION_INTERCEPT + PROB_CALIBRATION_SLOPE * logit
    cal = 1.0 / (1.0 + math.exp(-z))
    # linear taper of the correction: full strength at p=0, fading to none at p=0.5,
    # keeping the function continuous and monotonic across the whole [0,1] range.
    w = (0.5 - p) / 0.5
    return p + w * (cal - p)


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
    prob = max(0.0, min(1.0, float(prob)))

    # Calibrate ONLY closed (bounded) buckets — exact-degree and narrow ranges. Those
    # are where the overconfidence lives (the model calls them ~15% but they hit ~28%)
    # and where every NO bet is placed. Open-ended above/below buckets are left raw:
    # they sit near 0.5 by construction and are self-consistent (P(above)+P(below)=1),
    # so a one-sided remap there would break that complementarity and is not supported
    # by the reliability data (which is dominated by narrow buckets).
    is_bounded = bucket_lower is not None and bucket_upper is not None
    if is_bounded:
        prob = _calibrate_prob(prob)
    # Tail floor on BOTH bounded and open-ended buckets. The overconfidence busts
    # (Guangzhou #31: P(YES)=0.00008 on a "34°C or higher" open-ended bucket) were
    # open-ended, so a bounded-only floor missed exactly the trades it was meant to
    # catch. Whole-°C resolution + forecast noise means no bucket is truly < ~5%
    # likely; flooring P(YES) up to MIN_BUCKET_PROB cuts the NO edge on the extreme
    # tail below the entry gate. The entry decision uses only the traded side's
    # probability, so flooring that side is sufficient; open-ended complementarity
    # isn't relied on downstream (each side is fetched independently).
    if MIN_BUCKET_PROB > 0.0 and prob < MIN_BUCKET_PROB:
        prob = MIN_BUCKET_PROB
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

        raw_weighted_mean = sum(
            temp * (weights[m] / total_weight)
            for m, temp in model_temps.items() if m in weights
        )
        weighted_mean = raw_weighted_mean + METAR_WARM_CORRECTION_F  # match METAR resolution source (see get_signal_engine)
        model_spread_std = float(_pstdev(list(model_temps.values())))

        base_error = _interpolate_base_error(opp.hours_to_resolution)

        combined_std = math.sqrt(base_error ** 2 + model_spread_std ** 2)

        convective_inflated = False
        if city_key in CONVECTIVE_CITIES:
            combined_std *= CONVECTIVE_STD_INFLATION
            convective_inflated = True

        # vs raw consensus, not the bias-shifted mean — see get_signal_engine
        within_threshold = sum(1 for t in model_temps.values() if abs(t - raw_weighted_mean) < 2.0)
        model_agreement = within_threshold / len(model_temps)
        model_spread = max(model_temps.values()) - min(model_temps.values())

        engine_cache[key] = {
            "ensemble_mean": weighted_mean,
            "ensemble_std": combined_std,
            "model_spread_std": model_spread_std,
            "base_error": base_error,
            "model_agreement": model_agreement,
            "raw_models": model_temps,
            "raw_weighted_mean": raw_weighted_mean,
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
