"""Portfolio-level correlated risk limits.

51 cities, one atmosphere. `MAX_CONCURRENT_POSITIONS` caps how MANY positions
are open and `ONE_TRADE_PER_CITY_DATE` stops two buckets of the same city-day
being counted as two bets. Neither limits the thing that actually concentrates
risk here: a single synoptic system covering several cities at once. Four
independent-looking NO bets across Dallas, Austin, Houston and Atlanta on one
target date are one bet on one ridge, sized 4x.

That is not hypothetical. The open book on 2026-08-05 held Dallas and Austin,
same target date, both high-bucket NO — two cities 200km apart under the same
Texas heat ridge, each sized as though it were an independent position.

Two limits, because there are two different correlations:

    GROUP  — cities under one synoptic system on one target date. Regional.
    DIRECTION — every position that loses on the same SIGN of temperature
              surprise, on one target date, across all groups. A hemispheric
              heat event correlates cities that share no synoptic system at all.

Both are expressed in STAKES rather than dollars, the same way DAILY_LOSS_STAKES
is, so raising the stake does not silently loosen them.

Deliberately loose to begin with. The point is that the ceiling exists before it
is needed — a cap introduced during the heat wave it was meant to survive is a
cap introduced too late — not that it binds on today's flow.
"""
import logging

# --- Synoptic groups -------------------------------------------------------
# Coarser than a city, finer than the model-weight region. The unit is roughly
# "one air mass": cities that a single trough, ridge or monsoon surge tends to
# cover together.
#
# Deviations from the twelve suggested groups, and why:
#
#  * East Asia is split three ways. As one group it would have held 13 of the 51
#    cities, and Beijing and Hong Kong are 2,000km apart across a monsoon
#    boundary — they are almost never under one system, so lumping them would
#    make the cap bind on uncorrelated positions while still permitting four
#    genuinely correlated South China bets. South China gets its own group
#    because that is where this bot has actually lost money to correlated tail
#    events (Guangzhou twice, Hong Kong twice).
#  * Central America is added. Mexico City (2,200m, subtropical highland) and
#    Panama (9°N, tropical) belong to neither the US groups nor South America.
#  * Turkey sits in Eastern Europe rather than the Middle East, matching the
#    Anatolian/Balkan systems that actually drive it — and matching the "EU"
#    model region already assigned to both cities.
#
# Every name here must exist in weather.STATIONS; validate_synoptic_groups()
# enforces that at boot, because a typo would silently exempt a city from both
# caps rather than raising.
SYNOPTIC_GROUPS = {
    # North America
    "NYC": "US_EAST",
    "New York": "US_EAST",
    "Chicago": "US_EAST",
    "Toronto": "US_EAST",
    "Miami": "US_SOUTH",
    "Dallas": "US_SOUTH",
    "Austin": "US_SOUTH",
    "Houston": "US_SOUTH",
    "Atlanta": "US_SOUTH",
    "Los Angeles": "US_WEST",
    "San Francisco": "US_WEST",
    "Seattle": "US_WEST",
    "Denver": "US_WEST",
    "Mexico City": "CENTRAL_AMERICA",
    "Panama": "CENTRAL_AMERICA",
    # South America
    "Buenos Aires": "SOUTH_AMERICA",
    "Sao Paulo": "SOUTH_AMERICA",
    # Europe
    "London": "WESTERN_EUROPE",
    "Paris": "WESTERN_EUROPE",
    "Amsterdam": "WESTERN_EUROPE",
    "Berlin": "WESTERN_EUROPE",
    "Munich": "WESTERN_EUROPE",
    "Madrid": "WESTERN_EUROPE",
    "Milan": "WESTERN_EUROPE",
    "Helsinki": "EASTERN_EUROPE",
    "Warsaw": "EASTERN_EUROPE",
    "Moscow": "EASTERN_EUROPE",
    "Istanbul": "EASTERN_EUROPE",
    "Ankara": "EASTERN_EUROPE",
    # Middle East / Africa
    "Tel Aviv": "MIDDLE_EAST",
    "Jeddah": "MIDDLE_EAST",
    "Lagos": "AFRICA",
    "Cape Town": "AFRICA",
    # East Asia — see the note above on the three-way split
    "Tokyo": "EAST_ASIA_NORTH",
    "Seoul": "EAST_ASIA_NORTH",
    "Busan": "EAST_ASIA_NORTH",
    "Beijing": "EAST_ASIA_NORTH",
    "Qingdao": "EAST_ASIA_NORTH",
    "Shanghai": "CHINA_CENTRAL",
    "Wuhan": "CHINA_CENTRAL",
    "Chengdu": "CHINA_CENTRAL",
    "Chongqing": "CHINA_CENTRAL",
    "Guangzhou": "SOUTH_CHINA",
    "Shenzhen": "SOUTH_CHINA",
    "Hong Kong": "SOUTH_CHINA",
    "Taipei": "SOUTH_CHINA",
    # Southeast Asia / South Asia / Oceania
    "Singapore": "SOUTHEAST_ASIA",
    "Kuala Lumpur": "SOUTHEAST_ASIA",
    "Jakarta": "SOUTHEAST_ASIA",
    "Manila": "SOUTHEAST_ASIA",
    "Lucknow": "SOUTH_ASIA",
    # "Karachi": "SOUTH_ASIA",   # re-add with the station, see weather.STATIONS
    "Wellington": "OCEANIA",
}


