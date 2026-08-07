"""Second-opinion forecasts from providers OUTSIDE the Open-Meteo ensemble.

This is not a forecast improvement. `get_bucket_probability` already answers
"how plausible is this temperature", and a second deterministic forecast will
not sharpen that estimate at the margin.

It is a BLUNDER detector. It catches the errors that live upstream of the
probability calculation and are therefore invisible to it:

  * wrong station (the Hong Kong / Moscow / Seoul / Karachi class)
  * stale model run — no model_run_init is collected, so a several-hour-old run
    is currently undetectable
  * bucket parsed wrong from the market title
  * ensemble silently degraded to correlated members
  * a bias correction applied with the wrong sign or in the wrong direction

Each of those loses the full stake at once, and every one has happened here at
least once. They share a property: a second Open-Meteo member cannot see any of
them, because it is fetched through the same client, with the same STATIONS
lat/lon, on the same run schedule, aggregated the same way. Detecting them
requires a SEPARATE PIPELINE, not a separate model — which is why the providers
below are external services rather than additional ensemble members.

Consequence for the thresholds: the gate must be loose and must fire rarely.
Frequent firing means something is broken, not that the threshold needs tuning.

--- Why not MET Norway -------------------------------------------------------
api.met.no is free, needs no registration, and was the documented fallback. It
is deliberately NOT implemented here (owner decision 2026-08-06). MET Norway
runs MEPS for Scandinavia and ECMWF globally, with observation-based
post-processing only in Scandinavia and Spitsbergen. For 49 of our 51 cities it
is ECMWF-derived — the same model that carries the largest weight in every
regional blend. It would still check staleness and station identity, but it is
NOT an independent skill opinion, and a source labelled "independent" that
quietly agrees with the ensemble by construction is worse than no source: it
would make the gate look healthy while checking nothing. If it is ever added,
label it in the gate row so nobody later reads its agreement as confirmation.
"""

import logging
import threading
import time as _time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

from config import (
    INDEPENDENT_CACHE_TTL_SECONDS,
    INDEPENDENT_TIMEOUT_SECONDS,
    INDEPENDENT_BREAKER_FAILURES,
    INDEPENDENT_BREAKER_COOLDOWN_SECONDS,
    INDEPENDENT_USER_AGENT,
    METOFFICE_DATAHUB_KEY,
)

# --- The three states -------------------------------------------------------
# THE most important invariant in this module. A provider response is one of
# three things, never two:
#
#   DATA          HTTP 200 AND a parsed numeric temperature for the target date
#   NO_DATA       HTTP 200 with an explicit null, or an out-of-domain refusal
#                 (NWS 404, DataHub 400). The provider genuinely serves nothing
#                 here. A coverage gap, not a disagreement.
#   INCONCLUSIVE  everything else — timeout, 429, 5xx, HTML body, unparseable
#                 JSON, missing field, no key configured. Says NOTHING about the
#                 world.
#
# ONLY `DATA` MAY EVER REFUSE A TRADE.
#
# This is stated this forcefully because coverage_matrix.py drew a wrong
# conclusion from exactly this confusion three separate times. The final
# instance had Open-Meteo's rate limiter return HTML error pages, which rendered
# as "NO DATA ANYWHERE" and would have removed ukmo_uk_deterministic_2km from
# London — a member that works fine. A transient HTTP error was recorded as a
# fact about the world.
#
# The veto gate is far more exposed to that than the matrix script was: it runs
# continuously, against third-party providers, on the critical path of every
# trade decision. If a rate limit or an outage is read as "the independent
# forecast disagrees", the gate stops the book on an artefact — which is a worse
# failure than having no gate at all.
#
# So: never infer a temperature from an error. Never coerce a failed parse to
# 0.0, to a None that reads as agreement, or to any default. The three states
# are distinguished explicitly in code, in the logs, and in the stored gate row.
DATA = "DATA"
NO_DATA = "NO_DATA"
INCONCLUSIVE = "INCONCLUSIVE"


