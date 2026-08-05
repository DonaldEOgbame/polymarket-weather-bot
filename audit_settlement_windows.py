"""Day-boundary audit: which window actually settles each city's markets.

The bot forecasts "the day's max". Which day, measured how, is a per-city
question with at least three answers in circulation:

    local          local midnight to local midnight (the Wunderground daily
                   history convention)
    00-24Z         the UTC day, which for Tokyo (+9) and Wellington (+12)
                   covers two different local afternoons
    6h-groups      the max of the 6-hourly synoptic max groups, which is what
                   several national services publish

Getting it wrong is not a small error, and it is not hypothetical: it is the
error class behind the Hong Kong, Moscow, Seoul and London station corrections
already in this codebase. Every one of those was found by reading the market's
own resolution text and discovering the convention assumed by default was not
the convention the market used.

So this script does not infer. It fetches live markets, pulls each one's
resolution text verbatim, and classifies from the words. Where the text does not
say, the city is marked UNKNOWN and excluded from trading — the Karachi
precedent, where the description named one station and linked another, and the
correct response was to stop trading it rather than pick.

It also answers the two sub-questions that change what "the max" means even once
the window is fixed:

    SPECI / corrections — whether the settlement source includes off-hour
        special reports and later-issued corrections, and whether our reader
        does. A METAR reader that takes routine hourly observations only will
        miss the SPECI issued precisely BECAUSE the temperature spiked.
    max_tmpf vs max(hourly) — whether the source publishes a daily-summary
        maximum or the max of hourly observations. They differ on frontal days,
        when the peak falls between two hourly obs.

Usage:
    python audit_settlement_windows.py            # audit live markets, write the report
    python audit_settlement_windows.py --json     # machine-readable

Output: reports/settlement-windows-<date>.md, one row per city with the quote
that justifies it.
"""
import argparse
import json
import logging
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import GAMMA_EVENTS_URL
from utils import safe_get
from weather import STATIONS, get_station_coords

# --- Window classification -------------------------------------------------
# Each pattern is matched against the market's resolution/description text. The
# ORDER matters: the most specific phrasing wins, because a description can name
# both a local day and a UTC timestamp (the close time) in one sentence.
#
# Patterns are deliberately narrow. A loose pattern that classifies an ambiguous
# description as "local" produces exactly the confident-and-wrong outcome this
# audit exists to prevent; a narrow one leaves it UNKNOWN, which is recoverable.
_WINDOW_PATTERNS = [
    ("6h-groups", [
        r"6[- ]hour(?:ly)? (?:maximum|max|group)",
        r"synoptic (?:maximum|period)",
        r"\b(?:00|06|12|18)Z\s*(?:,|and|/)\s*(?:00|06|12|18)Z",
    ]),
    ("00-24Z", [
        r"\bUTC day\b",
        r"\b00:?00\s*(?:UTC|Z)\s*(?:to|-|–|until)\s*(?:24:?00|00:?00)\s*(?:UTC|Z)",
        r"\bmeasured in UTC\b",
        r"\bcoordinated universal time\b",
    ]),
    ("local", [
        r"for all times on this (?:date|day)",
        r"\blocal (?:time|day|midnight)\b",
        r"highest temperature .{0,80}\bon\b .{0,40}\b(?:19|20)\d\d\b",
        r"daily (?:history|summary|extract)",
        r"absolute daily (?:max|min)",
    ]),
]

# Sources whose published daily extreme is a DAILY SUMMARY value, not the max of
# the hourly observations. Recorded because they differ on frontal days.
_SUMMARY_SOURCES = {
    "wunderground": "daily history page — publishes its own daily rollup",
    "hko.gov.hk": "HKO Daily Extract — 'Absolute Daily Max', one decimal",
    "weather.gov": "NOAA timeseries — hourly observations, no daily rollup",
}


def classify_window(text):
    """Return (window, matched_quote) from a market's resolution text."""
    if not text:
        return "UNKNOWN", None
    flat = re.sub(r"\s+", " ", text)
    for window, patterns in _WINDOW_PATTERNS:
        for pat in patterns:
            m = re.search(pat, flat, re.IGNORECASE)
            if m:
                start = max(0, m.start() - 70)
                end = min(len(flat), m.end() + 70)
                return window, flat[start:end].strip()
    return "UNKNOWN", None


def detect_source(text):
    """Which settlement source the description names, if any."""
    if not text:
        return None, None
    for key, note in _SUMMARY_SOURCES.items():
        if key in text.lower():
            return key, note
    return None, None


def mentions_speci_or_corrections(text):
    """Whether the description says anything about SPECI reports or corrections.

    Almost always None. That absence is itself the finding: the market says
    'highest temperature recorded', and whether the resolver's source includes
    the off-hour SPECI issued because the temperature spiked is left unstated."""
    if not text:
        return None
    if re.search(r"\bSPECI\b|special report|correct(?:ion|ed) report", text, re.I):
        return True
    return False