def synoptic_group(city):
    """Group for a city name, or None if it is not mapped.

    Accepts the loose market-supplied name as well as the canonical station key,
    matching get_station_coords' longest-prefix rule so "Highest temperature in
    New York City" resolves the same way here as it does for the forecast."""
    if not city:
        return None
    if city in SYNOPTIC_GROUPS:
        return SYNOPTIC_GROUPS[city]
    lowered = city.lower()
    for key in sorted(SYNOPTIC_GROUPS, key=len, reverse=True):
        if key.lower() in lowered:
            return SYNOPTIC_GROUPS[key]
    return None


def risk_direction(side, bucket_low, bucket_high, ensemble_mean):
    """Which SIGN of temperature surprise loses this position money.

    "HOT" — the position loses if the day comes in warmer than forecast.
    "COLD" — it loses if the day comes in colder.
    None — undeterminable (missing bucket or mean); the caller must not silently
    treat that as either.

    This, not is_high, is the axis a heat wave correlates. A NO bet on a bucket
    ABOVE the forecast mean is a bet that it will not get that hot, and it busts
    on a hot surprise — whether the market is a daily max or a daily min. Two NO
    bets can both be on "highest temperature" markets and be exposed in
    OPPOSITE directions, which is why grouping by is_high would pool positions
    that hedge each other and split positions that compound.
    """
    if ensemble_mean is None:
        return None
    if bucket_low is None and bucket_high is None:
        return None

    if bucket_low is not None and bucket_high is not None:
        midpoint = (bucket_low + bucket_high) / 2.0
        bucket_is_above = midpoint > ensemble_mean
    elif bucket_low is not None:
        bucket_is_above = True      # "above X" / "X or higher"
    else:
        bucket_is_above = False     # "below X" / "X or lower"

    if side == "NO":
        # Betting the temperature MISSES the bucket. A bucket above the forecast
        # is missed by staying cool, so warmth is what breaks it.
        return "HOT" if bucket_is_above else "COLD"
    # YES is disabled in strategy.py, but the mapping is the exact inverse and
    # defining it here keeps this function total rather than partial.
    return "COLD" if bucket_is_above else "HOT"


def _stake_cap(stakes):
    """Convert a cap expressed in stakes into dollars at the CURRENT stake.

    Read at check time, not at import: the stake is dashboard-tunable, and a cap
    frozen at boot would keep enforcing the old stake's dollar ceiling."""
    from config import effective_stake
    return stakes * effective_stake()


def group_exposure(open_positions, group, target_date):
    """Open dollars in one synoptic group on one target date."""
    return sum(p["size_usdc"] or 0.0 for p in open_positions
               if p.get("target_date") == target_date
               and synoptic_group(p.get("city")) == group)


def direction_exposure(open_positions, direction, target_date):
    """Open dollars exposed to the same sign of temperature surprise on one
    target date, across every group.

    Positions whose direction could not be determined are EXCLUDED rather than
    assumed harmless — but see check_correlation_limits, which reports them, so
    an exclusion cannot quietly hollow out the cap."""
    return sum(p["size_usdc"] or 0.0 for p in open_positions
               if p.get("target_date") == target_date
               and p.get("risk_direction") == direction)