class IndependentForecast:
    """One provider's answer, with its state attached and never separable.

    There is deliberately no bare-float accessor. Every consumer must read
    `.state` to find out whether `.value_f` means anything, because the one bug
    this module exists to prevent is a number that came from an error being
    treated as a forecast."""

    __slots__ = ("state", "source", "value_f", "fetched_at", "detail")

    def __init__(self, state, source, value_f=None, fetched_at=None, detail=""):
        # Enforced, not documented: a non-DATA state can never carry a number.
        # This assertion is the structural version of "no error path ever
        # produces a numeric temperature" and it fires in tests, not in prod
        # logic, because the constructors below simply never violate it.
        if state != DATA:
            value_f = None
        self.state = state
        self.source = source
        self.value_f = value_f
        self.fetched_at = fetched_at
        self.detail = detail

    @property
    def is_data(self):
        return self.state == DATA

    def __repr__(self):
        v = "None" if self.value_f is None else f"{self.value_f:.1f}F"
        return f"<Independent {self.state} {self.source} {v} {self.detail}>"


# --- Provider routing -------------------------------------------------------
# Explicitly enumerated, NOT derived from STATIONS["region"]. The region field
# selects a model-weight blend, and its "US" bucket also contains Toronto,
# Mexico City and Panama — none of which api.weather.gov serves. Routing on
# `region` would send three cities to a provider that 404s them, and a 404
# handled carelessly is an INCONCLUSIVE that never resolves. The eleven names
# below are exactly the NWS-covered cities in STATIONS.
NWS_CITIES = frozenset({
    "NYC", "New York", "Chicago", "Miami", "Dallas", "Los Angeles",
    "San Francisco", "Austin", "Houston", "Seattle", "Denver", "Atlanta",
})

NWS_POINTS_URL = "https://api.weather.gov/points/{lat},{lon}"
DATAHUB_URL = "https://data.hub.api.metoffice.gov.uk/sitespecific/v0/point/hourly"

SOURCE_NWS = "nws"
SOURCE_DATAHUB = "metoffice_datahub"


def provider_for(city_key):
    """Which provider answers for this city. Never None — every city routes."""
    return SOURCE_NWS if city_key in NWS_CITIES else SOURCE_DATAHUB


# --- HTTP session -----------------------------------------------------------
# A private session, NOT utils.get_session(). That shared session mounts
# Retry(total=5, backoff_factor=1.0, status_forcelist=[429,500,502,503,504]),
# which turns one 3-second timeout budget into upwards of 30 seconds of
# retry-and-backoff on a 429 — on the critical path of every trade decision,
# for a signal that is allowed to be missing. The whole design here is "answer
# fast or don't answer": one attempt, short timeout, and a failure is simply
# INCONCLUSIVE. Retrying is what the 6-hour cache and the next scan cycle are
# for.
_session = None
_session_lock = threading.Lock()


def _get_session():
    global _session
    if _session is None:
        with _session_lock:
            if _session is None:
                s = requests.Session()
                s.headers.update({"User-Agent": INDEPENDENT_USER_AGENT})
                _session = s
    return _session


# --- Caches -----------------------------------------------------------------
# Keyed (city_key, target_date, is_high) with a 6h TTL, per the call budget:
# 51 cities x 3 refreshes/day = 153 calls, inside DataHub's 360/day free tier.
# A 10-minute scan interval would otherwise be ~7,300 calls/day and blow every
# free tier on the first afternoon.
#
# DATA and NO_DATA are cached; INCONCLUSIVE is NOT. Caching a transient failure
# would pin a 6-hour hole in coverage because one request timed out, and the
# circuit breaker below is the correct tool for a provider that is actually
# down — it distinguishes "this one call failed" from "this provider is
# failing", which a cache cannot.
_CACHE = {}
_CACHE_LOCK = threading.Lock()

# NWS gridpoint lookups are a separate, effectively permanent cache: a station's
# grid cell is a property of the coordinate, not of the forecast, so it changes
# only when NWS re-grids. Caching it removes one of the two calls per city per
# refresh, halving the NWS call count.
_GRIDPOINT_CACHE = {}
_GRIDPOINT_LOCK = threading.Lock()

# --- Circuit breaker --------------------------------------------------------
# Per provider, not global: DataHub being down must not blind the NWS cities.
_BREAKER = {}
_BREAKER_LOCK = threading.Lock()


