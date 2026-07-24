"""
METAR observation source — the SAME ruler Polymarket resolves against.

Polymarket weather markets resolve off Wunderground's daily history for a specific
ICAO airport station ("highest/lowest temperature recorded for all times on this day
... measured to whole degrees Celsius"). Wunderground publishes the airport METAR
observation feed. We read that same feed from the Iowa Environmental Mesonet ASOS
archive (free, no API key) so our verification and calibration use the exact numbers
the market settles on — not Open-Meteo's ERA5 reanalysis, which was found to differ by
up to ~1°C and flip whole-degree-Celsius outcomes.

Forecasts still come from Open-Meteo NWP (METAR is observations, it can't predict the
future). METAR is used for VERIFICATION: resolving trades, scoring calibration, and
learning the per-station Open-Meteo→METAR bias.
"""
import csv
import io
import logging
import math
from datetime import date as _date, datetime, timedelta
from zoneinfo import ZoneInfo

from utils import safe_get

MESONET_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"

# City name -> (ICAO station, IANA timezone). ICAO is the station Polymarket names in
# each market's resolution text; the tz makes "the highest temp on <local day>" align
# to the station's civil day, exactly as Wunderground's daily history page does.
STATION_ICAO = {
    "NYC": ("KLGA", "America/New_York"),
    "New York": ("KLGA", "America/New_York"),
    "Chicago": ("KORD", "America/Chicago"),
    "Miami": ("KMIA", "America/New_York"),
    "Dallas": ("KDAL", "America/Chicago"),
    "Los Angeles": ("KLAX", "America/Los_Angeles"),
    "San Francisco": ("KSFO", "America/Los_Angeles"),
    "Austin": ("KAUS", "America/Chicago"),
    "Houston": ("KHOU", "America/Chicago"),
    "Seattle": ("KSEA", "America/Los_Angeles"),
    "Denver": ("KBKF", "America/Denver"),
    "Atlanta": ("KATL", "America/New_York"),
    "Toronto": ("CYYZ", "America/Toronto"),
    "Mexico City": ("MMMX", "America/Mexico_City"),
    "Panama": ("MPMG", "America/Panama"),
    "Buenos Aires": ("SAEZ", "America/Argentina/Buenos_Aires"),
    "Sao Paulo": ("SBGR", "America/Sao_Paulo"),
    "London": ("EGLC", "Europe/London"),
    "Paris": ("LFPB", "Europe/Paris"),
    "Berlin": ("EDDB", "Europe/Berlin"),
    "Amsterdam": ("EHAM", "Europe/Amsterdam"),
    "Helsinki": ("EFHK", "Europe/Helsinki"),
    "Istanbul": ("LTFM", "Europe/Istanbul"),
    "Madrid": ("LEMD", "Europe/Madrid"),
    "Milan": ("LIMC", "Europe/Rome"),
    "Moscow": ("UUDD", "Europe/Moscow"),
    "Munich": ("EDDM", "Europe/Berlin"),
    "Warsaw": ("EPWA", "Europe/Warsaw"),
    "Tel Aviv": ("LLBG", "Asia/Jerusalem"),
    "Ankara": ("LTAC", "Europe/Istanbul"),
    "Jeddah": ("OEJN", "Asia/Riyadh"),
    "Lagos": ("DNMM", "Africa/Lagos"),
    "Cape Town": ("FACT", "Africa/Johannesburg"),
    "Tokyo": ("RJTT", "Asia/Tokyo"),
    "Hong Kong": ("VHHH", "Asia/Hong_Kong"),
    "Seoul": ("RKSI", "Asia/Seoul"),
    "Shanghai": ("ZSPD", "Asia/Shanghai"),
    "Beijing": ("ZBAA", "Asia/Shanghai"),
    "Guangzhou": ("ZGGG", "Asia/Shanghai"),
    "Shenzhen": ("ZGSZ", "Asia/Shanghai"),
    "Chengdu": ("ZUUU", "Asia/Shanghai"),
    "Chongqing": ("ZUCK", "Asia/Shanghai"),
    "Wuhan": ("ZHHH", "Asia/Shanghai"),
    "Qingdao": ("ZSQD", "Asia/Shanghai"),
    "Busan": ("RKPK", "Asia/Seoul"),
    "Taipei": ("RCSS", "Asia/Taipei"),
    "Singapore": ("WSSS", "Asia/Singapore"),
    "Kuala Lumpur": ("WMKK", "Asia/Kuala_Lumpur"),
    "Jakarta": ("WIII", "Asia/Jakarta"),
    "Manila": ("RPLL", "Asia/Manila"),
    "Karachi": ("OPMR", "Asia/Karachi"),
    "Lucknow": ("VILK", "Asia/Kolkata"),
    "Wellington": ("NZWN", "Pacific/Auckland"),
}

