"""The discrete values a market can actually settle on.

A temperature market does not settle on a real number. It settles on a reading a
station published, and stations publish on a grid. Two consequences:

  SETTLEMENT. Rounding a reading onto the wrong grid injects error. metar.py
      rounded every city to a whole °C, which is right for most of the world and
      wrong for North America, where ASOS reports whole °F: 27.78°C is exactly
      82°F, rounds to 28°C, and comes back as 82.4°F. On the 2°F-wide buckets US
      markets use, 0.9°F of spurious error decides outcomes.

  REACHABILITY. A bucket containing no grid point cannot pay YES, whatever the
      weather does. Those exist in principle — 44% of whole °F values are
      unreachable from a whole °C reading, so an "exactly 78°F" market against a
      °C-reporting station could never resolve YES.

In practice none has been seen, and the lattice explains why rather than leaving
it to luck: US markets quote °F against °F-reporting stations and the rest quote
°C against °C-reporting stations, so the market's unit is always the station's
unit and every bucket holds at least one reachable value. That makes a zero-
reachable bucket a strong signal of a PARSING bug rather than of free money,
which is why one is routed for manual review instead of traded.

Measured, not assumed — see audit_station_lattice.py and
reports/station-lattice.md.
"""
import logging
import math

# City -> "F" | "C" lives on the STATIONS table itself (weather.py). It is the
# unit the MARKET quotes in, because that is what the resolution source
# publishes and therefore what settles: US markets ask about "between 74-75°F",
# every other city about "11°C".
#
# Corroborated, not assumed. audit_station_lattice.py measured a week of
# observations at all 51 stations and the station's own reporting unit agrees
# with the market's unit at every station where the reporting unit is
# determinable — 40 °C, 10 °F. The single exception is KBKF (Denver), a military
# field whose observations sit on neither grid cleanly (22% whole °C, 31% whole
# °F, 14 distinct fractional parts); its markets quote °F like every other US
# city, so °F is what settles regardless of how the station itself reports.
# See reports/station-lattice.md.


def lattice_for_city(city_key):
    """"F" or "C" for a city, or None when it is not in STATIONS."""
    from weather import STATIONS
    st = STATIONS.get(city_key)
    return st.get("lattice") if st else None


def round_half_away(v):
    """Round half AWAY from zero (30.5 -> 31, -0.5 -> -1).

    Wunderground's whole-degree rollup convention. Python's round() is banker's
    rounding (30.5 -> 30), which mis-scores exactly the boundary readings the
    markets settle on. Duplicated from metar.py deliberately: this module must
    not import metar, which imports config, which is imported by everything."""
    return int(math.copysign(math.floor(abs(v) + 0.5), v))


def quantise_c(value_c, city_key):
    """Snap an observed °C reading onto the grid this city settles on, in °F.

    Takes °C because that is the unit the observation arrives in (IEM's `tmpc`),
    and rounding must happen in the SOURCE unit. Going °C -> °F -> °C to round
    reintroduces float error at exactly the half-degree boundaries the markets
    settle on: 24.5°C becomes 76.1°F, which converts back to 24.499999999999996
    and rounds DOWN to 24 instead of away to 25. That is a one-degree settlement
    error on the boundary readings, which is the same class of bug as the
    banker's-rounding issue round_half_away exists to prevent.

    Returns the plain conversion when the grid is unknown. That is the safe
    direction: an unquantised reading carries at most the instrument's own
    precision of error, while quantising onto the wrong grid adds up to half a
    grid step of error that is systematic rather than random."""
    lat = lattice_for_city(city_key)
    if lat == "C":
        return round_half_away(value_c) * 9.0 / 5.0 + 32.0
    f = value_c * 9.0 / 5.0 + 32.0
    if lat == "F":
        # Whole °F at source; this removes the float noise the °C round trip
        # through IEM introduced (82°F -> 27.78°C -> 82.004°F).
        return float(round_half_away(f))
    return f


def settleable_interval(bucket_low, bucket_high):
    """The °F interval that settles YES, from the STORED bucket bounds.

    This must stay identical to the padding weather.get_bucket_probability
    applies, or reachability would be computed over a different interval than
    the one being priced and would raise false impossible-bucket alarms. Each
    non-None bound moves outward by 0.5°F; an open end becomes unbounded.

    For a °C market that ±0.5°F pad is what reconstitutes the true °C rounding
    bin: parse_bucket deliberately returns "33°C" as (91.0, 91.8), which pads to
    [90.5, 92.3]°F = [32.5, 33.5]°C exactly."""
    lo = (bucket_low - 0.5) if bucket_low is not None else -200.0
    hi = (bucket_high + 0.5) if bucket_high is not None else 200.0
    return lo, hi


def reachable_values(bucket_low, bucket_high, city_key):
    """Every value the station could report that settles this bucket YES.

    Takes the STORED bucket bounds and pads them itself — see
    settleable_interval. Returns a sorted list, or None when the station's grid
    is unknown (which is not the same as an empty list — see star_tag)."""
    lat = lattice_for_city(city_key)
    if lat is None:
        return None
    lo, hi = settleable_interval(bucket_low, bucket_high)
    if lo > hi:
        return []

    out = []
    if lat == "F":
        for f in range(int(math.floor(lo)) - 1, int(math.ceil(hi)) + 2):
            if lo - 1e-9 <= f <= hi + 1e-9:
                out.append(float(f))
    else:
        c_lo = (lo - 32.0) * 5.0 / 9.0
        c_hi = (hi - 32.0) * 5.0 / 9.0
        for c in range(int(math.floor(c_lo)) - 1, int(math.ceil(c_hi)) + 2):
            f = c * 9.0 / 5.0 + 32.0
            if lo - 1e-9 <= f <= hi + 1e-9:
                out.append(f)
    return sorted(out)


def star_tag(bucket_low, bucket_high, city_key, market_id=None, question=None):
    """Flag a bucket that cannot settle YES on this station's grid.

    Returns (is_impossible, detail). `detail` carries the grid and the bounds so
    the dashboard and the log can say WHY without a second query.

    A firing here almost certainly means parse_bucket produced the wrong bounds,
    not that the market is free money — the 2026-06 Celsius zero-width bucket
    bug is exactly what this would have caught. So it routes for manual review
    and never auto-trades: acting on it would be betting real money on the
    proposition that our parser is right and the market maker is wrong, which is
    the wrong way round given the history."""
    values = reachable_values(bucket_low, bucket_high, city_key)
    detail = {"city": city_key, "lattice": lattice_for_city(city_key),
              "bucket_low": bucket_low, "bucket_high": bucket_high,
              "reachable": values, "market_id": market_id, "question": question}
    if values is None:
        return False, detail          # unknown grid: cannot claim impossibility
    if values:
        return False, detail
    logging.warning(
        f"IMPOSSIBLE_BUCKET | {market_id} | {question} | "
        f"[{bucket_low}, {bucket_high}]°F contains no reachable value on "
        f"{city_key}'s {lattice_for_city(city_key)} grid. This is far more likely a bucket-parsing bug "
        f"than free money — routed for manual review, NOT traded."
    )
    return True, detail