def _breaker_open(source):
    """True when this provider is in its cooldown and must not be called."""
    with _BREAKER_LOCK:
        st = _BREAKER.get(source)
        if not st or st["opened_at"] is None:
            return False
        if _time.monotonic() - st["opened_at"] >= INDEPENDENT_BREAKER_COOLDOWN_SECONDS:
            # Cooldown elapsed — close and let the next call probe the provider.
            st["opened_at"] = None
            st["failures"] = 0
            logging.info(f"independent: circuit breaker CLOSED for {source}")
            return False
        return True


def _breaker_record(source, ok):
    """Count consecutive failures; open the breaker at the threshold.

    Logged ONCE on the transition, not on every suppressed call. A provider
    outage should produce one line an hour, not one line per city per scan."""
    with _BREAKER_LOCK:
        st = _BREAKER.setdefault(source, {"failures": 0, "opened_at": None})
        if ok:
            st["failures"] = 0
            return
        st["failures"] += 1
        if st["failures"] >= INDEPENDENT_BREAKER_FAILURES and st["opened_at"] is None:
            st["opened_at"] = _time.monotonic()
            logging.warning(
                f"independent: circuit breaker OPEN for {source} after "
                f"{st['failures']} consecutive failures — suppressing calls for "
                f"{INDEPENDENT_BREAKER_COOLDOWN_SECONDS / 60:.0f}min. The veto "
                f"gate fails open while this is set."
            )


def reset_state():
    """Clear caches and breakers. For tests and for a manual re-probe."""
    with _CACHE_LOCK:
        _CACHE.clear()
    with _GRIDPOINT_LOCK:
        _GRIDPOINT_CACHE.clear()
    with _BREAKER_LOCK:
        _BREAKER.clear()


# --- Parsing helpers --------------------------------------------------------

def _c_to_f(c):
    return c * 9.0 / 5.0 + 32.0


def _extreme_for_day(by_day, target_date, is_high):
    """(state, value) for one local calendar day from {date: [temps_f]}.

    Returns NO_DATA when the provider simply does not cover the target date —
    a horizon gap is a coverage fact, not a failure. Never returns a number
    from an empty series."""
    vals = by_day.get(target_date) or []
    if not vals:
        return NO_DATA, None
    return DATA, (max(vals) if is_high else min(vals))


# --- NWS --------------------------------------------------------------------

def _nws_gridpoint(city_key, lat, lon):
    """(hourly_forecast_url, state, detail). Cached indefinitely once resolved.

    NWS point forecasts are HUMAN-EDITED: a forecaster reviews model output and
    adjusts it. That is the reason this provider is worth two calls — it is the
    only source available that can disagree with the ensemble for a reason the
    ensemble structurally cannot represent."""
    with _GRIDPOINT_LOCK:
        hit = _GRIDPOINT_CACHE.get(city_key)
    if hit:
        return hit, DATA, "cached gridpoint"

    url = NWS_POINTS_URL.format(lat=lat, lon=lon)
    try:
        r = _get_session().get(url, timeout=INDEPENDENT_TIMEOUT_SECONDS)
    except Exception as e:
        return None, INCONCLUSIVE, f"points request failed: {type(e).__name__}"

    # 404 is DEFINITIVE and means out of domain — verified 2026-08-06: Tokyo and
    # London both return 404 from /points while NYC returns 200. It is the NWS
    # analogue of Open-Meteo's 400, and it must NOT be lumped in with the 5xx
    # and timeout cases below, or an unserved city would sit permanently in
    # INCONCLUSIVE and be indistinguishable from an outage.
    if r.status_code == 404:
        return None, NO_DATA, "out of NWS domain (404)"
    if r.status_code != 200:
        return None, INCONCLUSIVE, f"points HTTP {r.status_code}"
    try:
        props = r.json().get("properties") or {}
    except ValueError:
        # An HTML error page is not JSON. This is the exact shape that produced
        # the coverage matrix's third wrong conclusion.
        return None, INCONCLUSIVE, "points body was not JSON"

    hourly_url = props.get("forecastHourly")
    if not hourly_url:
        return None, INCONCLUSIVE, "points response had no forecastHourly"

    with _GRIDPOINT_LOCK:
        _GRIDPOINT_CACHE[city_key] = hourly_url
    return hourly_url, DATA, ""


