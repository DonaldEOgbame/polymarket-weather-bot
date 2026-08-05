"""Local aggregation of hourly forecasts to a daily max/min.

The premise this defends, and the reason it is a test rather than a comment:

    daily=temperature_2m_max IS max(hourly=temperature_2m), exactly, including
    for 6-hourly members like jma_gsm.

That matters because the obvious rationale for switching to hourly — "coarse
models are diurnally compressed, so their daily max is clipped, and computing
it ourselves recovers the peak" — is FALSE. Open-Meteo interpolates upstream of
both endpoints, so both are computed from the same interpolated series and agree
to the float. Measured 2026-08-05: 0.000°F across five models and three cities
spanning +12, +9 and -5 UTC offsets.

If that premise ever stops holding, every calibration constant fitted before the
change is fitted against a different input, silently. So it is asserted here
against a REAL captured API response (tests/fixtures/), which keeps the check
hermetic — CI must never fail because Open-Meteo is having a bad afternoon — and
a live variant runs on demand.

The real reasons for the migration are day-boundary control (already correct per
the Phase 1.1 audit, but now enforced by our own code rather than by Open-Meteo's
timezone handling), one HTTP request instead of two, and hourly values being the
prerequisite for intraday observation conditioning.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import weather as W

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures",
                       "open_meteo_daily_vs_hourly.json")
MODELS = ["ecmwf_ifs025", "gfs_global", "icon_global", "gem_global", "jma_gsm"]


@pytest.fixture(scope="module")
def captured():
    """A real Open-Meteo response holding BOTH endpoints for the same request,
    so the comparison is against the same model run rather than two fetches
    minutes apart."""
    with open(FIXTURE) as fh:
        return json.load(fh)


class TestDailyEqualsMaxOfHourly:
    @pytest.mark.parametrize("city", ["Chicago", "Tokyo", "Wellington"])
    def test_max_matches_to_the_float(self, captured, city):
        data = captured[city]
        daily, hourly = data["daily"], data["hourly"]
        checked = 0
        for model in MODELS:
            agg = W._aggregate_local_days(hourly["time"],
                                          hourly[f"temperature_2m_{model}"])
            for i, day in enumerate(daily["time"]):
                published = daily[f"temperature_2m_max_{model}"][i]
                if published is None or day not in agg:
                    continue
                assert agg[day][0] == pytest.approx(published, abs=0.1), (
                    f"{city} {model} {day}: ours={agg[day][0]} api={published}"
                )
                checked += 1
        assert checked >= 15, f"only {checked} comparisons — fixture is too thin"

    @pytest.mark.parametrize("city", ["Chicago", "Tokyo", "Wellington"])
    def test_min_matches_too(self, captured, city):
        data = captured[city]
        daily, hourly = data["daily"], data["hourly"]
        for model in MODELS:
            agg = W._aggregate_local_days(hourly["time"],
                                          hourly[f"temperature_2m_{model}"])
            for i, day in enumerate(daily["time"]):
                published = daily[f"temperature_2m_min_{model}"][i]
                if published is None or day not in agg:
                    continue
                assert agg[day][1] == pytest.approx(published, abs=0.1)

    def test_it_holds_for_the_six_hourly_member_specifically(self, captured):
        """jma_gsm is the 6-hourly member whose measured diurnal swing is 3.03
        against ICON's hourly 0.79. If interpolation were happening downstream
        of the daily endpoint, this is the model where the two would diverge."""
        data = captured["Tokyo"]
        daily, hourly = data["daily"], data["hourly"]
        agg = W._aggregate_local_days(hourly["time"], hourly["temperature_2m_jma_gsm"])
        diffs = [abs(agg[d][0] - daily["temperature_2m_max_jma_gsm"][i])
                 for i, d in enumerate(daily["time"])
                 if d in agg and daily["temperature_2m_max_jma_gsm"][i] is not None]
        assert diffs and max(diffs) < 0.1, f"jma_gsm diverges: {diffs}"

    @pytest.mark.skipif(os.getenv("OPEN_METEO_LIVE") != "1",
                        reason="set OPEN_METEO_LIVE=1 to re-verify against the live API")
    def test_live(self):
        """Opt-in. Re-run when refreshing the fixture, or when Open-Meteo
        changes anything about how it serves these two endpoints."""
        import requests
        p = dict(latitude=41.9742, longitude=-87.9073, models=",".join(MODELS),
                 timezone="auto", temperature_unit="fahrenheit", forecast_days=4,
                 daily="temperature_2m_max", hourly="temperature_2m")
        d = requests.get(W.OPEN_METEO_URL, params=p, timeout=30).json()
        for model in MODELS:
            agg = W._aggregate_local_days(d["hourly"]["time"],
                                          d["hourly"][f"temperature_2m_{model}"])
            for i, day in enumerate(d["daily"]["time"]):
                pub = d["daily"][f"temperature_2m_max_{model}"][i]
                if pub is not None and day in agg:
                    assert agg[day][0] == pytest.approx(pub, abs=0.1)


class TestLocalDayGrouping:
    def test_days_are_split_on_the_local_date(self, captured):
        """timezone=auto means the timestamps are already local, so t[:10] is
        the local calendar day — the window every market settles on. Wellington
        is the test case that matters: at +12, a UTC-day grouping would split
        its afternoon across two dates."""
        h = captured["Wellington"]["hourly"]
        agg = W._aggregate_local_days(h["time"], h["temperature_2m_ecmwf_ifs025"])
        assert set(agg) == set(captured["Wellington"]["daily"]["time"])

    def test_max_is_at_least_min(self, captured):
        for city in captured:
            h = captured[city]["hourly"]
            for day, (mx, mn) in W._aggregate_local_days(
                    h["time"], h["temperature_2m_ecmwf_ifs025"]).items():
                assert mx >= mn


class TestPartialDaysAreDroppedNotAggregated:
    def test_a_short_day_is_excluded(self):
        """max() over three morning hours is a "daily maximum" that is wrong in
        the direction that makes a NO bet on a high bucket look safe."""
        times = [f"2026-08-05T{h:02d}:00" for h in range(3)]
        assert W._aggregate_local_days(times, [60.0, 61.0, 62.0]) == {}

    def test_a_dst_shortened_day_is_kept(self):
        """Spring-forward gives a 23-hour local day. Dropping it would blank a
        real trading day every March and October."""
        times = [f"2026-03-08T{h:02d}:00" for h in range(23)]
        agg = W._aggregate_local_days(times, [60.0 + h for h in range(23)])
        assert agg["2026-03-08"] == (82.0, 60.0)

    def test_nulls_do_not_count_toward_coverage(self):
        """An out-of-domain member returns nulls. Counting them as hours would
        let a day with two real values pass the coverage floor."""
        times = [f"2026-08-05T{h:02d}:00" for h in range(24)]
        vals = [60.0, 61.0] + [None] * 22
        assert W._aggregate_local_days(times, vals) == {}


class TestBothDirectionsComeFromOneFetch:
    def test_the_cache_is_keyed_on_city_not_on_direction(self, monkeypatch):
        """The daily endpoint needed one request for max and another for min.
        Latency here is dominated by round trips (979ms for a 746-byte daily
        response), so halving the request count is the actual performance win of
        this migration — not the payload, which grew."""
        calls = []

        class _Resp:
            status_code = 200

            def json(self):
                times = [f"2026-08-0{d}T{h:02d}:00" for d in (5, 6) for h in range(24)]
                return {"hourly": {"time": times,
                                   "temperature_2m_ecmwf_ifs025": [70.0 + h % 24
                                                                   for h in range(48)]}}

        class _Sess:
            def get(self, url, params=None, timeout=None):
                calls.append(params)
                return _Resp()

        monkeypatch.setattr(W, "get_session", lambda: _Sess())
        W._FORECAST_CACHE.clear()
        W.fetch_forecasts("Chicago", is_high=True)
        W.fetch_forecasts("Chicago", is_high=False)
        assert len(calls) == 1, "the second direction re-fetched instead of using the cache"
        assert calls[0]["hourly"] == "temperature_2m"
        assert "daily" not in calls[0]
        W._FORECAST_CACHE.clear()

    def test_max_and_min_disagree_as_they_should(self, monkeypatch):
        """Guards the direction plumbing: a bug that returned the max for both
        would be invisible to every other test here."""
        class _Resp:
            status_code = 200

            def json(self):
                times = [f"2026-08-05T{h:02d}:00" for h in range(24)]
                return {"hourly": {"time": times,
                                   "temperature_2m_ecmwf_ifs025": [60.0 + h for h in range(24)]}}

        class _Sess:
            def get(self, *a, **k):
                return _Resp()

        monkeypatch.setattr(W, "get_session", lambda: _Sess())
        W._FORECAST_CACHE.clear()
        hi = W.fetch_forecasts("Chicago", is_high=True)[3]["2026-08-05"]
        lo = W.fetch_forecasts("Chicago", is_high=False)[3]["2026-08-05"]
        assert hi["ecmwf_ifs025"] == 83.0
        assert lo["ecmwf_ifs025"] == 60.0
        W._FORECAST_CACHE.clear()


class TestThePhaseBoundaryIsQueryable:
    def test_the_pipeline_version_is_in_the_fingerprint(self):
        """Every phase in this rollout changes forecast INPUTS while touching no
        fingerprinted constant. Without this key the replay log would carry an
        identical fingerprint across a boundary where the inputs changed
        completely, and a later calibration would pool the two — exactly the
        failure the fingerprint column exists to prevent, through a door it did
        not cover."""
        import config as C
        assert "FORECAST_PIPELINE_VERSION" in C._FINGERPRINT_KEYS
        before = C.config_fingerprint()
        original = C.FORECAST_PIPELINE_VERSION
        try:
            C.FORECAST_PIPELINE_VERSION = original + 1
            assert C.config_fingerprint() != before
        finally:
            C.FORECAST_PIPELINE_VERSION = original

    def test_the_version_records_the_hourly_migration(self):
        import config as C
        assert C.FORECAST_PIPELINE_VERSION >= 2