# in-process cache: (icao, date_str) -> (max_c, min_c)
_METAR_CACHE: dict = {}


def get_station(city_key):
    """Return (icao, tz) for a canonical city key, or (None, None)."""
    v = STATION_ICAO.get(city_key)
    return v if v else (None, None)


def fetch_day_extremes(icao, tz, date_str):
    """Return (max_c, min_c) of all METAR temperature obs on the station's LOCAL
    calendar day date_str (YYYY-MM-DD), matching Wunderground's daily rollup. Returns
    (None, None) if the day has no published observations yet or the fetch fails.

    Temperatures are the raw METAR °C values; the caller rounds to whole °C to match
    the resolution precision, exactly as Polymarket does."""
    key = (icao, date_str)
    if key in _METAR_CACHE:
        return _METAR_CACHE[key]

    y, m, d = (int(x) for x in date_str.split("-"))
    nd = _date(y, m, d) + timedelta(days=1)
    params = {
        "station": icao, "data": "tmpc",
        "year1": y, "month1": m, "day1": d,
        "year2": nd.year, "month2": nd.month, "day2": nd.day,
        "tz": tz, "format": "onlycomma", "latlon": "no", "missing": "M",
    }
    result = (None, None)
    try:
        resp = safe_get(MESONET_URL, params=params, timeout=30)
        if resp.status_code == 200:
            temps = []
            for row in csv.DictReader(io.StringIO(resp.text)):
                # keep only rows whose local timestamp falls on the target day
                if row.get("valid", "")[:10] != date_str:
                    continue
                v = row.get("tmpc", "M")
                if v not in ("M", ""):
                    try:
                        temps.append(float(v))
                    except ValueError:
                        continue
            if temps:
                result = (max(temps), min(temps))
    except Exception as e:
        logging.error(f"METAR fetch failed for {icao} {date_str}: {e}")
    # Only cache days that are COMPLETE in station-local time, and only real data.
    # The monitor loop re-fetches the target day every cycle precisely to watch the
    # intraday max evolve — a permanent cache here froze the "observed max" at the
    # first fetch of the day, blinding the bucket-bust check to the afternoon high.
    # Failed fetches ((None, None)) are never cached so transient errors can heal.
    if result != (None, None) and day_complete(tz, date_str):
        _METAR_CACHE[key] = result
    return result


def day_complete(tz, date_str):
    """True once the station's LOCAL calendar day date_str has fully elapsed, with a
    2h grace after local midnight because IEM's archive can lag its final obs."""
    try:
        now_local = datetime.now(ZoneInfo(tz))
    except Exception:
        now_local = datetime.utcnow()
    if date_str >= now_local.date().isoformat():
        return False
    return not (
        (now_local.date() - timedelta(days=1)).isoformat() == date_str
        and now_local.hour < 2
    )


def final_extreme_f(city_key, date_str, is_high):
    """The SETTLED daily extreme: like resolved_extreme_f, but returns None until the
    station's local calendar day has fully elapsed. Settlement and calibration must
    use this — resolved_extreme_f mid-day returns the partial max (e.g. Guangzhou's
    8am temperature), which booked phantom wins and poisoned model_accuracy."""
    icao, tz = get_station(city_key)
    if not icao or not day_complete(tz, date_str):
        return None
    return resolved_extreme_f(city_key, date_str, is_high)


def resolved_extreme_f(city_key, date_str, is_high):
    """The realized daily extreme SO FAR at this city's resolution station on
    date_str, returned in °F to match the rest of the pipeline. None if not yet
    available. Mid-day this is the running extreme, NOT the final one — use it for
    intraday bucket-bust checks only; settlement must go through final_extreme_f.

    Rounds to whole °C first (the market's resolution precision) then converts, so a
    31.4°C reading and a 30.6°C reading both land where the market actually settles."""
    icao, tz = get_station(city_key)
    if not icao:
        return None
    mx_c, mn_c = fetch_day_extremes(icao, tz, date_str)
    val_c = mx_c if is_high else mn_c
    if val_c is None:
        return None
    rounded_c = round_half_away(val_c)  # whole-°C resolution precision
    return rounded_c * 9.0 / 5.0 + 32.0


def round_half_away(v):
    """Round half AWAY from zero (30.5 → 31, -0.5 → -1), matching Wunderground's
    whole-degree rollup. Python's round() is banker's rounding (30.5 → 30), which
    mis-scores exactly the boundary readings the markets settle on."""
    return int(math.copysign(math.floor(abs(v) + 0.5), v))