def _fetch_nws(city_key, station, target_date, is_high):
    hourly_url, state, detail = _nws_gridpoint(city_key, station["lat"], station["lon"])
    if hourly_url is None:
        return IndependentForecast(state, SOURCE_NWS, detail=detail)

    try:
        r = _get_session().get(hourly_url, timeout=INDEPENDENT_TIMEOUT_SECONDS)
    except Exception as e:
        return IndependentForecast(INCONCLUSIVE, SOURCE_NWS,
                                   detail=f"hourly request failed: {type(e).__name__}")
    if r.status_code == 404:
        return IndependentForecast(NO_DATA, SOURCE_NWS, detail="hourly 404")
    if r.status_code != 200:
        return IndependentForecast(INCONCLUSIVE, SOURCE_NWS,
                                   detail=f"hourly HTTP {r.status_code}")
    try:
        periods = ((r.json().get("properties") or {}).get("periods")) or []
    except ValueError:
        return IndependentForecast(INCONCLUSIVE, SOURCE_NWS,
                                   detail="hourly body was not JSON")
    if not periods:
        return IndependentForecast(INCONCLUSIVE, SOURCE_NWS,
                                   detail="hourly had no periods")

    # startTime carries the station's LOCAL offset ("2026-08-06T01:00:00-04:00"),
    # so startTime[:10] is the local calendar date — the same trick
    # weather._aggregate_local_days uses on Open-Meteo's timezone=auto output,
    # and the window every market actually settles on per the 2026-08-05 audit.
    # No timezone conversion needed, and none should be added: converting would
    # introduce a second day-boundary rule alongside the one the resolver uses.
    by_day = {}
    for p in periods:
        t = p.get("startTime")
        temp = p.get("temperature")
        if not t or temp is None:
            continue
        if p.get("temperatureUnit") == "C":
            temp = _c_to_f(temp)
        elif p.get("temperatureUnit") != "F":
            continue  # unknown unit — drop rather than guess
        by_day.setdefault(t[:10], []).append(float(temp))

    state, value = _extreme_for_day(by_day, target_date, is_high)
    if state != DATA:
        return IndependentForecast(NO_DATA, SOURCE_NWS,
                                   detail=f"no hourly coverage for {target_date}")
    return IndependentForecast(DATA, SOURCE_NWS, value_f=value,
                               fetched_at=datetime.now(timezone.utc).isoformat())


# --- UK Met Office DataHub --------------------------------------------------