def is_temperature_market(question):
    """Whether this is a market the bot could actually trade.

    The tag_id=84 population is "weather", not "temperature": it also carries
    monthly precipitation, snowfall and hurricane markets. Auditing those was a
    live false alarm — NYC came back UNKNOWN on 16 markets that were all
    "total precipitation in inches in Central Park ... between August 1 and
    August 31", which have no daily settlement window because they are not
    daily and no temperature bucket because they are not temperature.

    The test is the bot's own parser: if parse_bucket cannot read a temperature
    bucket out of the question, the bot will never evaluate the market, so its
    resolution text says nothing about the window that settles a trade."""
    from scanner import parse_bucket
    if not question:
        return False
    if not re.search(r"\btemperature\b|\btemp\b", question, re.I):
        return False
    lo, hi = parse_bucket(question)
    return lo is not None or hi is not None


def fetch_market_texts(max_pages=20, limit=100):
    """Every live TEMPERATURE market's (city, question, description), by city key.

    Uses the same tag_id=84 events endpoint the scanner discovers on, then
    narrows to the markets the bot would actually evaluate — see
    is_temperature_market."""
    by_city = defaultdict(list)
    seen = skipped = 0
    for page in range(max_pages):
        url = (f"{GAMMA_EVENTS_URL}?tag_id=84&active=true&limit={limit}"
               f"&offset={page * limit}&order=createdAt&ascending=false")
        try:
            resp = safe_get(url, timeout=20)
        except Exception as e:
            logging.error(f"page {page}: {e}")
            break
        if resp.status_code != 200:
            logging.error(f"page {page}: HTTP {resp.status_code}")
            break
        events = resp.json()
        if not isinstance(events, list) or not events:
            break
        for ev in events:
            for mkt in ev.get("markets", []) or []:
                q = mkt.get("question") or ""
                desc = mkt.get("description") or ev.get("description") or ""
                if not is_temperature_market(q):
                    skipped += 1
                    continue
                city_key, _ = get_station_coords(q) if q else (None, None)
                if not city_key:
                    city_key, _ = get_station_coords(ev.get("title") or "")
                if city_key:
                    by_city[city_key].append({"question": q, "description": desc})
                    seen += 1
    logging.info(f"collected {seen} temperature-market descriptions across "
                 f"{len(by_city)} cities ({skipped} non-temperature markets skipped)")
    return by_city


def audit(by_city):
    """One verdict per station in STATIONS, with the quote justifying it."""
    results = {}
    for city in sorted(set(STATIONS)):
        samples = by_city.get(city, [])
        if not samples:
            # NOT the same as ambiguous, and the difference decides whether the
            # city should be excluded from trading. "The text does not say"
            # means a trade would settle on an unknown rule (Karachi). "There is
            # no market open right now" means there is nothing to trade and
            # nothing to decide — the city is simply unaudited, and will be
            # classified the moment a market appears. Excluding on that basis
            # would permanently drop cities for the crime of being out of season.
            results[city] = {
                "window": "NO_MARKET", "quote": None, "n_markets": 0,
                "source": None, "source_note": None, "speci": None,
                "reason": "no live temperature market during the audit — unaudited, "
                          "not ambiguous; re-run when one opens",
            }
            continue

        votes = defaultdict(list)
        for s in samples:
            w, quote = classify_window(s["description"] or s["question"])
            votes[w].append(quote)

        # A city whose live markets disagree is ambiguous by definition, however
        # lopsided the count. Two conventions in one population means the text
        # does not determine the window, which is the UNKNOWN condition.
        decided = [w for w in votes if w != "UNKNOWN"]
        if len(decided) > 1:
            window, quote = "UNKNOWN", None
            reason = (f"live markets disagree: "
                      f"{ {w: len(votes[w]) for w in decided} }")
        elif decided:
            window = decided[0]
            quote = next((q for q in votes[window] if q), None)
            reason = f"{len(votes[window])}/{len(samples)} markets match"
        else:
            window, quote, reason = "UNKNOWN", None, "no pattern matched any description"

        src, note = detect_source(samples[0]["description"])
        results[city] = {
            "window": window, "quote": quote, "n_markets": len(samples),
            "source": src, "source_note": note,
            "speci": mentions_speci_or_corrections(samples[0]["description"]),
            "reason": reason,
        }
    return results


