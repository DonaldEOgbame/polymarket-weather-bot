"""Tests for the independent forecast veto gate (independent.py).

The bulk of this file guards ONE invariant: no error path may ever produce a
numeric temperature. That is not a hypothetical — coverage_matrix.py drew three
separate wrong conclusions from exactly this confusion, the last one reading
rate-limiter HTML as "NO DATA ANYWHERE" and nearly deleting five working models
from the plan. The veto gate is more exposed than that script was: it runs
continuously, against third-party providers, on the critical path of every trade
decision. If a 429 is read as "the independent forecast disagrees", the gate
stops the book on an artefact — a worse failure than having no gate at all.
"""
import json
import os
import sys

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import config
import independent
from independent import (
    DATA, NO_DATA, INCONCLUSIVE,
    IndependentForecast, evaluate_veto, get_independent_forecast,
    provider_for, veto_gate_rows, _bucket_overlaps_band,
    SOURCE_NWS, SOURCE_DATAHUB, NWS_CITIES,
)
from weather import STATIONS


# --- Fakes ------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status_code=200, payload=None, body=None):
        self.status_code = status_code
        self._payload = payload
        self.text = body if body is not None else ""

    def json(self):
        if self._payload is None:
            raise ValueError("not JSON")
        return self._payload


class FakeSession:
    """Records calls and replays a queued list of responses/exceptions."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError(f"unexpected extra request to {url}")
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def nws_points(hourly_url="https://api.weather.gov/gridpoints/OKX/37,46/forecast/hourly"):
    return FakeResponse(200, {"properties": {"forecastHourly": hourly_url}})


def nws_hourly(periods):
    return FakeResponse(200, {"properties": {"periods": periods}})


def period(start, temp, unit="F"):
    return {"startTime": start, "temperature": temp, "temperatureUnit": unit}


@pytest.fixture(autouse=True)
def clean_state():
    independent.reset_state()
    independent.reset_tripwire()
    yield
    independent.reset_state()
    independent.reset_tripwire()


@pytest.fixture
def armed(monkeypatch):
    monkeypatch.setattr(config, "INDEPENDENT_VETO_ENABLED", True)
    monkeypatch.setattr(config, "DISAGREEMENT_VETO_F", 5.0)
    monkeypatch.setattr(config, "PLAUSIBLE_BAND_F", 2.0)


def use_session(monkeypatch, session):
    monkeypatch.setattr(independent, "_get_session", lambda: session)


# --- Provider routing -------------------------------------------------------

class TestProviderRouting:
    def test_every_us_city_routes_to_nws(self):
        for city in NWS_CITIES:
            assert provider_for(city) == SOURCE_NWS

    def test_every_station_resolves_to_a_provider(self):
        """Every city in STATIONS routes somewhere. No city is silently ungated."""
        for city in STATIONS:
            assert provider_for(city) in (SOURCE_NWS, SOURCE_DATAHUB)

    def test_non_us_cities_route_to_datahub(self):
        for city in ("Tokyo", "London", "Hong Kong", "Wellington", "Lagos"):
            assert provider_for(city) == SOURCE_DATAHUB

    def test_north_american_non_us_cities_do_not_route_to_nws(self):
        """Toronto, Mexico City and Panama carry region="US" in STATIONS but
        api.weather.gov does not serve them. Routing on `region` instead of the
        explicit city list would send all three to a provider that 404s them."""
        for city in ("Toronto", "Mexico City", "Panama"):
            assert STATIONS[city]["region"] == "US"
            assert provider_for(city) == SOURCE_DATAHUB

    def test_nws_city_count_matches_the_plan(self):
        """Eleven NWS cities, plus "New York" as an alias of "NYC"."""
        assert len(NWS_CITIES) == 12
        assert {"NYC", "New York"} <= NWS_CITIES


# --- The three states -------------------------------------------------------

class TestThreeStateHandling:
    """Every failure mode resolves to a state, and only DATA carries a number."""

    @pytest.mark.parametrize("failure,label", [
        (requests.exceptions.Timeout("timed out"), "timeout"),
        (requests.exceptions.ConnectionError("refused"), "connection error"),
        (FakeResponse(429), "429"),
        (FakeResponse(500), "500"),
        (FakeResponse(503), "503"),
        (FakeResponse(200, payload=None, body="<html>rate limited</html>"), "HTML body"),
    ])
    def test_transport_failures_are_inconclusive(self, monkeypatch, failure, label):
        use_session(monkeypatch, FakeSession([failure]))
        res = get_independent_forecast("NYC", "2026-08-07", True)
        assert res.state == INCONCLUSIVE, label
        assert res.value_f is None, f"{label} produced a temperature"

    def test_malformed_json_is_inconclusive(self, monkeypatch):
        """A 200 whose body is valid JSON but the wrong shape."""
        use_session(monkeypatch, FakeSession([FakeResponse(200, {"unexpected": 1})]))
        res = get_independent_forecast("NYC", "2026-08-07", True)
        assert res.state == INCONCLUSIVE
        assert res.value_f is None

    def test_nws_404_is_no_data_not_inconclusive(self, monkeypatch):
        """404 from /points is DEFINITIVE out-of-domain (verified 2026-08-06:
        Tokyo and London 404, NYC 200). Lumping it in with 5xx would leave an
        unserved city permanently INCONCLUSIVE and indistinguishable from an
        outage."""
        use_session(monkeypatch, FakeSession([FakeResponse(404)]))
        res = get_independent_forecast("NYC", "2026-08-07", True)
        assert res.state == NO_DATA
        assert res.value_f is None

    def test_explicit_null_is_no_data_not_inconclusive(self, monkeypatch):
        """HTTP 200 with explicit nulls is a coverage fact, not an error."""
        use_session(monkeypatch, FakeSession([
            nws_points(),
            nws_hourly([period("2026-08-07T01:00:00-04:00", None)]),
        ]))
        res = get_independent_forecast("NYC", "2026-08-07", True)
        assert res.state == NO_DATA
        assert res.value_f is None

    def test_no_data_and_inconclusive_are_logged_distinguishably(self, monkeypatch, caplog):
        use_session(monkeypatch, FakeSession([FakeResponse(500)]))
        with caplog.at_level("WARNING"):
            res = get_independent_forecast("NYC", "2026-08-07", True)
        assert res.state == INCONCLUSIVE
        assert "INCONCLUSIVE" in caplog.text
        assert "Gate fails open" in caplog.text

        independent.reset_state()
        caplog.clear()
        use_session(monkeypatch, FakeSession([FakeResponse(404)]))
        with caplog.at_level("WARNING"):
            res = get_independent_forecast("NYC", "2026-08-07", True)
        assert res.state == NO_DATA
        assert "INCONCLUSIVE" not in caplog.text

    def test_missing_datahub_key_is_inconclusive_not_no_data(self, monkeypatch):
        """An unset key must never read as "UKMO covers nothing there"."""
        monkeypatch.setattr(independent, "METOFFICE_DATAHUB_KEY", "")
        res = get_independent_forecast("Tokyo", "2026-08-07", True)
        assert res.state == INCONCLUSIVE
        assert res.value_f is None
        assert "not configured" in res.detail

    def test_no_error_path_ever_produces_a_temperature(self, monkeypatch):
        """The headline invariant, swept over every failure shape at once.

        Not 0.0, not a default, not a None that a caller could read as
        agreement — a non-DATA state carries no number at all."""
        failures = [
            requests.exceptions.Timeout("t"),
            requests.exceptions.ConnectionError("c"),
            FakeResponse(429), FakeResponse(500), FakeResponse(502),
            FakeResponse(200, payload=None, body="<html>error</html>"),
            FakeResponse(200, {"properties": {}}),
            FakeResponse(200, {}),
        ]
        for f in failures:
            independent.reset_state()
            use_session(monkeypatch, FakeSession([f]))
            res = get_independent_forecast("NYC", "2026-08-07", True)
            assert res.state != DATA
            assert res.value_f is None
            # And the constructor refuses to hold one even if asked.
            forced = IndependentForecast(res.state, "x", value_f=0.0)
            assert forced.value_f is None

    def test_unhandled_exception_resolves_to_inconclusive(self, monkeypatch):
        """No exception escapes onto the trading path."""
        def boom():
            raise RuntimeError("something nobody anticipated")
        monkeypatch.setattr(independent, "_get_session", boom)
        res = get_independent_forecast("NYC", "2026-08-07", True)
        assert res.state == INCONCLUSIVE
        assert res.value_f is None


# --- Parsing ----------------------------------------------------------------

class TestNWSParsing:
    def test_daily_max_from_local_day(self, monkeypatch):
        use_session(monkeypatch, FakeSession([
            nws_points(),
            nws_hourly([
                period("2026-08-07T01:00:00-04:00", 70),
                period("2026-08-07T14:00:00-04:00", 88),
                period("2026-08-07T22:00:00-04:00", 75),
                period("2026-08-08T14:00:00-04:00", 95),   # next day, must not leak
            ]),
        ]))
        res = get_independent_forecast("NYC", "2026-08-07", True)
        assert res.state == DATA
        assert res.value_f == 88.0

    def test_daily_min_from_local_day(self, monkeypatch):
        use_session(monkeypatch, FakeSession([
            nws_points(),
            nws_hourly([
                period("2026-08-07T05:00:00-04:00", 62),
                period("2026-08-07T14:00:00-04:00", 88),
            ]),
        ]))
        res = get_independent_forecast("NYC", "2026-08-07", False)
        assert res.state == DATA
        assert res.value_f == 62.0

    def test_local_offset_defines_the_day(self, monkeypatch):
        """startTime carries the station's local offset, so startTime[:10] IS the
        local calendar day — the window every market settles on. A UTC reading of
        the same stamps would move the 22:00 EDT hour into the next day."""
        use_session(monkeypatch, FakeSession([
            nws_points(),
            nws_hourly([period("2026-08-07T22:00:00-04:00", 99)]),
        ]))
        res = get_independent_forecast("NYC", "2026-08-07", True)
        assert res.state == DATA and res.value_f == 99.0

    def test_celsius_periods_are_converted(self, monkeypatch):
        use_session(monkeypatch, FakeSession([
            nws_points(),
            nws_hourly([period("2026-08-07T14:00:00-04:00", 20, unit="C")]),
        ]))
        res = get_independent_forecast("NYC", "2026-08-07", True)
        assert res.state == DATA
        assert res.value_f == pytest.approx(68.0)

    def test_unknown_unit_is_dropped_not_guessed(self, monkeypatch):
        use_session(monkeypatch, FakeSession([
            nws_points(),
            nws_hourly([period("2026-08-07T14:00:00-04:00", 20, unit="K")]),
        ]))
        res = get_independent_forecast("NYC", "2026-08-07", True)
        assert res.state == NO_DATA
        assert res.value_f is None

    def test_date_outside_horizon_is_no_data(self, monkeypatch):
        use_session(monkeypatch, FakeSession([
            nws_points(),
            nws_hourly([period("2026-08-07T14:00:00-04:00", 88)]),
        ]))
        res = get_independent_forecast("NYC", "2026-12-25", True)
        assert res.state == NO_DATA


class TestDataHubParsing:
    def _payload(self, points):
        return {"features": [{"properties": {"timeSeries": points}}]}

    def test_utc_series_aggregates_to_local_day(self, monkeypatch):
        """DataHub timestamps are UTC, so the local day must come from the
        station's IANA timezone. Tokyo is UTC+9: 2026-08-06T20:00Z is
        2026-08-07 05:00 local and belongs to the 7th."""
        monkeypatch.setattr(independent, "METOFFICE_DATAHUB_KEY", "test-key")
        use_session(monkeypatch, FakeSession([FakeResponse(200, self._payload([
            {"time": "2026-08-06T20:00Z", "screenTemperature": 25.0},   # 7th local
            {"time": "2026-08-07T06:00Z", "screenTemperature": 33.0},   # 7th local
            {"time": "2026-08-07T16:00Z", "screenTemperature": 40.0},   # 8th local
        ]))]))
        res = get_independent_forecast("Tokyo", "2026-08-07", True)
        assert res.state == DATA
        assert res.value_f == pytest.approx(91.4)  # 33C, not the 40C on the 8th

    def test_celsius_converted_to_fahrenheit(self, monkeypatch):
        monkeypatch.setattr(independent, "METOFFICE_DATAHUB_KEY", "test-key")
        use_session(monkeypatch, FakeSession([FakeResponse(200, self._payload([
            {"time": "2026-08-07T12:00Z", "screenTemperature": 0.0},
        ]))]))
        res = get_independent_forecast("London", "2026-08-07", True)
        assert res.state == DATA
        assert res.value_f == pytest.approx(32.0)

    def test_400_is_no_data(self, monkeypatch):
        monkeypatch.setattr(independent, "METOFFICE_DATAHUB_KEY", "test-key")
        use_session(monkeypatch, FakeSession([FakeResponse(400)]))
        res = get_independent_forecast("Tokyo", "2026-08-07", True)
        assert res.state == NO_DATA

    def test_401_is_inconclusive_not_no_data(self, monkeypatch):
        """A rejected key says nothing about coverage. Treating it as absence is
        how a rotated credential silently disables the gate while looking like a
        clean negative result."""
        monkeypatch.setattr(independent, "METOFFICE_DATAHUB_KEY", "bad-key")
        use_session(monkeypatch, FakeSession([FakeResponse(401)]))
        res = get_independent_forecast("Tokyo", "2026-08-07", True)
        assert res.state == INCONCLUSIVE

    def test_unparseable_points_are_inconclusive(self, monkeypatch):
        monkeypatch.setattr(independent, "METOFFICE_DATAHUB_KEY", "test-key")
        use_session(monkeypatch, FakeSession([FakeResponse(200, self._payload([
            {"time": "not-a-timestamp", "screenTemperature": 20.0},
        ]))]))
        res = get_independent_forecast("Tokyo", "2026-08-07", True)
        assert res.state == INCONCLUSIVE
        assert res.value_f is None

    def test_key_is_sent_as_a_header_not_a_query_param(self, monkeypatch):
        monkeypatch.setattr(independent, "METOFFICE_DATAHUB_KEY", "secret-key")
        sess = FakeSession([FakeResponse(200, self._payload([
            {"time": "2026-08-07T12:00Z", "screenTemperature": 20.0}]))])
        use_session(monkeypatch, sess)
        get_independent_forecast("London", "2026-08-07", True)
        url, kwargs = sess.calls[0]
        assert kwargs["headers"]["apikey"] == "secret-key"
        assert "secret-key" not in json.dumps(kwargs.get("params", {}))


# --- Cache ------------------------------------------------------------------

class TestCache:
    def test_second_call_inside_ttl_does_not_refetch(self, monkeypatch):
        sess = FakeSession([
            nws_points(),
            nws_hourly([period("2026-08-07T14:00:00-04:00", 88)]),
        ])
        use_session(monkeypatch, sess)
        a = get_independent_forecast("NYC", "2026-08-07", True)
        n = len(sess.calls)
        b = get_independent_forecast("NYC", "2026-08-07", True)
        assert a.value_f == b.value_f == 88.0
        assert len(sess.calls) == n, "cache miss inside TTL"

    def test_expired_entry_is_dropped_not_served(self, monkeypatch):
        """A stale cached temperature served past its TTL is one of the shapes of
        "an error path produced a number"."""
        monkeypatch.setattr(config, "INDEPENDENT_CACHE_TTL_SECONDS", 0)
        monkeypatch.setattr(independent, "INDEPENDENT_CACHE_TTL_SECONDS", 0)
        sess = FakeSession([
            nws_points(),
            nws_hourly([period("2026-08-07T14:00:00-04:00", 88)]),
            nws_hourly([period("2026-08-07T14:00:00-04:00", 91)]),
        ])
        use_session(monkeypatch, sess)
        assert get_independent_forecast("NYC", "2026-08-07", True).value_f == 88.0
        assert get_independent_forecast("NYC", "2026-08-07", True).value_f == 91.0

    def test_inconclusive_is_not_cached(self, monkeypatch):
        """Caching a transient failure would pin a 6-hour hole in coverage
        because one request timed out."""
        sess = FakeSession([
            FakeResponse(500),
            nws_points(),
            nws_hourly([period("2026-08-07T14:00:00-04:00", 88)]),
        ])
        use_session(monkeypatch, sess)
        assert get_independent_forecast("NYC", "2026-08-07", True).state == INCONCLUSIVE
        assert get_independent_forecast("NYC", "2026-08-07", True).value_f == 88.0

    def test_high_and_low_are_cached_separately(self, monkeypatch):
        sess = FakeSession([
            nws_points(),
            nws_hourly([period("2026-08-07T05:00:00-04:00", 62),
                        period("2026-08-07T14:00:00-04:00", 88)]),
            nws_hourly([period("2026-08-07T05:00:00-04:00", 62),
                        period("2026-08-07T14:00:00-04:00", 88)]),
        ])
        use_session(monkeypatch, sess)
        assert get_independent_forecast("NYC", "2026-08-07", True).value_f == 88.0
        assert get_independent_forecast("NYC", "2026-08-07", False).value_f == 62.0

    def test_gridpoint_is_reused_across_dates(self, monkeypatch):
        sess = FakeSession([
            nws_points(),
            nws_hourly([period("2026-08-07T14:00:00-04:00", 88)]),
            nws_hourly([period("2026-08-08T14:00:00-04:00", 90)]),
        ])
        use_session(monkeypatch, sess)
        get_independent_forecast("NYC", "2026-08-07", True)
        get_independent_forecast("NYC", "2026-08-08", True)
        points_calls = [u for u, _ in sess.calls if "/points/" in u]
        assert len(points_calls) == 1


# --- Circuit breaker --------------------------------------------------------

class TestCircuitBreaker:
    def test_opens_after_n_consecutive_failures(self, monkeypatch):
        monkeypatch.setattr(config, "INDEPENDENT_BREAKER_FAILURES", 3)
        monkeypatch.setattr(independent, "INDEPENDENT_BREAKER_FAILURES", 3)
        sess = FakeSession([FakeResponse(500)] * 3)
        use_session(monkeypatch, sess)
        for d in ("2026-08-07", "2026-08-08", "2026-08-09"):
            get_independent_forecast("NYC", d, True)
        calls_before = len(sess.calls)
        res = get_independent_forecast("NYC", "2026-08-10", True)
        assert res.state == INCONCLUSIVE
        assert "circuit breaker open" in res.detail
        assert len(sess.calls) == calls_before, "breaker did not suppress the call"

    def test_closes_after_cooldown(self, monkeypatch):
        monkeypatch.setattr(config, "INDEPENDENT_BREAKER_FAILURES", 2)
        monkeypatch.setattr(independent, "INDEPENDENT_BREAKER_FAILURES", 2)
        monkeypatch.setattr(independent, "INDEPENDENT_BREAKER_COOLDOWN_SECONDS", 0)
        sess = FakeSession([
            FakeResponse(500), FakeResponse(500),
            nws_points(),
            nws_hourly([period("2026-08-07T14:00:00-04:00", 88)]),
        ])
        use_session(monkeypatch, sess)
        get_independent_forecast("NYC", "2026-08-07", True)
        get_independent_forecast("NYC", "2026-08-08", True)
        # Same date as the first attempt: INCONCLUSIVE is not cached, so this
        # re-fetches and proves the breaker let the call through.
        res = get_independent_forecast("NYC", "2026-08-07", True)
        assert res.state == DATA, "breaker never closed after cooldown"
        assert res.value_f == 88.0

    def test_success_resets_the_failure_count(self, monkeypatch):
        monkeypatch.setattr(config, "INDEPENDENT_BREAKER_FAILURES", 3)
        monkeypatch.setattr(independent, "INDEPENDENT_BREAKER_FAILURES", 3)
        sess = FakeSession([
            FakeResponse(500), FakeResponse(500),
            nws_points(),
            nws_hourly([period("2026-08-07T14:00:00-04:00", 88)]),
            FakeResponse(500), FakeResponse(500),
        ])
        use_session(monkeypatch, sess)
        get_independent_forecast("NYC", "2026-08-01", True)
        get_independent_forecast("NYC", "2026-08-02", True)
        assert get_independent_forecast("NYC", "2026-08-07", True).state == DATA
        get_independent_forecast("NYC", "2026-08-03", True)
        res = get_independent_forecast("NYC", "2026-08-04", True)
        assert "circuit breaker open" not in (res.detail or "")

    def test_missing_key_does_not_trip_the_breaker(self, monkeypatch):
        """A configuration gap is not a provider outage."""
        monkeypatch.setattr(independent, "METOFFICE_DATAHUB_KEY", "")
        monkeypatch.setattr(independent, "INDEPENDENT_BREAKER_FAILURES", 2)
        for d in ("2026-08-07", "2026-08-08", "2026-08-09"):
            get_independent_forecast("Tokyo", d, True)
        res = get_independent_forecast("Tokyo", "2026-08-10", True)
        assert "circuit breaker" not in (res.detail or "")

    def test_breaker_is_per_provider(self, monkeypatch):
        """DataHub being down must not blind the NWS cities."""
        monkeypatch.setattr(independent, "METOFFICE_DATAHUB_KEY", "test-key")
        monkeypatch.setattr(independent, "INDEPENDENT_BREAKER_FAILURES", 2)
        sess = FakeSession([
            FakeResponse(500), FakeResponse(500),
            nws_points(),
            nws_hourly([period("2026-08-07T14:00:00-04:00", 88)]),
        ])
        use_session(monkeypatch, sess)
        get_independent_forecast("Tokyo", "2026-08-07", True)
        get_independent_forecast("London", "2026-08-07", True)
        assert get_independent_forecast("NYC", "2026-08-07", True).state == DATA


# --- Gate logic -------------------------------------------------------------

def veto_for(monkeypatch, independent_value, ensemble_mean, lo, hi, state=DATA):
    fc = IndependentForecast(state, SOURCE_NWS, value_f=independent_value,
                             fetched_at="2026-08-06T00:00:00+00:00")
    monkeypatch.setattr(independent, "get_independent_forecast",
                        lambda *a, **k: fc)
    return evaluate_veto("NYC", "2026-08-07", True, ensemble_mean, lo, hi)


class TestGrossDisagreement:
    def test_fires_at_5_1_degrees(self, monkeypatch, armed):
        v = veto_for(monkeypatch, 85.1, 80.0, 90.0, 92.0)
        assert v["disagreement_f"] == pytest.approx(5.1)
        assert v["veto_gross"] is True
        assert v["vetoed"] is True

    def test_does_not_fire_at_4_9_degrees(self, monkeypatch, armed):
        v = veto_for(monkeypatch, 84.9, 80.0, 90.0, 92.0)
        assert v["disagreement_f"] == pytest.approx(4.9)
        assert v["veto_gross"] is False

    def test_does_not_fire_exactly_at_the_threshold(self, monkeypatch, armed):
        """Strictly greater than, per the plan: `> DISAGREEMENT_VETO_F`."""
        v = veto_for(monkeypatch, 85.0, 80.0, 90.0, 92.0)
        assert v["veto_gross"] is False

    def test_fires_in_either_direction(self, monkeypatch, armed):
        assert veto_for(monkeypatch, 74.0, 80.0, 90.0, 92.0)["veto_gross"] is True


class TestBucketBand:
    """band = independent +/- 2.0. Overlap refuses; adjacency does not."""

    def test_fires_when_bucket_overlaps_band(self, monkeypatch, armed):
        # band = [68, 72]; bucket [71, 73] overlaps
        v = veto_for(monkeypatch, 70.0, 70.0, 71.0, 73.0)
        assert v["veto_band"] is True
        assert v["vetoed"] is True

    def test_does_not_fire_when_merely_adjacent(self, monkeypatch, armed):
        # band = [68, 72]; bucket [72, 74] touches at one point only
        v = veto_for(monkeypatch, 70.0, 70.0, 72.0, 74.0)
        assert v["veto_band"] is False

    def test_does_not_fire_when_clear_of_the_band(self, monkeypatch, armed):
        v = veto_for(monkeypatch, 70.0, 70.0, 80.0, 82.0)
        assert v["veto_band"] is False

    def test_bucket_containing_the_band_fires(self, monkeypatch, armed):
        v = veto_for(monkeypatch, 70.0, 70.0, 60.0, 80.0)
        assert v["veto_band"] is True

    def test_exact_bucket_inside_the_band_fires(self, monkeypatch, armed):
        v = veto_for(monkeypatch, 70.0, 70.0, 71.0, 71.0)
        assert v["veto_band"] is True

    def test_exact_bucket_on_the_band_edge_does_not_fire(self, monkeypatch, armed):
        v = veto_for(monkeypatch, 70.0, 70.0, 72.0, 72.0)
        assert v["veto_band"] is False

    def test_above_bucket_uses_a_half_line_not_a_zero_bound(self, monkeypatch, armed):
        """"above 71" is [71, +inf). Reading the missing upper bound as 0.0 —
        or as a finite default — would mis-place every open-ended bucket."""
        assert _bucket_overlaps_band(71.0, None, 68.0, 72.0) is True
        assert _bucket_overlaps_band(90.0, None, 68.0, 72.0) is False

    def test_below_bucket_uses_a_half_line(self, monkeypatch, armed):
        assert _bucket_overlaps_band(None, 69.0, 68.0, 72.0) is True
        assert _bucket_overlaps_band(None, 50.0, 68.0, 72.0) is False

    def test_unbounded_bucket_never_fires(self):
        assert _bucket_overlaps_band(None, None, 68.0, 72.0) is False


class TestOnlyDataCanVeto:
    @pytest.mark.parametrize("state", [NO_DATA, INCONCLUSIVE])
    def test_non_data_states_never_veto(self, monkeypatch, armed, state):
        """Even with an absurd disagreement, a non-DATA state refuses nothing."""
        v = veto_for(monkeypatch, None, 80.0, 90.0, 92.0, state=state)
        assert v["vetoed"] is False
        assert v["veto_gross"] is False
        assert v["veto_band"] is False
        assert v["disagreement_f"] is None
        assert v["independent_value"] is None

    def test_missing_ensemble_mean_does_not_veto(self, monkeypatch, armed):
        v = veto_for(monkeypatch, 85.0, None, 90.0, 92.0)
        assert v["vetoed"] is False
        assert v["disagreement_f"] is None


class TestArmingAndCounterfactual:
    def test_disabled_gate_still_records_its_conclusion(self, monkeypatch):
        """§5c: the counterfactual survives the gate being off."""
        monkeypatch.setattr(config, "INDEPENDENT_VETO_ENABLED", False)
        monkeypatch.setattr(config, "DISAGREEMENT_VETO_F", 5.0)
        monkeypatch.setattr(config, "PLAUSIBLE_BAND_F", 2.0)
        v = veto_for(monkeypatch, 90.0, 80.0, 95.0, 97.0)
        assert v["veto_gross"] is True, "conclusion lost when disabled"
        assert v["vetoed"] is False, "disabled gate refused a trade"
        assert v["armed"] is False

    def test_tripwire_suppresses_the_refusal_but_not_the_record(self, monkeypatch, armed):
        independent._TRIPPED = True
        v = veto_for(monkeypatch, 90.0, 80.0, 95.0, 97.0)
        assert v["veto_gross"] is True
        assert v["vetoed"] is False
        assert v["armed"] is False

    def test_armed_gate_refuses(self, monkeypatch, armed):
        v = veto_for(monkeypatch, 90.0, 80.0, 95.0, 97.0)
        assert v["vetoed"] is True
        assert v["armed"] is True


class TestGateRows:
    def test_refusal_reason_names_the_source_and_the_value(self, monkeypatch, armed):
        v = veto_for(monkeypatch, 90.5, 80.0, 95.0, 97.0)
        rows = veto_gate_rows(v)
        gross = next(r for r in rows if r["gate"] == "independent_gross_disagreement")
        assert gross["passed"] is False
        assert SOURCE_NWS in gross["detail"]
        assert "90.5" in gross["detail"]
        assert gross["observed"] == pytest.approx(10.5)
        assert gross["threshold"] == 5.0

    def test_both_rows_present_even_with_no_data(self, monkeypatch, armed):
        v = veto_for(monkeypatch, None, 80.0, 95.0, 97.0, state=NO_DATA)
        rows = veto_gate_rows(v)
        assert {r["gate"] for r in rows} == {
            "independent_gross_disagreement", "independent_bucket_band"}
        assert all(r["passed"] for r in rows)

    def test_rows_present_for_an_empty_veto(self):
        """A missing veto record must still produce passing rows, never a
        KeyError on the trading path."""
        rows = veto_gate_rows({})
        assert len(rows) == 2
        assert all(r["passed"] for r in rows)

    def test_disabled_gate_rows_say_so(self, monkeypatch, armed):
        independent._TRIPPED = True
        v = veto_for(monkeypatch, 90.0, 80.0, 95.0, 97.0)
        rows = veto_gate_rows(v)
        assert all(r["passed"] for r in rows), "disabled gate still refusing"
        assert "auto-disabled" in rows[0]["detail"]

    def test_gate_rows_are_storable_by_log_replay_signal(self, monkeypatch, armed):
        """observed/threshold must be float-coercible or NULL — db.py casts them."""
        for state in (DATA, NO_DATA, INCONCLUSIVE):
            v = veto_for(monkeypatch, 90.0 if state == DATA else None,
                         80.0, 95.0, 97.0, state=state)
            for r in veto_gate_rows(v):
                assert r["observed"] is None or float(r["observed"]) == r["observed"]
                assert r["threshold"] is None or float(r["threshold"]) == r["threshold"]


# --- Isolation from the ensemble --------------------------------------------

class TestNeverEntersTheEnsemble:
    """The failure mode that would quietly turn a veto into an unmeasured fifth
    model with no bias correction and no place in the family caps."""

    def test_evaluate_veto_does_not_mutate_engine_fields(self, monkeypatch, armed):
        engine = {
            "ensemble_mean": 80.0, "ensemble_std": 3.0, "model_spread": 1.0,
            "model_agreement": 0.9, "model_count": 4,
        }
        before = dict(engine)
        veto_for(monkeypatch, 95.0, engine["ensemble_mean"], 90.0, 92.0)
        assert engine == before

    @pytest.mark.parametrize("field", [
        "ensemble_mean", "ensemble_std", "model_spread",
        "model_agreement", "model_count",
    ])
    def test_veto_record_carries_no_ensemble_field(self, monkeypatch, armed, field):
        v = veto_for(monkeypatch, 95.0, 80.0, 90.0, 92.0)
        assert field not in v

    def test_independent_value_is_absent_from_the_engine_result(self, monkeypatch, armed):
        """Full evaluation path: the number the veto saw must not appear in any
        ensemble statistic afterwards."""
        engine = {
            "ensemble_mean": 80.0, "ensemble_std": 3.0, "model_spread": 1.0,
            "model_agreement": 0.9, "model_count": 4,
        }
        veto_for(monkeypatch, 95.0, engine["ensemble_mean"], 90.0, 92.0)
        assert engine["ensemble_mean"] == 80.0
        assert 95.0 not in engine.values()


# --- Tripwire ---------------------------------------------------------------

class TestTripwire:
    def _stats(self, considered, fired, by_city=None, top_city=None, share=0.0):
        return {"window_hours": 24, "considered": considered, "fired": fired,
                "fire_rate": (fired / considered) if considered else 0.0,
                "by_city": by_city or {}, "top_city": top_city,
                "top_city_share": share}

    def test_fires_above_the_threshold(self, monkeypatch, armed, caplog):
        monkeypatch.setattr("db.independent_veto_stats",
                            lambda hours=24: self._stats(100, 30, {"NYC": 30}, "NYC", 1.0))
        with caplog.at_level("ERROR"):
            independent.check_tripwire()
        assert independent.veto_armed() is False
        assert "AUTO-DISABLED" in caplog.text

    def test_does_not_fire_below_the_threshold(self, monkeypatch, armed):
        monkeypatch.setattr("db.independent_veto_stats",
                            lambda hours=24: self._stats(100, 20, {"NYC": 20}, "NYC", 1.0))
        independent.check_tripwire()
        assert independent.veto_armed() is True

    def test_small_sample_does_not_trip(self, monkeypatch, armed):
        """1 veto out of 2 is 50% and means nothing."""
        monkeypatch.setattr("db.independent_veto_stats",
                            lambda hours=24: self._stats(2, 1, {"NYC": 1}, "NYC", 1.0))
        independent.check_tripwire()
        assert independent.veto_armed() is True

    def test_disabled_gate_keeps_logging(self, monkeypatch, armed):
        monkeypatch.setattr("db.independent_veto_stats",
                            lambda hours=24: self._stats(100, 40, {"NYC": 40}, "NYC", 1.0))
        independent.check_tripwire()
        assert independent.veto_armed() is False
        v = veto_for(monkeypatch, 95.0, 80.0, 90.0, 92.0)
        assert v["veto_gross"] is True, "stopped logging after auto-disable"
        assert v["vetoed"] is False, "still refusing after auto-disable"

    def test_latches_and_does_not_re_arm_itself(self, monkeypatch, armed):
        """An auto-re-arming gate would oscillate back into the same storm."""
        stats = {"v": self._stats(100, 40, {"NYC": 40}, "NYC", 1.0)}
        monkeypatch.setattr("db.independent_veto_stats", lambda hours=24: stats["v"])
        independent.check_tripwire()
        assert independent.veto_armed() is False
        stats["v"] = self._stats(100, 0)
        independent.check_tripwire()
        assert independent.veto_armed() is False

    def test_city_concentration_logs_an_error_naming_the_city(self, monkeypatch, armed, caplog):
        monkeypatch.setattr(
            "db.independent_veto_stats",
            lambda hours=24: self._stats(100, 10, {"Hong Kong": 8, "NYC": 2},
                                         "Hong Kong", 0.8))
        with caplog.at_level("ERROR"):
            independent.check_tripwire()
        assert "Hong Kong" in caplog.text
        assert "station-mismatch" in caplog.text
        # Concentration alone must not disable the gate — 10% fire rate is fine.
        assert independent.veto_armed() is True

    def test_measurement_failure_does_not_disable_the_gate(self, monkeypatch, armed):
        """A broken query must not silently remove a safety check."""
        def boom(hours=24):
            raise RuntimeError("db is on fire")
        monkeypatch.setattr("db.independent_veto_stats", boom)
        assert independent.check_tripwire() is None
        assert independent.veto_armed() is True

    def test_reset_re_arms(self, monkeypatch, armed):
        independent._TRIPPED = True
        assert independent.veto_armed() is False
        independent.reset_tripwire()
        assert independent.veto_armed() is True


# --- Config -----------------------------------------------------------------

class TestConfigWiring:
    def test_veto_constants_are_in_the_fingerprint(self):
        for key in ("INDEPENDENT_VETO_ENABLED", "DISAGREEMENT_VETO_F",
                    "PLAUSIBLE_BAND_F", "INDEPENDENT_VETO_MAX_FIRE_RATE"):
            assert key in config._FINGERPRINT_KEYS

    def test_fingerprint_changes_when_a_threshold_moves(self, monkeypatch):
        before = config.config_fingerprint()
        monkeypatch.setattr(config, "DISAGREEMENT_VETO_F", 7.0)
        assert config.config_fingerprint() != before

    def test_defaults_match_the_plan(self):
        assert config.DISAGREEMENT_VETO_F == 5.0
        assert config.PLAUSIBLE_BAND_F == 2.0
        assert config.INDEPENDENT_CACHE_TTL_SECONDS == 21600
        assert config.INDEPENDENT_TIMEOUT_SECONDS == 3.0
        assert config.INDEPENDENT_VETO_MAX_FIRE_RATE == 0.25

    def test_armed_by_default(self):
        """Owner decision 2026-08-05: live from deploy, no shadow period."""
        assert config.INDEPENDENT_VETO_ENABLED is True

    def test_replay_schema_version_bumped(self):
        assert config.REPLAY_SCHEMA_VERSION >= 2

    @pytest.mark.parametrize("key,bad", [
        ("INDEPENDENT_VETO_ENABLED", False),
        ("DISAGREEMENT_VETO_F", 1.0),
        ("DISAGREEMENT_VETO_F", 50.0),
        ("PLAUSIBLE_BAND_F", 20.0),
        ("INDEPENDENT_VETO_MAX_FIRE_RATE", 0.0),
    ])
    def test_stale_env_values_are_caught(self, monkeypatch, key, bad):
        monkeypatch.setattr(config, key, bad)
        problems = config.validate_env_ranges()
        assert any(key in p for p in problems), f"{key}={bad} passed validation"

    def test_clean_config_has_no_veto_problems(self):
        problems = config.validate_env_ranges()
        assert not [p for p in problems if p.startswith("INDEPENDENT")
                    or p.startswith("DISAGREEMENT") or p.startswith("PLAUSIBLE")]


# --- End to end through strategy.evaluate_opportunity -----------------------

@pytest.fixture
def wired(monkeypatch):
    """A throwaway DB with the replay tables, wired into db.py."""
    import tempfile
    path = os.path.join(tempfile.mkdtemp(), "veto.db")
    monkeypatch.setattr(config, "DB_PATH", path)
    import db as dbmod
    monkeypatch.setattr(dbmod, "DB_PATH", path)
    dbmod.init_db()
    return dbmod


class _Opp:
    def __init__(self, **kw):
        defaults = dict(
            market_id="0xabc", token_id_yes="ty", token_id_no="tn",
            city="Tokyo", date="2026-08-07", bucket_low=88.0, bucket_high=89.0,
            yes_price=0.60, no_price=0.30, volume=50000.0,
            hours_to_resolution=36.0, question="q", is_high=True,
        )
        defaults.update(kw)
        for k, v in defaults.items():
            setattr(self, k, v)


def _tight_engine(spread=0.1):
    """An engine tight enough to clear every ensemble gate, so the ONLY thing
    that can refuse the trade is the veto."""
    import weather as W
    raw = {"ecmwf_ifs025": 70.0, "jma_gsm": 70.0 + spread,
           "icon_global": 70.0 - spread, "gem_global": 70.0}
    corr = W.applied_corrections("Tokyo", True, raw)
    temps = {m: v + corr[m] for m, v in raw.items()}
    return W._build_engine_result(temps, "AP", "Tokyo", 36.0, True,
                                  raw_models=raw, corrections=corr)


def _evaluate(monkeypatch, dbmod, opp, engine, forecast):
    import strategy as S
    monkeypatch.setattr(S, "get_live_spread_fraction", lambda t: 0.02)
    monkeypatch.setattr(S, "get_orderbook_depth_usd", lambda t: (500.0, 400.0))
    monkeypatch.setattr(S, "execute_query", dbmod.execute_query)
    monkeypatch.setattr(independent, "get_independent_forecast",
                        lambda *a, **k: forecast)
    return S.evaluate_opportunity(
        opp, {"available_cash": 100.0, "total_equity": 100.0, "locked_cash": 0.0},
        engine_res=engine)


class TestEndToEnd:
    def test_baseline_trade_fires_without_a_veto(self, monkeypatch, wired, armed):
        """The control. Without this, a passing veto test proves nothing —
        the trade might have been refused for an unrelated reason."""
        res = _evaluate(monkeypatch, wired, _Opp(), _tight_engine(),
                        IndependentForecast(NO_DATA, SOURCE_DATAHUB))
        assert res is not None and res["signal"] == "BUY_NO"

    def test_armed_gross_disagreement_refuses_the_trade(self, monkeypatch, wired, armed):
        res = _evaluate(monkeypatch, wired, _Opp(), _tight_engine(),
                        IndependentForecast(DATA, SOURCE_NWS, value_f=88.5,
                                            fetched_at="2026-08-06T00:00:00+00:00"))
        assert res is None, "gross disagreement did not refuse the trade"

        row = wired.fetch_query(
            "SELECT decision, skip_reason, independent_source, independent_value,"
            " disagreement_f, veto_gross, vetoed FROM replay_signals")[-1]
        assert row["decision"] == "SKIP"
        # The refusal reason names the source AND the value, so a human reading
        # the skip log can go straight to checking the station.
        assert SOURCE_NWS in row["skip_reason"]
        assert "88.5" in row["skip_reason"]
        assert row["independent_source"] == SOURCE_NWS
        assert row["independent_value"] == 88.5
        assert row["disagreement_f"] == pytest.approx(15.77, abs=0.1)
        assert row["veto_gross"] == 1
        assert row["vetoed"] == 1

    def test_disabled_gate_lets_the_same_trade_through(self, monkeypatch, wired, armed):
        """Same inputs, tripwire fired: the trade proceeds and the conclusion is
        still on the row. This is the pair that proves `vetoed` tracks the
        EFFECT while veto_gross tracks the CONCLUSION."""
        independent._TRIPPED = True
        res = _evaluate(monkeypatch, wired, _Opp(), _tight_engine(),
                        IndependentForecast(DATA, SOURCE_NWS, value_f=88.5))
        assert res is not None and res["signal"] == "BUY_NO"
        row = wired.fetch_query(
            "SELECT veto_gross, vetoed FROM replay_signals")[-1]
        assert row["veto_gross"] == 1, "conclusion lost while disabled"
        assert row["vetoed"] == 0

    def test_counterfactual_survives_refusal_by_an_earlier_gate(
            self, monkeypatch, wired, armed):
        """§5c. A trade the spread gate already killed still carries the veto's
        opinion — otherwise the counterfactual is a biased sample of exactly the
        question the 14-day review asks."""
        res = _evaluate(monkeypatch, wired, _Opp(), _tight_engine(spread=2.5),
                        IndependentForecast(DATA, SOURCE_NWS, value_f=88.5,
                                            fetched_at="2026-08-06T00:00:00+00:00"))
        assert res is None
        row = wired.fetch_query(
            "SELECT skip_reason, independent_state, independent_value,"
            " disagreement_f, veto_gross, vetoed FROM replay_signals")[-1]
        # An ensemble gate is the reported reason, not the veto — the veto sits
        # last precisely so a trade blocked upstream says what really blocked it.
        assert "independent forecast" not in row["skip_reason"]
        assert "agreement too low" in row["skip_reason"]
        assert row["independent_state"] == DATA
        assert row["independent_value"] == 88.5
        assert row["veto_gross"] == 1, "counterfactual lost behind an earlier gate"

    def test_both_gate_rows_are_stored_for_every_signal(self, monkeypatch, wired, armed):
        _evaluate(monkeypatch, wired, _Opp(), _tight_engine(),
                  IndependentForecast(INCONCLUSIVE, SOURCE_DATAHUB,
                                      detail="HTTP 429"))
        gates = {r["gate"] for r in wired.fetch_query(
            "SELECT gate FROM replay_gates")}
        assert "independent_gross_disagreement" in gates
        assert "independent_bucket_band" in gates

    def test_inconclusive_lets_the_trade_through(self, monkeypatch, wired, armed):
        """A rate limit must never look like a disagreement. This is the whole
        reason the three states exist."""
        res = _evaluate(monkeypatch, wired, _Opp(), _tight_engine(),
                        IndependentForecast(INCONCLUSIVE, SOURCE_NWS,
                                            detail="HTTP 429"))
        assert res is not None and res["signal"] == "BUY_NO"
        row = wired.fetch_query(
            "SELECT independent_state, independent_value, vetoed"
            " FROM replay_signals")[-1]
        assert row["independent_state"] == INCONCLUSIVE
        assert row["independent_value"] is None
        assert row["vetoed"] == 0

    def test_no_data_and_inconclusive_are_distinguishable_in_the_db(
            self, monkeypatch, wired, armed):
        _evaluate(monkeypatch, wired, _Opp(market_id="a"), _tight_engine(),
                  IndependentForecast(NO_DATA, SOURCE_DATAHUB, detail="400"))
        _evaluate(monkeypatch, wired, _Opp(market_id="b"), _tight_engine(),
                  IndependentForecast(INCONCLUSIVE, SOURCE_DATAHUB, detail="429"))
        states = [r["independent_state"] for r in wired.fetch_query(
            "SELECT independent_state FROM replay_signals ORDER BY id")]
        assert states == [NO_DATA, INCONCLUSIVE]

    def test_ensemble_statistics_are_untouched_by_the_veto(
            self, monkeypatch, wired, armed):
        """The independent value must not reach ensemble_mean, sigma,
        model_spread, model_agreement or model_count — in memory or on disk."""
        engine = _tight_engine()
        before = {k: engine[k] for k in
                  ("ensemble_mean", "ensemble_std", "model_spread",
                   "model_agreement", "model_count")}
        _evaluate(monkeypatch, wired, _Opp(), engine,
                  IndependentForecast(DATA, SOURCE_NWS, value_f=88.5))
        for k, v in before.items():
            assert engine[k] == v, f"veto mutated engine_res[{k}]"

        row = wired.fetch_query(
            "SELECT ensemble_mean, weighted_spread_sd, model_agreement,"
            " model_count, sigma_final, independent_value FROM replay_signals")[-1]
        assert row["independent_value"] == 88.5
        assert row["ensemble_mean"] == pytest.approx(before["ensemble_mean"])
        assert row["weighted_spread_sd"] == pytest.approx(before["model_spread"])
        assert row["model_agreement"] == pytest.approx(before["model_agreement"])
        assert row["model_count"] == before["model_count"]
        for field in ("ensemble_mean", "weighted_spread_sd", "sigma_final"):
            assert row[field] != 88.5


class TestTripwireQuery:
    """db.independent_veto_stats — the denominator is the whole point."""

    def _signal(self, dbmod, **kw):
        row = {
            "timestamp": kw.get("timestamp", "2099-01-01T00:00:00+00:00"),
            "schema_version": 2, "config_fingerprint": "x", "mode": "paper",
            "city_key": kw.get("city_key", "NYC"),
            "independent_state": kw.get("state", DATA),
            "veto_gross": int(kw.get("gross", 0)),
            "veto_band": int(kw.get("band", 0)),
            "vetoed": int(kw.get("vetoed", 0)),
        }
        gates = [{"gate": "edge_threshold", "observed": 0.1, "threshold": 0.08,
                  "passed": kw.get("other_gates_pass", True), "detail": ""}]
        if kw.get("gross") or kw.get("band"):
            gates.append({"gate": "independent_gross_disagreement",
                          "observed": 9.0, "threshold": 5.0, "passed": False,
                          "detail": ""})
        return dbmod.log_replay_signal(row, gates)

    def test_denominator_excludes_signals_other_gates_refused(self, wired):
        """Most evaluations fail the edge threshold and never reach the veto.
        Counting them would divide by thousands and guarantee the tripwire never
        fires no matter how badly the gate misbehaves."""
        for _ in range(3):
            self._signal(wired, other_gates_pass=True)
        for _ in range(50):
            self._signal(wired, other_gates_pass=False)
        stats = wired.independent_veto_stats(hours=24 * 365 * 100)
        assert stats["considered"] == 3

    def test_denominator_excludes_states_that_cannot_veto(self, wired):
        """NO_DATA and INCONCLUSIVE signals measure provider COVERAGE, not gate
        behaviour. With no DataHub key 40 of 51 cities are permanently
        INCONCLUSIVE, so counting them would let the veto fire on every single US
        signal — a total failure of the armed cities — while reporting ~22% and
        never tripping."""
        for _ in range(40):
            self._signal(wired, state=INCONCLUSIVE, city_key="Tokyo")
        for _ in range(11):
            self._signal(wired, state=DATA, city_key="NYC", gross=True, vetoed=True)
        stats = wired.independent_veto_stats(hours=24 * 365 * 100)
        assert stats["considered"] == 11
        assert stats["fired"] == 11
        assert stats["fire_rate"] == 1.0, "a total failure must read as 100%"
        # The looser denominator stays reportable.
        assert stats["all_gate_passing"] == 51

    def test_no_data_does_not_dilute_the_rate(self, wired):
        for _ in range(30):
            self._signal(wired, state=NO_DATA, city_key="Tokyo")
        for _ in range(2):
            self._signal(wired, state=DATA, city_key="NYC", gross=True, vetoed=True)
        for _ in range(2):
            self._signal(wired, state=DATA, city_key="NYC")
        stats = wired.independent_veto_stats(hours=24 * 365 * 100)
        assert stats["considered"] == 4
        assert stats["fire_rate"] == 0.5

    def test_veto_gate_failure_does_not_remove_a_signal_from_the_denominator(self, wired):
        """The veto's own failing gate row must not exclude the signal — that
        would drive the measured rate to zero exactly when it matters."""
        self._signal(wired, gross=True, vetoed=True, other_gates_pass=True)
        stats = wired.independent_veto_stats(hours=24 * 365 * 100)
        assert stats["considered"] == 1
        assert stats["fired"] == 1
        assert stats["fire_rate"] == 1.0

    def test_fired_counts_the_conclusion_not_the_effect(self, wired):
        """Otherwise disabling the gate drives the rate to zero and it re-arms
        itself into the same storm."""
        self._signal(wired, gross=True, vetoed=False)
        stats = wired.independent_veto_stats(hours=24 * 365 * 100)
        assert stats["fired"] == 1

    def test_city_concentration_is_reported(self, wired):
        for _ in range(8):
            self._signal(wired, city_key="Hong Kong", gross=True, vetoed=True)
        for _ in range(2):
            self._signal(wired, city_key="NYC", gross=True, vetoed=True)
        stats = wired.independent_veto_stats(hours=24 * 365 * 100)
        assert stats["top_city"] == "Hong Kong"
        assert stats["top_city_share"] == pytest.approx(0.8)
        assert stats["by_city"] == {"Hong Kong": 8, "NYC": 2}

    def test_rows_without_a_veto_record_are_excluded(self, wired):
        """Pre-deploy rows have no independent_state. They are not evidence of a
        veto that did not fire."""
        wired.log_replay_signal(
            {"timestamp": "2099-01-01T00:00:00+00:00", "schema_version": 1,
             "config_fingerprint": "old", "city_key": "NYC"},
            [{"gate": "edge_threshold", "observed": 0.1, "threshold": 0.08,
              "passed": True, "detail": ""}])
        stats = wired.independent_veto_stats(hours=24 * 365 * 100)
        assert stats["considered"] == 0

    def test_window_excludes_old_rows(self, wired):
        self._signal(wired, timestamp="1999-01-01T00:00:00+00:00",
                     gross=True, vetoed=True)
        stats = wired.independent_veto_stats(hours=24)
        assert stats["considered"] == 0
        assert stats["fire_rate"] == 0.0

    def test_empty_window_is_not_a_division_by_zero(self, wired):
        stats = wired.independent_veto_stats(hours=24)
        assert stats["considered"] == 0
        assert stats["fire_rate"] == 0.0
        assert stats["top_city"] is None


# --- Live provider reachability (network) -----------------------------------

@pytest.mark.skipif(
    os.getenv("RUN_LIVE_PROVIDER_TESTS", "").lower() not in ("1", "true", "yes"),
    reason="network test — set RUN_LIVE_PROVIDER_TESTS=1 to run")
class TestLiveProviders:
    """§7's last item: every US city resolves to an NWS gridpoint, every non-US
    city returns a DataHub value.

    OPT-IN, not marker-deselected. CI runs a bare `pytest tests/ -q` and is
    deliberately hermetic — a test that reaches api.weather.gov would make this
    repository's CI fail on NWS's outages rather than on its own changes. Run it
    by hand (RUN_LIVE_PROVIDER_TESTS=1) when the routing table changes, because
    it is the only check that routing matches what the providers actually serve,
    and the coverage matrix's whole lesson is that this must be measured."""

    def test_every_us_city_resolves_to_a_gridpoint(self):
        independent.reset_state()
        failures = []
        for city in sorted(NWS_CITIES):
            url, state, detail = independent._nws_gridpoint(
                city, STATIONS[city]["lat"], STATIONS[city]["lon"])
            if state != DATA or not url:
                failures.append(f"{city}: {state} {detail}")
        assert not failures, failures

    @pytest.mark.skipif(not config.METOFFICE_DATAHUB_KEY,
                        reason="METOFFICE_DATAHUB_KEY not set")
    def test_every_non_us_city_returns_a_datahub_value(self):
        independent.reset_state()
        from datetime import datetime, timedelta, timezone as _tz
        target = (datetime.now(_tz.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
        failures = []
        for city in sorted(set(STATIONS) - NWS_CITIES):
            res = get_independent_forecast(city, target, True)
            if res.state != DATA:
                failures.append(f"{city}: {res.state} {res.detail}")
        assert not failures, failures