def _fetch_datahub(city_key, station, target_date, is_high, tz_name):
    """UKMO Global Spot hourly, aggregated to the station's local day.

    UKMO's own operational output: a tier-one global model, genuinely
    independent of ECMWF, and the member most conspicuously absent from the
    ensemble.

    NOTE: unlike NWS, DataHub timestamps are UTC, so the local calendar day must
    be derived with the station's IANA timezone (from metar.STATION_ICAO). This
    is the one place in the veto path where a day-boundary bug could hide, so
    the conversion is explicit and the timezone comes from the same table METAR
    reads — reader and resolver stay on one rule."""
    if not METOFFICE_DATAHUB_KEY:
        # A missing key is NOT a coverage gap. NO_DATA would assert that UKMO
        # serves nothing at this city, which is false and would quietly become
        # "checked, found nothing" in the 14-day review.
        return IndependentForecast(INCONCLUSIVE, SOURCE_DATAHUB,
                                   detail="METOFFICE_DATAHUB_KEY not configured")
    if not tz_name:
        return IndependentForecast(INCONCLUSIVE, SOURCE_DATAHUB,
                                   detail=f"no IANA timezone known for {city_key}")

    params = {
        "latitude": station["lat"],
        "longitude": station["lon"],
        "excludeParameterMetadata": "true",
        "includeLocationName": "false",
    }
    try:
        r = _get_session().get(
            DATAHUB_URL, params=params,
            headers={"apikey": METOFFICE_DATAHUB_KEY, "accept": "application/json"},
            timeout=INDEPENDENT_TIMEOUT_SECONDS,
        )
    except Exception as e:
        return IndependentForecast(INCONCLUSIVE, SOURCE_DATAHUB,
                                   detail=f"request failed: {type(e).__name__}")

    # 400 is DEFINITIVE out-of-domain, matching the Open-Meteo convention
    # established in coverage_matrix.py. 401/403 are NOT: a rejected key says
    # nothing about coverage, and treating it as absence is how a rotated
    # credential would silently disable the gate while looking like a clean
    # negative result.
    if r.status_code == 400:
        return IndependentForecast(NO_DATA, SOURCE_DATAHUB, detail="out of domain (400)")
    if r.status_code != 200:
        return IndependentForecast(INCONCLUSIVE, SOURCE_DATAHUB,
                                   detail=f"HTTP {r.status_code}")
    try:
        payload = r.json()
    except ValueError:
        return IndependentForecast(INCONCLUSIVE, SOURCE_DATAHUB,
                                   detail="body was not JSON")

    try:
        features = payload.get("features") or []
        series = (features[0].get("properties") or {}).get("timeSeries") or [] if features else []
    except (AttributeError, IndexError, TypeError):
        return IndependentForecast(INCONCLUSIVE, SOURCE_DATAHUB,
                                   detail="unexpected payload shape")
    if not series:
        return IndependentForecast(INCONCLUSIVE, SOURCE_DATAHUB,
                                   detail="empty timeSeries")

    try:
        zone = ZoneInfo(tz_name)
    except Exception:
        return IndependentForecast(INCONCLUSIVE, SOURCE_DATAHUB,
                                   detail=f"unknown timezone {tz_name}")

    by_day = {}
    parsed_any = False
    for pt in series:
        t = pt.get("time")
        c = pt.get("screenTemperature")
        if not t or c is None:
            continue
        try:
            when = datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone(zone)
            by_day.setdefault(when.strftime("%Y-%m-%d"), []).append(_c_to_f(float(c)))
            parsed_any = True
        except (ValueError, TypeError):
            continue

    if not parsed_any:
        # 200 with a series we could not read a single point from. That is a
        # parse failure, not a coverage fact — INCONCLUSIVE.
        return IndependentForecast(INCONCLUSIVE, SOURCE_DATAHUB,
                                   detail="no parseable screenTemperature points")

    state, value = _extreme_for_day(by_day, target_date, is_high)
    if state != DATA:
        return IndependentForecast(NO_DATA, SOURCE_DATAHUB,
                                   detail=f"no coverage for {target_date}")
    return IndependentForecast(DATA, SOURCE_DATAHUB, value_f=value,
                               fetched_at=datetime.now(timezone.utc).isoformat())


# --- Public entry point -----------------------------------------------------

def get_independent_forecast(city_name, target_date, is_high):
    """The second opinion for one (city, date, direction). Never raises.

    Never raises is load-bearing, not defensive politeness: this runs inside
    trade evaluation, and an exception escaping here would stop the bot placing
    trades because a third-party weather API had a bad minute. Any unexpected
    failure resolves to INCONCLUSIVE, which fails OPEN — the trade proceeds
    exactly as it would have before this module existed."""
    try:
        return _get_independent_forecast_inner(city_name, target_date, is_high)
    except Exception as e:
        logging.warning(
            f"independent: unexpected failure for {city_name} {target_date} "
            f"is_high={is_high}: {e}", exc_info=True)
        return IndependentForecast(INCONCLUSIVE, "unknown",
                                   detail=f"unhandled {type(e).__name__}")


