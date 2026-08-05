import math
import json
import logging
from db import execute_query
import time as _time
from config import (
    OPEN_METEO_URL, BASE_FORECAST_ERROR,
    MIN_MODEL_COUNT, CONVECTIVE_STD_INFLATION, CONVECTIVE_CITIES,
    GFS_BIAS_CORRECTIONS, model_bias_correction,
    ENABLE_PROB_CALIBRATION, PROB_CALIBRATION_INTERCEPT, PROB_CALIBRATION_SLOPE,
    METAR_WARM_CORRECTION_F, MIN_BUCKET_PROB,
    SIGMA_SPREAD_COEF, SIGMA_SCALE_HIGH, SIGMA_SCALE_LOW, MIN_SIGMA_F, MAX_SIGMA_F,
    SIGMA_STUDENT_T_DF,
)

def _pstdev(data):
    """Calculate the population standard deviation of data (equivalent to np.std(data))."""
    n = len(data)
    if n == 0:
        return 0.0
    mean = sum(data) / n
    variance = sum((x - mean) ** 2 for x in data) / n
    return math.sqrt(variance)


def _weighted_pstdev(values_by_model, weights):
    """Weight-aware population standard deviation of the ensemble members.

    The unweighted version let a member carrying 5% of the blend move the spread
    gate as far as ECMWF carrying 40%. It is also the statistic the sigma
    coefficient is fitted against, so it must match what config.py was fitted on.
    Members absent from `weights` contribute nothing, matching how the weighted
    mean already treats them."""
    tw = sum(weights.get(m, 0.0) for m in values_by_model)
    if tw <= 0:
        return 0.0
    mean = sum(v * weights.get(m, 0.0) / tw for m, v in values_by_model.items())
    var = sum(weights.get(m, 0.0) / tw * (v - mean) ** 2 for m, v in values_by_model.items())
    return math.sqrt(max(var, 0.0))

def _norm_cdf(x, loc=0.0, scale=1.0):
    """Calculate the standard normal CDF (equivalent to norm.cdf(x, loc, scale))."""
    if scale <= 0.0:
        scale = 0.5  # safe clamp to match weather.py's minimum std logic
    return 0.5 * (1.0 + math.erf((x - loc) / (scale * math.sqrt(2.0))))