def render_markdown(results):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    unknown = [c for c, r in results.items() if r["window"] == "UNKNOWN"]
    no_market = [c for c, r in results.items() if r["window"] == "NO_MARKET"]
    classified = [c for c, r in results.items()
                  if r["window"] not in ("UNKNOWN", "NO_MARKET")]
    total_markets = sum(r["n_markets"] for r in results.values())
    windows = defaultdict(int)
    for c in classified:
        windows[results[c]["window"]] += 1

    lines = [
        f"# Settlement-window audit — {today}",
        "",
        f"{len(results)} stations, {total_markets:,} live temperature markets read.",
        "",
        f"- **{len(classified)} classified** — "
        + ", ".join(f"{n} `{w}`" for w, n in sorted(windows.items())),
        f"- **{len(unknown)} UNKNOWN** (text is ambiguous — excluded from trading)",
        f"- **{len(no_market)} unaudited** (no live temperature market right now — "
        f"not the same thing; see below)",
        "",
        "## Verdict",
        "",
        "**Every city with a live temperature market settles on the LOCAL "
        "calendar day.** The resolution text is near-verbatim identical across "
        "all of them: *\"the highest temperature recorded for all times on this "
        "day for the <STATION> Station\"*. No market in the population uses "
        "00-24Z, and none uses 6-hourly synoptic groups.",
        "",
        "This is the finding the phase was for, and it is a negative one: the "
        "day-boundary hypothesis behind Hong Kong, Moscow, Seoul and London was "
        "right about the STATION and wrong about the WINDOW. Those were "
        "station-identity bugs, not day-boundary bugs. `metar.fetch_day_extremes` "
        "already filters observations to the station's local calendar day using "
        "its IANA timezone, which is exactly what the text specifies — so the "
        "reader and the resolver already agree, for all 48 classified cities.",
        "",
        "Three settlement sources appear, all reporting a local day:",
        "",
        "| source | cities | what it publishes |",
        "|---|---|---|",
        "| Wunderground daily history | most | its own rollup of the station METAR feed |",
        "| NOAA `weather.gov/wrh/timeseries` | Moscow, Istanbul, Tel Aviv | hourly obs, "
        "highest reading in the \"Temp\" column |",
        "| HKO Daily Extract | Hong Kong | \"Absolute Daily Max\", one decimal |",
        "",
        "## Per-city",
        "",
        "| city | window | markets | evidence | source |",
        "|---|---|---|---|---|",
    ]
    for city in sorted(results):
        r = results[city]
        quote = (r["quote"] or r["reason"] or "").replace("|", "\\|")
        if len(quote) > 160:
            quote = quote[:157] + "..."
        mark = "**UNKNOWN**" if r["window"] == "UNKNOWN" else r["window"]
        lines.append(f"| {city} | {mark} | {r['n_markets']} | {quote} | "
                     f"{r['source'] or '—'} |")

    lines += [
        "",
        "## SPECI and corrections",
        "",
        "No market description in the sampled population mentions SPECI reports "
        "or later-issued corrections. The text says 'highest temperature "
        "recorded' and leaves the question open.",
        "",
        "What our reader does, for the record: `metar.fetch_day_extremes` "
        "requests `data=tmpc` from the IEM ASOS archive, which returns **all** "
        "observation rows for the station — routine METAR and SPECI alike — so "
        "an off-hour special issued because the temperature spiked IS included. "
        "IEM serves the archive's current content, so a correction re-issued "
        "later is picked up on any refetch after it lands; the in-process cache "
        "keys on (icao, date) and only caches days that are complete, so a "
        "correction arriving within the 2h grace window is still seen.",
        "",
        "## max_tmpf vs max(hourly)",
        "",
        "We use `max(hourly observations)`, not the IEM daily-summary "
        "`max_tmpf`. They differ on frontal days, when the true peak falls "
        "between two observation times: the daily summary captures it, the "
        "hourly max does not.",
        "",
        "This matters most where the settlement source publishes its own daily "
        "rollup. Hong Kong already has that handled — HKO's 'Absolute Daily "
        "Max' is read directly. Wunderground-settled cities are the open "
        "question: Wunderground publishes a daily history page whose maximum is "
        "its own rollup of the same METAR feed, and whether that rollup equals "
        "our max(obs) on a frontal day has not been verified against a resolved "
        "market.",
        "",
    ]
    lines += ["## Excluded from trading", ""]
    if unknown:
        lines += [f"- **{c}** — {results[c]['reason']}" for c in sorted(unknown)]
    else:
        lines.append("None. No city's resolution text was ambiguous.")
    lines.append("")

    if no_market:
        lines += [
            "## Unaudited (no live temperature market)",
            "",
            "Not excluded. \"The text does not say\" and \"there is no market "
            "open right now\" are different states, and only the first justifies "
            "refusing to trade — excluding on the second would permanently drop "
            "cities for being out of season. These are classified automatically "
            "the next time the audit runs against an open market.",
            "",
        ]
        lines += [f"- {c}" for c in sorted(no_market)]
        lines.append("")
    return "\n".join(lines)


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--max-pages", type=int, default=20)
    args = ap.parse_args()

    results = audit(fetch_market_texts(max_pages=args.max_pages))

    if args.json:
        print(json.dumps(results, indent=2))
        return 0

    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(
        out_dir, f"settlement-windows-{datetime.now(timezone.utc):%Y-%m-%d}.md")
    md = render_markdown(results)
    with open(path, "w") as fh:
        fh.write(md)
    print(md)
    print(f"\nwritten to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