def _get_independent_forecast_inner(city_name, target_date, is_high):
    from weather import get_station_coords
    from metar import STATION_ICAO

    city_key, station = get_station_coords(city_name)
    if not station:
        return IndependentForecast(INCONCLUSIVE, "unknown",
                                   detail=f"no station mapping for {city_name}")

    key = (city_key, target_date, bool(is_high))
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
    if hit:
        age = _time.monotonic() - hit[0]
        if age < INDEPENDENT_CACHE_TTL_SECONDS:
            return hit[1]
        # Past TTL the entry is DROPPED, not returned-and-refreshed. A stale
        # cached temperature served past its TTL is one of the shapes of "an
        # error path produced a number" that §7 asserts against.
        with _CACHE_LOCK:
            _CACHE.pop(key, None)

    source = provider_for(city_key)
    if _breaker_open(source):
        return IndependentForecast(INCONCLUSIVE, source,
                                   detail="circuit breaker open")

    if source == SOURCE_NWS:
        res = _fetch_nws(city_key, station, target_date, is_high)
    else:
        tz_name = (STATION_ICAO.get(city_key) or (None, None))[1]
        res = _fetch_datahub(city_key, station, target_date, is_high, tz_name)

    # A configuration gap is not a provider failure — an unset DataHub key would
    # otherwise trip the breaker on the first three cities and then log an
    # outage that is really an empty env var.
    if "not configured" not in res.detail:
        _breaker_record(source, res.state != INCONCLUSIVE)

    if res.state == INCONCLUSIVE:
        logging.warning(
            f"independent: INCONCLUSIVE for {city_key} {target_date} "
            f"is_high={is_high} via {source} — {res.detail}. Gate fails open."
        )
    else:
        with _CACHE_LOCK:
            _CACHE[key] = (_time.monotonic(), res)
    return res


# --- The tripwire -----------------------------------------------------------
# Stands in for the shadow period the owner decision deliberately skipped, and
# is therefore not optional. A gate meant to catch RARE blunders that fires on a
# quarter of real trade candidates is itself the blunder — so it takes itself
# out of the decision rather than continuing to stop the book on what is almost
# certainly an artefact (a mismatched station, or a provider returning a
# different settlement window than expected).
#
# Latching, deliberately. Once tripped it stays tripped until the process
# restarts, even if the rate falls back under the threshold. An auto-re-arming
# gate would oscillate straight back into the same storm, and each oscillation
# costs real refused trades. Re-arming is a human action after a human has
# looked at why it fired.
_TRIPPED = False
_TRIP_LOCK = threading.Lock()
_last_stats = None


def veto_armed():
    """Whether the veto may actually REFUSE a trade right now.

    False does not stop the counterfactual being computed and logged — that is
    the whole design of §5c, and it is what makes the 14-day review possible on
    a gate that turned itself off."""
    if not _veto_enabled():
        return False
    with _TRIP_LOCK:
        return not _TRIPPED


def _veto_enabled():
    # Read through the module, not `from config import`, so a test (or a future
    # runtime toggle) that rebinds the constant is honoured rather than silently
    # ignored by an import-time copy — the PAPER_MODE lesson.
    import config
    return bool(config.INDEPENDENT_VETO_ENABLED)


def tripwire_state():
    """(tripped, last_stats) for the dashboard and the report."""
    with _TRIP_LOCK:
        return _TRIPPED, _last_stats


def reset_tripwire():
    """Re-arm the gate. Manual action, and for tests."""
    global _TRIPPED, _last_stats
    with _TRIP_LOCK:
        _TRIPPED = False
        _last_stats = None