def _betainc(a, b, x):
    """Regularised incomplete beta I_x(a, b), by the standard continued fraction
    (Lentz). Only needed for the Student-t CDF below — stdlib has no equivalent
    and the project deliberately avoids a scipy dependency."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbeta = math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)
    front = math.exp(a * math.log(x) + b * math.log(1.0 - x) - lbeta) / a
    fv, c, d = 1.0, 1.0, 0.0
    for i in range(300):
        m = i // 2
        if i == 0:
            num = 1.0
        elif i % 2 == 0:
            num = (m * (b - m) * x) / ((a + 2 * m - 1) * (a + 2 * m))
        else:
            num = -((a + m) * (a + b + m) * x) / ((a + 2 * m) * (a + 2 * m + 1))
        d = 1.0 + num * d
        d = 1e-30 if abs(d) < 1e-30 else d
        d = 1.0 / d
        c = 1.0 + num / c
        c = 1e-30 if abs(c) < 1e-30 else c
        fv *= c * d
        if abs(1.0 - c * d) < 1e-10:
            break
    return front * (fv - 1.0)


def _student_t_cdf(x, loc=0.0, scale=1.0, df=4.0):
    """Student-t CDF, VARIANCE-MATCHED to `scale`.

    The t distribution's own variance is df/(df-2) times its scale parameter, so
    passing sigma straight in as the scale would silently widen the distribution
    on top of the fatter tails and make the fitted SIGMA_SCALE_* constants mean
    something different. Dividing first keeps Var = scale**2, so switching this
    kernel on changes tail SHAPE only — the fitted sigma still means what it
    meant. Falls back to Gaussian for df <= 2, where the variance is undefined."""
    if scale <= 0.0:
        scale = 0.5
    if df <= 2.0:
        return _norm_cdf(x, loc, scale)
    s = scale / math.sqrt(df / (df - 2.0))
    t = (x - loc) / s
    ib = _betainc(df / 2.0, 0.5, df / (df + t * t))
    return 0.5 * ib if t <= 0.0 else 1.0 - 0.5 * ib


def _bucket_cdf(x, loc, scale):
    """The distribution used to price buckets: Student-t when
    SIGMA_STUDENT_T_DF > 2, Gaussian otherwise."""
    if SIGMA_STUDENT_T_DF and SIGMA_STUDENT_T_DF > 2.0:
        return _student_t_cdf(x, loc, scale, SIGMA_STUDENT_T_DF)
    return _norm_cdf(x, loc, scale)


def compute_sigma_stages(spread_std, lead_hours, is_high, city_key=None):
    """Every intermediate value on the way to sigma, as a dict.

    Exists so the replay log can record WHERE a sigma came from, not just what
    it ended up as. A sigma of 8.0 tells you nothing; a sigma that was 5.6
    before the convective step and 8.0 after the clamp tells you which constant
    to look at."""
    base = _interpolate_base_error(lead_hours)
    post_spread = base + SIGMA_SPREAD_COEF * float(spread_std)
    k = SIGMA_SCALE_HIGH if is_high else SIGMA_SCALE_LOW
    post_direction = k * post_spread
    convective = bool(city_key and city_key in CONVECTIVE_CITIES)
    post_convective = post_direction * (CONVECTIVE_STD_INFLATION if convective else 1.0)
    post_clamp = min(max(post_convective, MIN_SIGMA_F), MAX_SIGMA_F)
    return {
        "base": base,
        "post_spread": post_spread,
        "direction_scale": k,
        "post_direction": post_direction,
        "convective": convective,
        "post_convective": post_convective,
        "post_clamp": post_clamp,
        "clamped": post_clamp != post_convective,
    }


def compute_sigma(spread_std, lead_hours, is_high, city_key=None):
    """Forecast sigma in °F.

        sigma = k_dir * (base(lead) + SIGMA_SPREAD_COEF * spread_std)

    Replaces sqrt(base**2 + spread_std**2), which buried the spread term: under
    quadrature a wide ensemble moved sigma by tenths of a degree, where the
    measured relationship is roughly linear with a slope above 3. See the fit
    notes on SIGMA_SPREAD_COEF and SIGMA_SCALE_* in config.py.

    `spread_std` must be the WEIGHTED standard deviation of the members, not
    max-min: the coefficient is fitted against the former precisely because it
    does not drift as the ensemble grows.

    Thin wrapper over compute_sigma_stages so there is exactly one definition of
    the formula — the replay log and the trading decision cannot disagree."""
    return compute_sigma_stages(spread_std, lead_hours, is_high, city_key)["post_clamp"]


def _diurnal_range(model_temps, opposite_temps, weights, is_high):
    """Weighted-mean max minus weighted-mean min, in °F, or None.

    Both means use the SAME weights as the traded forecast, so the range is
    consistent with the mean it will be added to. Only members present in both
    directions contribute — a member that returns a max but no min would
    otherwise shift one end of the range and not the other, inventing diurnal
    amplitude out of a coverage gap."""
    if not opposite_temps:
        return None
    shared = set(model_temps) & set(opposite_temps) & set(weights)
    tw = sum(weights[m] for m in shared)
    if tw <= 0:
        return None
    this_mean = sum(model_temps[m] * weights[m] for m in shared) / tw
    other_mean = sum(opposite_temps[m] * weights[m] for m in shared) / tw
    rng = (this_mean - other_mean) if is_high else (other_mean - this_mean)
    return rng if rng > 0 else None


def _build_engine_result(model_temps, region, city_key, lead_hours, is_high,
                         raw_models=None, corrections=None, opposite_temps=None):
    """Assemble the signal-engine dict from a model→temp map.

    Single implementation shared by get_signal_engine (one market) and
    prefetch_signal_engines (whole scan). These were two copies of the same
    thirty lines and had already drifted apart in their comments; sigma is now
    fitted per direction, which is exactly the kind of change that must not be
    made twice. Returns None when the ensemble is too thin to trade."""
    if not model_temps or len(model_temps) < MIN_MODEL_COUNT:
        if model_temps:
            logging.warning(
                f"Only {len(model_temps)} model(s) available for {city_key} "
                f"(is_high={is_high}), need >= {MIN_MODEL_COUNT} — skipping"
            )
        return None

    weights = WEIGHTS[region]
    total_weight = sum(weights[m] for m in model_temps if m in weights)
    if total_weight == 0:
        return None

    raw_weighted_mean = sum(
        temp * (weights[m] / total_weight)
        for m, temp in model_temps.items() if m in weights
    )
    # METAR_WARM_CORRECTION_F is 0 by default since 2026-07-31 — the per-model,
    # per-direction corrections in fetch_forecasts now carry what this used to
    # approximate with one global number. Kept as a knob, not a default.
    weighted_mean = raw_weighted_mean + METAR_WARM_CORRECTION_F

    model_spread_std = float(_weighted_pstdev(model_temps, weights))
    sigma_stages = compute_sigma_stages(model_spread_std, lead_hours, is_high, city_key)
    combined_std = sigma_stages["post_clamp"]

    # Agreement is measured against the RAW consensus, not the bias-shifted mean:
    # no model temp contains METAR_WARM_CORRECTION_F, so comparing to the shifted
    # mean made the band asymmetric and could fail a perfectly agreeing ensemble.
    # WEIGHTED so that adding a low-weight regional member cannot swing the gate
    # as hard as ECMWF — see the n-invariance note on MIN_MODEL_AGREEMENT.
    agree_w = sum(weights.get(m, 0.0) for m, t in model_temps.items()
                  if abs(t - raw_weighted_mean) < 2.0)
    model_agreement = agree_w / total_weight if total_weight else 0.0

    return {
        "ensemble_mean": weighted_mean,
        "ensemble_std": combined_std,
        "model_spread_std": model_spread_std,
        "base_error": _interpolate_base_error(lead_hours),
        "model_agreement": model_agreement,
        "raw_models": model_temps,
        "raw_weighted_mean": raw_weighted_mean,
        "city_key": city_key,
        # Reported as a weighted stdev, NOT max-min. max-min can only grow with
        # member count, so the old value made MAX_MODEL_SPREAD a de-facto cap on
        # ensemble SIZE rather than on disagreement.
        "model_spread": model_spread_std,
        "model_spread_range": max(model_temps.values()) - min(model_temps.values()),
        "lead_time_hours": lead_hours,
        "model_count": len(model_temps),
        "is_high": bool(is_high),
        "convective_inflated": city_key in CONVECTIVE_CITIES,
        # --- replay-log payload ---------------------------------------
        # raw_models here is the PRE-correction Open-Meteo value, unlike the
        # "raw_models" key above (which is post-correction and kept under that
        # name only because the signals table and the dashboard already read
        # it). `corrections` is what applied_corrections returned at fetch
        # time, so raw + correction == the value actually used, provably,
        # without a replay needing to know the config history.
        # The diurnal range the ensemble expects for this day, in °F. Free since
        # Phase 1.2 returns both directions from one request. Intraday
        # conditioning needs it to turn the dimensionless fitted remaining-rise
        # fraction into degrees — a fraction of a range is meaningless without
        # the range. None when the opposite direction is unavailable, which
        # makes conditioning fall through rather than guess.
        "forecast_diurnal_range_f": _diurnal_range(model_temps, opposite_temps,
                                                   weights, is_high),
        "raw_models_pre_correction": dict(raw_models) if raw_models else None,
        "corrections_applied": dict(corrections) if corrections else None,
        "model_weights": {m: weights[m] for m in model_temps if m in weights},
        "sigma_stages": sigma_stages,
        "region": region,
    }

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
    "NYC": {"lat": 40.7772, "lon": -73.8726, "region": "US", "window": "local"},          # LaGuardia (KLGA)
    "New York": {"lat": 40.7772, "lon": -73.8726, "region": "US", "window": "local"},     # LaGuardia (KLGA)
    "Chicago": {"lat": 41.9742, "lon": -87.9073, "region": "US", "window": "local"},      # O'Hare (KORD)
    "Miami": {"lat": 25.7932, "lon": -80.2906, "region": "US", "window": "local"},        # Miami Intl (KMIA)
    "Dallas": {"lat": 32.8471, "lon": -96.8518, "region": "US", "window": "local"},       # Love Field (KDAL)
    "Los Angeles": {"lat": 33.9416, "lon": -118.4085, "region": "US", "window": "local"}, # LAX (KLAX)
    "San Francisco": {"lat": 37.6213, "lon": -122.3790, "region": "US", "window": "local"}, # SFO (KSFO)
    "Austin": {"lat": 30.1975, "lon": -97.6664, "region": "US", "window": "local"},       # Austin-Bergstrom (KAUS)
    "Houston": {"lat": 29.6454, "lon": -95.2789, "region": "US", "window": "local"},      # Hobby (KHOU)
    "Seattle": {"lat": 47.4502, "lon": -122.3088, "region": "US", "window": "local"},     # Sea-Tac (KSEA)
    "Denver": {"lat": 39.7017, "lon": -104.7527, "region": "US", "window": "local"},      # Buckley SFB (KBKF)
    "Atlanta": {"lat": 33.6407, "lon": -84.4277, "region": "US", "window": "local"},      # Hartsfield (KATL)
    "Toronto": {"lat": 43.6777, "lon": -79.6248, "region": "US", "window": "local"},      # Pearson (CYYZ)
    "Mexico City": {"lat": 19.4363, "lon": -99.0721, "region": "US", "window": "local"},  # Benito Juárez (MMMX)
    "Panama": {"lat": 8.9733, "lon": -79.5556, "region": "US", "window": "local"},        # Marcos A. Gelabert (MPMG)
    # South America (GFS unavailable here — GLOBAL blend, no GFS)
    "Buenos Aires": {"lat": -34.8222, "lon": -58.5358, "region": "GLOBAL", "window": "local"}, # Ezeiza/Pistarini (SAEZ)
    "Sao Paulo": {"lat": -23.4356, "lon": -46.4731, "region": "GLOBAL", "window": "local"},    # Guarulhos (SBGR)
    # Europe
    "London": {"lat": 51.5048, "lon": 0.0495, "region": "EU", "window": "local"},         # London City Airport (EGLC) — NOT Heathrow
    "Paris": {"lat": 48.9694, "lon": 2.4414, "region": "EU", "window": "local"},          # Le Bourget (LFPB)
    "Berlin": {"lat": 52.3667, "lon": 13.5033, "region": "EU", "window": "local"},        # BER (EDDB)
    "Amsterdam": {"lat": 52.3105, "lon": 4.7683, "region": "EU", "window": "local"},      # Schiphol (EHAM)
    "Helsinki": {"lat": 60.3172, "lon": 24.9633, "region": "EU", "window": "local"},      # Vantaa (EFHK)
    "Istanbul": {"lat": 41.2753, "lon": 28.7519, "region": "EU", "window": "local"},      # Istanbul Airport (LTFM)
    "Madrid": {"lat": 40.4936, "lon": -3.5668, "region": "EU", "window": "local"},        # Barajas (LEMD)
    "Milan": {"lat": 45.6306, "lon": 8.7231, "region": "EU", "window": "local"},          # Malpensa (LIMC)
    # Vnukovo, NOT Domodedovo: Moscow markets resolve on NOAA's feed for Vnukovo
    # International (weather.gov/wrh/timeseries?site=UUWW per the market description).
    # The airport-convention default picked Domodedovo, ~40km away — same class of
    # bug as the Hong Kong Observatory fix. Audited across all Gamma weather markets
    # 2026-07-28; every other city's station matched.
    "Moscow": {"lat": 55.5915, "lon": 37.2615, "region": "EU", "window": "local"},        # Vnukovo (UUWW)
    "Munich": {"lat": 48.3537, "lon": 11.7750, "region": "EU", "window": "local"},        # Munich (EDDM)
    "Warsaw": {"lat": 52.1657, "lon": 20.9671, "region": "EU", "window": "local"},        # Chopin (EPWA)
    # Middle East / Africa (GFS unavailable — GLOBAL blend, no GFS).
    # Ankara stays EU: ecmwf_ifs025 + gfs_global both return data there.
    "Tel Aviv": {"lat": 32.0114, "lon": 34.8867, "region": "GLOBAL", "window": "local"},  # Ben Gurion (LLBG)
    "Ankara": {"lat": 40.1281, "lon": 32.9951, "region": "EU", "window": "local"},        # Esenboğa (LTAC)
    "Jeddah": {"lat": 21.6796, "lon": 39.1566, "region": "GLOBAL", "window": "local"},    # King Abdulaziz (OEJN)
    "Lagos": {"lat": 6.5774, "lon": 3.3212, "region": "GLOBAL", "window": "local"},       # Murtala Muhammed (DNMM)
    "Cape Town": {"lat": -33.9648, "lon": 18.6017, "region": "GLOBAL", "window": "local"}, # Cape Town Intl (FACT)
    # Asia-Pacific
    "Tokyo": {"lat": 35.5523, "lon": 139.7798, "region": "AP", "window": "local"},        # Haneda (RJTT)
    # HK Observatory HQ, Tsim Sha Tsui — NOT the airport. Every other city in this
    # table resolves on an airport station, so the airport convention was applied
    # here by default and was wrong: HK markets resolve on the Hong Kong Observatory
    # "Absolute Daily Max (deg. C)" from its Daily Extract, per the market
    # description text. HKG (22.3080, 113.9185) sits ~26km west on reclaimed land
    # surrounded by water; HKO is inland urban Kowloon and reads warmer on hot days.
    # On the 1°C buckets these markets use, that bias decides the outcome.
    "Hong Kong": {"lat": 22.3022, "lon": 114.1746, "region": "AP", "window": "local"},    # HK Observatory HQ
    "Seoul": {"lat": 37.4602, "lon": 126.4407, "region": "AP", "window": "local"},        # INCHEON (RKSI) — NOT the city centre
    "Shanghai": {"lat": 31.1443, "lon": 121.8083, "region": "AP", "window": "local"},     # Pudong (ZSPD)
    "Beijing": {"lat": 40.0799, "lon": 116.5847, "region": "AP", "window": "local"},      # Capital (ZBAA)
    "Guangzhou": {"lat": 23.3924, "lon": 113.2988, "region": "AP", "window": "local"},    # Baiyun (ZGGG)
    "Shenzhen": {"lat": 22.6393, "lon": 113.8107, "region": "AP", "window": "local"},     # Bao'an (ZGSZ)
    "Chengdu": {"lat": 30.5785, "lon": 103.9469, "region": "AP", "window": "local"},      # Shuangliu (ZUUU)
    "Chongqing": {"lat": 29.7192, "lon": 106.6417, "region": "AP", "window": "local"},    # Jiangbei (ZUCK)
    "Wuhan": {"lat": 30.7838, "lon": 114.2081, "region": "AP", "window": "local"},        # Tianhe (ZHHH)
    "Qingdao": {"lat": 36.3319, "lon": 120.3742, "region": "AP", "window": "local"},      # Jiaodong (ZSQD)
    "Busan": {"lat": 35.1795, "lon": 128.9380, "region": "AP", "window": "local"},        # Gimhae (RKPK)
    "Taipei": {"lat": 25.0694, "lon": 121.5525, "region": "AP", "window": "local"},       # Songshan (RCSS)
    "Singapore": {"lat": 1.3644, "lon": 103.9915, "region": "AP", "window": "local"},     # Changi (WSSS)
    "Kuala Lumpur": {"lat": 2.7456, "lon": 101.7072, "region": "AP", "window": "local"},  # KLIA (WMKK)
    "Jakarta": {"lat": -6.1256, "lon": 106.6559, "region": "AP", "window": "local"},      # Soekarno-Hatta (WIII)
    "Manila": {"lat": 14.5086, "lon": 121.0197, "region": "AP", "window": "local"},       # NAIA (RPLL)
    # Karachi REMOVED (untradeable until resolved): Polymarket's own market description
    # contradicts itself — the text names "Masroor Airbase Station" (OPMR) but the
    # resolution URL in the same description points at wunderground .../pk/karachi/OPKC
    # (Jinnah Intl, ~10km away). Which station actually settles is unknowable from the
    # text; a wrong guess is the Hong Kong bug all over again. Re-add once a resolved
    # Karachi market's outcome is checked against both stations' readings.
    # "Karachi": {"lat": 24.8936, "lon": 66.9385, "region": "AP", "window": "local"},     # Masroor Airbase (OPMR)
    "Lucknow": {"lat": 26.7606, "lon": 80.8893, "region": "AP", "window": "local"},       # CCS Intl (VILK)
    "Wellington": {"lat": -41.3272, "lon": 174.8053, "region": "AP", "window": "local"},  # Wellington Intl (NZWN)
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

# --- Settlement window ------------------------------------------------------
# `window` on each STATIONS entry says WHICH DAY, measured how, settles that
# city's markets. Determined by reading the markets' own resolution text, not by
# convention — see audit_settlement_windows.py and
# reports/settlement-windows-2026-08-05.md.
#
# The 2026-08-05 audit read 20,988 live temperature markets across 48 cities and
# found ONE convention, verbatim in every description:
#
#     "the highest temperature recorded for all times on this day
#      for the <STATION> Station"
#
# So every entry is "local" — local midnight to local midnight. No market uses
# 00-24Z and none uses 6-hourly synoptic max groups.
#
# That is a negative result and it is worth stating plainly: the Hong Kong,
# Moscow, Seoul and London corrections were STATION-identity bugs, not
# day-boundary bugs. metar.fetch_day_extremes already filters observations to
# the station's local calendar day via its IANA timezone, which is exactly what
# the text specifies, so reader and resolver already agree.
#
# The field exists anyway, and is validated at boot, because the audit is a
# snapshot: Polymarket has changed resolution sources before (Moscow moved to a
# NOAA feed), and the next change should surface as a value here rather than as
# a run of inexplicable losses. Set a city to "UNKNOWN" to exclude it from
# trading — the Karachi precedent.
VALID_WINDOWS = {"local", "00-24Z", "6h-groups", "UNKNOWN"}


def settlement_window(city_name):
    """The window that settles this city, or None if the city is unknown."""
    _, station = get_station_coords(city_name)
    return station.get("window") if station else None


def is_tradeable_window(city_name):
    """False when the settlement window is not established.

    An UNKNOWN window means a trade would settle on a rule nobody has read.
    Karachi is the precedent: its description named Masroor Airbase and linked
    Jinnah International, and refusing to trade it was the correct call."""
    return settlement_window(city_name) not in (None, "UNKNOWN")


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

# A local day must be this well covered by hourly values before its max/min is
# trusted. Open-Meteo returns whole local days, but a truncated response, a
# model that starts mid-day, or a DST transition can leave a day with a handful
# of hours — and max() over 3 morning hours is a "daily maximum" that is simply
# wrong, in the direction that makes a NO bet look safe. 20 of 24 tolerates the
# 23-hour spring-forward day without tolerating a genuinely partial one.
MIN_HOURLY_COVERAGE = 20


def _aggregate_local_days(times, values):
    """(max, min) per LOCAL calendar day from an hourly series.

    `times` are Open-Meteo's local-time stamps (timezone=auto), so the first ten
    characters ARE the local date — which is the window every market settles on
    per the 2026-08-05 audit. Days with too few hours are dropped rather than
    aggregated; see MIN_HOURLY_COVERAGE."""
    buckets = {}
    for t, v in zip(times, values):
        if v is None:
            continue
        buckets.setdefault(t[:10], []).append(v)
    return {day: (max(vals), min(vals))
            for day, vals in buckets.items() if len(vals) >= MIN_HOURLY_COVERAGE}


def _fetch_hourly(city_key, station, force_refresh=False):
    """Hourly temperature per model, aggregated to local-day (max, min).

    Returns {date: {model: (max_f, min_f)}} or None.

    HOURLY rather than daily=temperature_2m_max, for two reasons — and NOT for
    the reason it looks like:

      * It is NOT to remove the diurnal-compression artifact. Measured
        2026-08-05: daily=temperature_2m_max equals max(hourly) to 0.000°F for
        all five members INCLUDING 6-hourly jma_gsm, because Open-Meteo
        interpolates upstream of both endpoints. See
        tests/test_hourly_aggregation.py, which asserts this rather than
        leaving it as a comment nobody re-checks. The only real lever on the
        diurnal artifact is preferring natively-hourly members (Phase 2.1).
      * It IS the prerequisite for intraday observation conditioning, which
        needs the shape of the day and not just its peak.

    It also halves the request count. Both directions come out of one series,
    where the daily endpoint needed a separate call for max and for min — and on
    this API latency is dominated by round trips, not payload (measured: 979ms /
    746B for one daily field, 1597ms / 4780B for the whole hourly series)."""
    cached = _FORECAST_CACHE.get(city_key) if not force_refresh else None
    if cached:
        age = _time.monotonic() - cached[0]
        if age < _FORECAST_TTL_SECONDS:
            logging.debug(f"Forecast cache hit for {city_key} (age={age:.0f}s)")
            return cached[1]

    models = list(WEIGHTS[station["region"]].keys())
    params = {
        "latitude": station["lat"],
        "longitude": station["lon"],
        "hourly": "temperature_2m",
        "models": ",".join(models),
        # Local time, so t[:10] is the local calendar day the market settles on.
        "timezone": "auto",
        "temperature_unit": "fahrenheit",
        "forecast_days": 4,
    }
    try:
        resp = get_session().get(OPEN_METEO_URL, params=params, timeout=10)
        if resp.status_code != 200:
            logging.error(f"Open-Meteo error ({resp.status_code}): {resp.text[:300]}")
            return None
    except Exception as e:
        logging.error(f"Open-Meteo request failed: {e}")
        return None

    hourly = resp.json().get("hourly", {})
    times = hourly.get("time", [])
    by_date = {}
    for model in models:
        series = hourly.get(f"temperature_2m_{model}")
        if not series:
            continue          # out-of-domain member; the caller drops nulls
        for day, (mx, mn) in _aggregate_local_days(times, series).items():
            by_date.setdefault(day, {})[model] = (mx, mn)

    _FORECAST_CACHE[city_key] = (_time.monotonic(), by_date)
    return by_date


def fetch_forecasts(city_name, is_high=True, force_refresh=False):
    city_key, station = get_station_coords(city_name)
    if not station:
        logging.warning(f"No station mapping found for {city_name}")
        return None

    region = station["region"]
    by_date = _fetch_hourly(city_key, station, force_refresh)
    if by_date is None:
        return None

    forecasts_by_date = {}
    raw_by_date = {}
    corrections_by_date = {}
    for date_str, per_model in by_date.items():
        model_temps = {m: (mx if is_high else mn) for m, (mx, mn) in per_model.items()}

        # Corrections come from ONE place (applied_corrections) so the replay
        # log records exactly what was applied here — see that function. The
        # per-model, per-direction split exists because a model that runs cold
        # on daily maxima generally runs warm on daily minima: Open-Meteo
        # interpolates coarse-timestep output to hourly, and smoothing a diurnal
        # curve clips the afternoon peak and lifts the overnight trough. One
        # signed offset applied to both directions corrects one of them the
        # wrong way; for jma_gsm the old flat +1.55 was 3.26°F out on minima.
        corr = applied_corrections(city_key, is_high, model_temps)
        raw_by_date[date_str] = dict(model_temps)
        corrections_by_date[date_str] = corr
        forecasts_by_date[date_str] = {m: v + corr[m] for m, v in model_temps.items()}

    # Not cached here: _fetch_hourly caches the direction-INDEPENDENT series, so
    # both is_high values are served from one HTTP call. Caching the corrected
    # output too would double the memory for a per-direction dict comprehension
    # over four dates.
    return (forecasts_by_date, city_key, region, raw_by_date, corrections_by_date)

def get_signal_engine(city_name, target_date, is_high=True, hours_to_resolution=48.0):
    res = fetch_forecasts(city_name, is_high)
    if not res:
        return None
    forecasts_by_date, city_key, region, raw_by_date, corr_by_date = res

    if target_date not in forecasts_by_date:
        logging.warning(f"Target date {target_date} not in forecast range")
        return None

    # The other direction, for the diurnal range. Free: it comes out of the same
    # cached hourly series that produced this one.
    opposite = fetch_forecasts(city_name, not is_high)
    opposite_temps = opposite[0].get(target_date) if opposite else None

    engine = _build_engine_result(
        forecasts_by_date[target_date], region, city_key,
        hours_to_resolution, is_high,
        raw_models=raw_by_date.get(target_date),
        corrections=corr_by_date.get(target_date),
        opposite_temps=opposite_temps,
    )
    return _condition_on_observations(engine, target_date)


def _condition_on_observations(engine, target_date):
    """Apply intraday conditioning, never letting it break the trading path.

    intraday.condition reads live METAR. That is a network call on the
    evaluation path, and the whole point of the fallback design is that a slow
    or broken observation feed degrades to the unconditioned forecast rather
    than to no trading at all."""
    if engine is None:
        return None
    try:
        from intraday import condition
        return condition(engine, target_date)
    except Exception as e:
        logging.error(f"intraday conditioning failed for "
                      f"{engine.get('city_key')} {target_date}: {e}", exc_info=True)
        return engine

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


def _truncated_cdf(x, mean, std, bound, bound_is_floor):
    """CDF of the forecast distribution truncated at an OBSERVED extreme.

    The final daily max cannot be below the max already observed today, and the
    final daily min cannot be above the min already observed. That is
    arithmetic, not a probabilistic statement, so it is applied as a hard
    truncation and renormalisation rather than as a shifted mean — a bucket
    entirely below an observed maximum must price at exactly zero, not at
    something small.

    Renormalisation is numerically safe here because intraday.condition sets the
    conditioned mean to at least the bound, which keeps CDF(bound) <= 0.5. The
    guard below covers the case where a caller sets a bound without doing that.
    """
    if bound is None:
        return _bucket_cdf(x, mean, std)

    if bound_is_floor:
        if x <= bound:
            return 0.0
        c_bound = _bucket_cdf(bound, mean, std)
        denom = 1.0 - c_bound
        if denom <= 1e-9:
            # The model is so far below the observation that essentially all of
            # its mass is already excluded. Renormalising would divide by ~0;
            # the honest reading is that everything above the bound is possible
            # and the model has no view left, so fall back to untruncated.
            return _bucket_cdf(x, mean, std)
        return min(1.0, (_bucket_cdf(x, mean, std) - c_bound) / denom)

    # Ceiling: no mass above an observed minimum.
    if x >= bound:
        return 1.0
    c_bound = _bucket_cdf(bound, mean, std)
    if c_bound <= 1e-9:
        return _bucket_cdf(x, mean, std)
    return max(0.0, _bucket_cdf(x, mean, std) / c_bound)


def bucket_probability_stages(engine_result, bucket_lower, bucket_upper):
    """P(YES) at each stage: raw CDF, post-Platt, post-floor.

    Same reason as compute_sigma_stages — a logged probability of exactly 0.05
    is unreadable without knowing whether the floor bound or the Gaussian
    genuinely landed there. get_bucket_probability delegates here so there is
    one implementation."""
    mean = engine_result["ensemble_mean"]
    std = max(engine_result["ensemble_std"], 0.5)

    lb = bucket_lower if bucket_lower is not None else -1000.0
    ub = bucket_upper if bucket_upper is not None else 1000.0

    if bucket_lower is not None and bucket_upper is not None:
        lb -= 0.5
        ub += 0.5
    elif bucket_lower is not None:
        lb -= 0.5
    elif bucket_upper is not None:
        ub += 0.5

    # Truncated at today's observed extreme when intraday conditioning applied.
    bound = engine_result.get("hard_bound")
    is_floor = engine_result.get("hard_bound_is_floor", True)
    raw = (_truncated_cdf(ub, mean, std, bound, is_floor)
           - _truncated_cdf(lb, mean, std, bound, is_floor))
    raw = max(0.0, min(1.0, float(raw)))

    is_bounded = bucket_lower is not None and bucket_upper is not None
    post_platt = _calibrate_prob(raw) if is_bounded else raw

    # The tail floor is a statement about FORECAST noise: no bucket is truly
    # less than ~5% likely when the forecast could be a degree off. It is not a
    # statement about observed facts. When today's observations have already
    # excluded the bucket, the answer is exactly zero, and flooring it back to
    # 0.05 would throw away the single most valuable thing intraday
    # conditioning produces — a bucket that CANNOT pay, priced as such.
    excluded_by_observation = bound is not None and raw <= 0.0
    post_floor = post_platt
    floor_bound = (MIN_BUCKET_PROB > 0.0 and post_platt < MIN_BUCKET_PROB
                   and not excluded_by_observation)
    if floor_bound:
        post_floor = MIN_BUCKET_PROB
    post_floor = max(0.0, min(1.0, float(post_floor)))

    return {
        "raw": raw,
        "post_platt": post_platt,
        "post_floor": post_floor,
        "platt_applied": is_bounded and ENABLE_PROB_CALIBRATION,
        "floor_bound": floor_bound,
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

    bound = engine_result.get("hard_bound")
    is_floor = engine_result.get("hard_bound_is_floor", True)
    prob = (_truncated_cdf(ub, mean, std, bound, is_floor)
            - _truncated_cdf(lb, mean, std, bound, is_floor))
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
    # ...but never over a bucket that today's observations have already ruled
    # out. See bucket_probability_stages: the floor models forecast noise, and
    # an observed extreme is not noise.
    excluded_by_observation = bound is not None and prob <= 0.0
    if MIN_BUCKET_PROB > 0.0 and prob < MIN_BUCKET_PROB and not excluded_by_observation:
        prob = MIN_BUCKET_PROB
    return max(0.0, min(1.0, float(prob)))


def applied_corrections(city_key, is_high, models):
    """The °F added to each model's raw Open-Meteo value, as a dict.

    ONE definition, used by fetch_forecasts to apply the corrections and by the
    replay logger to record them. They cannot drift apart, which is the entire
    point: the replay log stores raw + applied, and `applied` has to be what was
    actually applied, not a later reconstruction of what probably was. The
    2026-07-31 audit had to hardcode which corrections shipped on which date,
    and got the ensemble mean wrong on 5 of 27 trades as a result."""
    out = {}
    for model in models:
        c = 0.0
        if model == "gfs_global" and city_key in GFS_BIAS_CORRECTIONS:
            c += GFS_BIAS_CORRECTIONS[city_key]
        c += model_bias_correction(model, is_high)
        out[model] = c
    return out

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
    # Both directions for every city, not just the one an opportunity asked for:
    # the diurnal range needs the opposite direction, and since Phase 1.2 both
    # come out of one cached HTTP response, so the second is free.
    for city, is_high in {(c, d) for c, _ in city_is_high_keys for d in (True, False)}:
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
        forecasts_by_date, city_key, region, raw_by_date, corr_by_date = raw

        if opp.date not in forecasts_by_date:
            engine_cache[key] = None
            continue

        opposite = forecast_cache.get((opp.city, not opp.is_high))
        opposite_temps = opposite[0].get(opp.date) if opposite else None

        engine = _build_engine_result(
            forecasts_by_date[opp.date], region, city_key,
            opp.hours_to_resolution, opp.is_high,
            raw_models=raw_by_date.get(opp.date),
            corrections=corr_by_date.get(opp.date),
            opposite_temps=opposite_temps,
        )
        engine_cache[key] = _condition_on_observations(engine, opp.date)

    hits = sum(1 for v in engine_cache.values() if v is not None)
    conditioned = sum(1 for v in engine_cache.values()
                      if v and v.get("intraday", {}).get("applied"))
    logging.info(
        f"Weather prefetch: {len(city_is_high_keys)} city fetches → "
        f"{hits}/{len(engine_cache)} opportunity keys populated"
        + (f" | {conditioned} conditioned on today's observations"
           if conditioned else "")
    )
    return engine_cache


def validate_city_tables():
    """Every city named in a city-keyed config table must exist in STATIONS.

    A city that does not is a silent no-op: "Tampa" sat in both
    CONVECTIVE_CITIES and GFS_BIAS_CORRECTIONS while absent from STATIONS, so
    its std inflation and its -1.3°F GFS correction never once applied, and
    nothing said so. Typos in these tables fail exactly this quietly.

    Returns a list of human-readable problems; empty means clean. See
    validate_config_tables() for the raising entrypoint."""
    problems = []
    for label, names in (("CONVECTIVE_CITIES", CONVECTIVE_CITIES),
                         ("GFS_BIAS_CORRECTIONS", GFS_BIAS_CORRECTIONS)):
        for missing in sorted(set(names) - set(STATIONS)):
            problems.append(f"{label} names '{missing}', which is not in STATIONS "
                            f"(its setting has never applied)")
    # Every station must declare an AUDITED settlement window. A missing one is
    # not a harmless default: it means a city was added without anyone reading
    # what day its markets actually settle on, which is how the Hong Kong and
    # Moscow station bugs got in.
    for city, station in sorted(STATIONS.items()):
        w = station.get("window")
        if w is None:
            problems.append(
                f"STATIONS['{city}'] has no 'window' — run audit_settlement_windows.py "
                f"and record which day its markets settle on")
        elif w not in VALID_WINDOWS:
            problems.append(
                f"STATIONS['{city}'] window={w!r} is not one of {sorted(VALID_WINDOWS)}")
    return problems


def validate_model_tables():
    """Every model in WEIGHTS must carry an explicit bias correction.

    A model with no entry used to inherit a timestep prior; it now raises. This
    catches that at boot instead of on the first fetch, where the exception would
    be swallowed by prefetch_signal_engines' per-city handler and present as
    "no opportunities" rather than as a misconfiguration."""
    problems = []
    for region, weights in WEIGHTS.items():
        for model in sorted(weights):
            for is_high in (True, False):
                try:
                    model_bias_correction(model, is_high)
                except KeyError as e:
                    problems.append(f"WEIGHTS[{region!r}]: {e.args[0]}")
    return problems


def validate_config_tables(raise_on_problem=True):
    """Boot-time guard over every table that can silently not apply.

    RAISES by default. These are code-controlled, version-tracked tables, so a
    problem here means a human edited one half of a pair and not the other —
    exactly the failure that shipped Tampa (a setting that never applied for
    weeks) and gfs_global's phantom +0.7°F. Refusing to boot is the cheap
    outcome; trading on a table that quietly does nothing is the expensive one.

    Call BEFORE the first scan cycle, from every entrypoint."""
    from config import validate_env_ranges
    from risk import validate_synoptic_groups
    problems = (validate_city_tables() + validate_model_tables()
                + validate_synoptic_groups() + validate_env_ranges())
    for p in problems:
        logging.error(f"Config validation: {p}")
    if problems and raise_on_problem:
        raise RuntimeError(
            f"{len(problems)} config table problem(s) — refusing to start:\n  "
            + "\n  ".join(problems)
        )
    return problems


def log_model_accuracy(city, target_date, model, forecast_temp, actual_temp, is_high=None):
    """Record one model's forecast against the settled temperature.

    is_high is part of the key: a city's daily max and min are two different
    forecasting problems with opposite biases, and writing both under one key
    left 24 of 47 verified city-days in the 2026-07-31 export carrying two
    conflicting 'actual' values. UPSERT on the natural key so a re-run refreshes
    a row instead of stacking duplicates.

    The `WHERE is_high IS NOT NULL` on the conflict target is REQUIRED, not
    decorative: the backing index is partial (it excludes legacy rows whose
    direction could not be recovered), and SQLite rejects a conflict target that
    does not match a real index — without it every call here raises
    OperationalError."""
    execute_query('''
        INSERT INTO model_accuracy (city, target_date, is_high, model, forecast_temp, actual_temp)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (city, target_date, is_high, model) WHERE is_high IS NOT NULL
        DO UPDATE SET forecast_temp=excluded.forecast_temp, actual_temp=excluded.actual_temp
    ''', (city, target_date, (None if is_high is None else int(bool(is_high))),
          model, forecast_temp, actual_temp))