def check_correlation_limits(open_positions, city, target_date, size, direction):
    """May this trade be opened without breaching a correlated-exposure cap?

    Returns (allowed: bool, reason: str|None, detail: dict). `detail` always
    carries the group, both exposures and both caps, so the refusal log answers
    "what bound, and by how much" without a follow-up query.
    """
    from config import (ENABLE_CORRELATION_LIMITS, MAX_GROUP_STAKES_PER_DATE,
                        MAX_DIRECTION_STAKES_PER_DATE)

    group = synoptic_group(city)
    group_now = group_exposure(open_positions, group, target_date) if group else 0.0
    dir_now = direction_exposure(open_positions, direction, target_date) if direction else 0.0
    group_cap = _stake_cap(MAX_GROUP_STAKES_PER_DATE)
    dir_cap = _stake_cap(MAX_DIRECTION_STAKES_PER_DATE)
    unknown = sum(1 for p in open_positions
                  if p.get("target_date") == target_date and not p.get("risk_direction"))

    detail = {
        "group": group, "target_date": target_date, "size": size,
        "direction": direction,
        "group_exposure": group_now, "group_cap": group_cap,
        "direction_exposure": dir_now, "direction_cap": dir_cap,
        "positions_with_unknown_direction": unknown,
    }

    if not ENABLE_CORRELATION_LIMITS:
        return True, None, detail

    # An unmapped city is a configuration gap, not a licence to concentrate.
    # Logged at warning so it gets fixed, but not blocked: refusing to trade a
    # city because a lookup table is incomplete is the wrong failure direction
    # for a gap that validate_synoptic_groups() already catches at boot.
    if group is None:
        logging.warning(
            f"CORRELATION | {city} is not in SYNOPTIC_GROUPS — group cap not "
            f"applied. Add it to risk.SYNOPTIC_GROUPS."
        )
    elif group_now + size > group_cap:
        return False, (
            f"Synoptic group cap: {group} already holds ${group_now:.2f} open on "
            f"{target_date}; ${size:.2f} more would reach "
            f"${group_now + size:.2f} > ${group_cap:.2f} "
            f"({MAX_GROUP_STAKES_PER_DATE:g} stakes). One system covers this "
            f"whole group — these are not independent bets."
        ), detail

    if direction and dir_now + size > dir_cap:
        return False, (
            f"Same-direction cap: ${dir_now:.2f} already exposed to a {direction} "
            f"surprise on {target_date} across all groups; ${size:.2f} more would "
            f"reach ${dir_now + size:.2f} > ${dir_cap:.2f} "
            f"({MAX_DIRECTION_STAKES_PER_DATE:g} stakes)."
        ), detail

    return True, None, detail


def backfill_risk_direction():
    """Classify already-open positions that predate the risk_direction column.

    Without this the same-direction cap is hollow for as long as the pre-change
    book takes to turn over — days, under hold-to-settlement — which is exactly
    the window the change is supposed to be watched in.

    The mean has to be the one that PRICED the bucket, not today's forecast, so
    it comes from replay_signals (which records ensemble_mean per evaluation)
    joined on market_id, taking the earliest row for that market — the
    evaluation closest to the entry decision. Positions with no replay row stay
    NULL; a guess would be worse than an honest unknown, and
    check_correlation_limits already reports the count.

    Returns the number of rows classified."""
    from db import execute_query, fetch_query

    rows = fetch_query("""
        SELECT p.id, p.side, p.market_id,
               (SELECT r.ensemble_mean FROM replay_signals r
                 WHERE r.market_id = p.market_id AND r.ensemble_mean IS NOT NULL
                 ORDER BY r.id ASC LIMIT 1) AS mean,
               (SELECT r.bucket_low FROM replay_signals r
                 WHERE r.market_id = p.market_id AND r.ensemble_mean IS NOT NULL
                 ORDER BY r.id ASC LIMIT 1) AS lo,
               (SELECT r.bucket_high FROM replay_signals r
                 WHERE r.market_id = p.market_id AND r.ensemble_mean IS NOT NULL
                 ORDER BY r.id ASC LIMIT 1) AS hi
          FROM positions p
         WHERE p.risk_direction IS NULL
    """)
    done = 0
    for r in rows:
        d = risk_direction(r["side"], r["lo"], r["hi"], r["mean"])
        if d:
            execute_query("UPDATE positions SET risk_direction=? WHERE id=?", (d, r["id"]))
            execute_query("UPDATE trades SET risk_direction=? "
                          "WHERE market_id=? AND risk_direction IS NULL",
                          (d, r["market_id"]))
            done += 1
    if rows:
        logging.info(
            f"risk_direction backfill: {done}/{len(rows)} open position(s) classified"
            + ("" if done == len(rows) else
               " — the rest have no replay row and stay unknown by design")
        )
    return done


def validate_synoptic_groups():
    """Every city in STATIONS must have a group, and every group key must name a
    real station. Returns a list of problems; empty means clean.

    Both directions matter and fail differently. A station with no group is
    exempt from both caps and trades unconstrained; a group entry naming no
    station is a typo that silently exempts the city it was meant to cover.
    Neither says anything at runtime — the same quiet-failure shape as the
    "Tampa" entry that sat in two config tables for weeks without applying."""
    from weather import STATIONS
    problems = []
    for missing in sorted(set(STATIONS) - set(SYNOPTIC_GROUPS)):
        problems.append(f"STATIONS has '{missing}' with no SYNOPTIC_GROUPS entry "
                        f"(it would be exempt from the correlated-exposure caps)")
    for extra in sorted(set(SYNOPTIC_GROUPS) - set(STATIONS)):
        problems.append(f"SYNOPTIC_GROUPS names '{extra}', which is not in STATIONS "
                        f"(its group has never applied)")
    return problems