def check_tripwire():
    """Evaluate the rolling fire rate and city concentration; disable if needed.

    Called once per scan cycle, not per evaluation: it is a DB aggregate, and
    running it inside the eval loop would put a table scan on the trading path
    for a number that cannot meaningfully change between two markets in the same
    cycle.

    Never raises — a failure to MEASURE the gate must not disable the bot, and
    must not disable the gate either (that would let a broken query silently
    remove a safety check)."""
    global _TRIPPED, _last_stats
    try:
        from db import independent_veto_stats
        import config
        from alerts import add_notification_safe

        stats = independent_veto_stats(hours=24)
        with _TRIP_LOCK:
            _last_stats = stats
            already = _TRIPPED

        if stats["considered"] < config.INDEPENDENT_VETO_MIN_SAMPLE:
            return stats

        # City concentration is checked whether or not the rate tripped. It is
        # the station-mismatch signature and it is the single most valuable
        # thing this gate can tell us — the correct response is to FIX THE
        # STATION, never to loosen the threshold. Hong Kong, Moscow and Seoul
        # were all this bug, each found only after losing money.
        if (stats["fired"] and
                stats["top_city_share"] > config.INDEPENDENT_VETO_CITY_CONCENTRATION):
            logging.error(
                f"INDEPENDENT VETO CONCENTRATED IN ONE CITY: {stats['top_city']} "
                f"accounts for {stats['top_city_share']:.0%} of "
                f"{stats['fired']} vetoes in the last {stats['window_hours']}h. "
                f"That is a station-mismatch signature. Check "
                f"STATIONS['{stats['top_city']}'] against the market's own "
                f"resolution text before touching DISAGREEMENT_VETO_F."
            )
            add_notification_safe(
                "error",
                f"Independent veto concentrated in {stats['top_city']} "
                f"({stats['top_city_share']:.0%} of vetoes) — likely wrong station.",
                severity="error")

        if stats["fire_rate"] > config.INDEPENDENT_VETO_MAX_FIRE_RATE and not already:
            with _TRIP_LOCK:
                _TRIPPED = True
            logging.error(
                f"INDEPENDENT VETO AUTO-DISABLED: fired on {stats['fired']}/"
                f"{stats['considered']} ({stats['fire_rate']:.0%}) of gate-passing "
                f"signals in the last {stats['window_hours']}h, over the "
                f"{config.INDEPENDENT_VETO_MAX_FIRE_RATE:.0%} tripwire. A blunder "
                f"detector firing this often is not detecting blunders. The gate "
                f"will keep LOGGING its counterfactual but will no longer refuse "
                f"trades, and stays disabled until the process is restarted. "
                f"By city: {stats['by_city']}"
            )
            add_notification_safe(
                "error",
                f"Independent veto auto-disabled: {stats['fire_rate']:.0%} fire "
                f"rate over 24h ({stats['fired']}/{stats['considered']}).",
                severity="error")
        return stats
    except Exception as e:
        logging.error(f"independent: tripwire check failed: {e}", exc_info=True)
        return None


# --- The gate ---------------------------------------------------------------

def _bucket_overlaps_band(bucket_low, bucket_high, lo_band, hi_band):
    """Does the bucket intersect the independent forecast's plausible band?

    STRICT inequalities, so a bucket merely ADJACENT to the band — sharing a
    single boundary value with it — does not fire. An open-ended bucket
    ("above X" / "below X") is treated as the half-line it actually is; reading
    a missing bound as 0.0 would put every above-bucket's lower edge at the
    freezing point of nothing in particular."""
    if bucket_low is None and bucket_high is None:
        return False
    if bucket_low is None:                    # (-inf, high]
        return lo_band < bucket_high
    if bucket_high is None:                   # [low, +inf)
        return hi_band > bucket_low
    if bucket_low == bucket_high:             # exact bucket: a point
        return lo_band < bucket_low < hi_band
    return bucket_low < hi_band and bucket_high > lo_band


def evaluate_veto(city, target_date, is_high, ensemble_mean,
                  bucket_low, bucket_high):
    """The independent second opinion on one NO bet, as a flat record.

    Returns a dict that is BOTH the gate input and the stored counterfactual:

      state / source / value / fetched_at / detail   what the provider said
      disagreement_f                                 |independent - ensemble|
      veto_gross / veto_band                         what the veto CONCLUDED
      vetoed                                         what it will actually DO
      armed                                          whether it may refuse

    veto_* and `vetoed` are separate fields on purpose. When the tripwire has
    disabled the gate, or the provider returned NO_DATA, the conclusion is still
    recorded and only the effect is suppressed. That separation is the entire
    value of §5c: it is what lets "was the veto right?" be answered from stored
    rows once settled_value populates, including for the trades it refused and
    the ones it would have refused but didn't.

    NEVER adjusts a probability. The independent value does not enter
    ensemble_mean, sigma, model_spread, model_agreement or model_count, and must
    not be made to — blending would turn a veto into an unmeasured fifth model
    with no bias correction and no place in the family caps."""
    import config

    fc = get_independent_forecast(city, target_date, is_high)
    armed = veto_armed()

    row = {
        "independent_source": fc.source,
        "independent_state": fc.state,
        "independent_value": fc.value_f,
        "independent_fetched_at": fc.fetched_at,
        "independent_detail": fc.detail or None,
        "disagreement_f": None,
        "veto_gross": False,
        "veto_band": False,
        "vetoed": False,
        "armed": armed,
    }

    # ONLY `DATA` MAY EVER REFUSE A TRADE. NO_DATA is a coverage gap and
    # INCONCLUSIVE is an error; neither is evidence of disagreement, and
    # treating either as one is how a rate limit becomes a halted book.
    if not fc.is_data or ensemble_mean is None:
        return row

    disagreement = abs(fc.value_f - ensemble_mean)
    row["disagreement_f"] = disagreement
    row["veto_gross"] = disagreement > config.DISAGREEMENT_VETO_F

    lo_band = fc.value_f - config.PLAUSIBLE_BAND_F
    hi_band = fc.value_f + config.PLAUSIBLE_BAND_F
    row["veto_band"] = _bucket_overlaps_band(bucket_low, bucket_high,
                                             lo_band, hi_band)

    row["vetoed"] = bool(armed and row["veto_gross"])
    return row


def veto_gate_rows(veto):
    """The two structured gate rows, in the shape _no_side_gates uses.

    `passed` reflects what the gate DID, so replay_gates keeps meaning "why was
    this trade refused". A gate the tripwire has disabled passes here while
    veto_gross/veto_band on the signal row still record that it would have
    fired — the counterfactual lives in the signal row, the decision lives
    here, and neither can be mistaken for the other."""
    import config

    src = veto.get("independent_source") or "none"
    val = veto.get("independent_value")
    state = veto.get("independent_state")
    dis = veto.get("disagreement_f")
    armed = veto.get("armed")
    val_s = "n/a" if val is None else f"{val:.1f}°F"
    suffix = "" if armed else " [gate auto-disabled by tripwire — logged only]"

    gross_fired = bool(veto.get("veto_gross") and armed)
    band_fired = False  # Independent bucket band veto disabled by owner decision 2026-08-07

    return [
        {"gate": "independent_gross_disagreement",
         "observed": dis, "threshold": config.DISAGREEMENT_VETO_F,
         "passed": not gross_fired,
         "detail": (f"independent forecast ({src}) says {val_s}, "
                    f"{(dis or 0):.1f}°F from the ensemble — over the "
                    f"{config.DISAGREEMENT_VETO_F}°F blunder threshold. "
                    f"Suspect wrong station, stale run or misparsed bucket"
                    f"{suffix}")},
        {"gate": "independent_bucket_band",
         # `observed` is the independent VALUE here, not the disagreement: this
         # condition is about where the second opinion sits relative to the
         # bucket, and storing the disagreement again would make the two rows
         # indistinguishable in a query.
         "observed": val, "threshold": config.PLAUSIBLE_BAND_F,
         "passed": not band_fired,
         "detail": (f"independent forecast ({src}, state={state}) says {val_s}; "
                    f"the bucket falls inside its ±{config.PLAUSIBLE_BAND_F}°F "
                    f"plausible band, so the outcome being bet against is live"
                    f"{suffix}")},
    ]


def prefetch_independent(opportunities):
    """Warm the cache for a scan's opportunities, sequentially.

    Called from the scan cycle so the eval loop hits cache rather than paying a
    round trip inside trade evaluation. Purely an optimisation: every consumer
    goes through get_independent_forecast, which fetches on demand, so a
    prefetch failure costs latency and nothing else."""
    keys = {(opp.city, opp.date, bool(opp.is_high)) for opp in opportunities}
    states = {DATA: 0, NO_DATA: 0, INCONCLUSIVE: 0}
    for city, date, is_high in sorted(keys):
        res = get_independent_forecast(city, date, is_high)
        states[res.state] = states.get(res.state, 0) + 1
    logging.info(
        f"Independent prefetch: {len(keys)} keys → {states[DATA]} DATA, "
        f"{states[NO_DATA]} NO_DATA, {states[INCONCLUSIVE]} INCONCLUSIVE"
    )
    return states
