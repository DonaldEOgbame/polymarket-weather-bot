"""Open-Meteo coverage probing.

The whole value of this script is that it does not draw the wrong conclusion
from an error, and there are two ways to draw the wrong conclusion:

  BATCHING BY MODEL. A request naming several models returns HTTP 400 for ALL
      of them if any single ID is invalid, so the probe blames every model in
      the batch. That is the documented origin of the "GFS unavailable in the
      Southern Hemisphere" belief embedded in weather.STATIONS.

  BATCHING BY COORDINATE. Verified 2026-08-05: a 400 reading "No data is
      available for this location" means OUT-OF-DOMAIN, not invalid ID.
      gfs_hrrr, ncep_nbm_conus and ncep_nam_conus all return 200 at Chicago and
      400 at Tokyo. So one out-of-domain coordinate 400s the whole chunk, and a
      naive coordinate batch reports "no data anywhere" for every limited-area
      model — the same error as above, on the other axis.

The second one was introduced while speeding this script up and caught by
probing a limited-area model at an in-domain coordinate. These tests exist so it
cannot come back.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import coverage_matrix as CM


class TestOneModelPerRequest:
    def test_the_request_names_exactly_one_model(self, monkeypatch):
        """A multi-model request cannot distinguish 'this model has no data
        here' from 'some other model in the batch has a typo'."""
        seen = []

        class _R:
            status_code = 200

            def json(self):
                return [{"hourly": {"time": [], "temperature_2m": [1.0] * 96}}]

        monkeypatch.setattr(CM.requests, "get",
                            lambda url, params=None, timeout=None:
                            (seen.append(params), _R())[1])
        CM.probe_model("ecmwf_ifs025", ["Chicago"])
        assert all("," not in p["models"] for p in seen)


class TestOutOfDomainIsNotInvalidId:
    def test_a_400_chunk_falls_back_to_per_coordinate(self, monkeypatch):
        """The regression this file exists for. One out-of-domain coordinate
        must not condemn the whole chunk."""
        calls = []

        def fake(url, params=None, timeout=None):
            lats = params["latitude"].split(",")
            calls.append(len(lats))

            class _R:
                pass
            r = _R()
            # Chicago in domain, Tokyo out. A chunk containing both 400s.
            if any(l.startswith("35.5") for l in lats):
                r.status_code = 400
                r.text = "No data is available for this location"
                r.json = lambda: {"reason": "No data is available for this location"}
            else:
                r.status_code = 200
                r.json = lambda: [{"hourly": {"temperature_2m": [70.0] * 96}}
                                  for _ in lats]
            return r

        monkeypatch.setattr(CM.requests, "get", fake)
        monkeypatch.setattr(CM.time, "sleep", lambda *a: None)
        out = CM.probe_model("gfs_hrrr", ["Chicago", "Tokyo"])
        assert out["Chicago"]["ok"] is True, "an in-domain city was condemned by its chunkmate"
        assert out["Tokyo"]["ok"] is False
        assert max(calls) > 1 and 1 in calls, "no per-coordinate fallback happened"

    def test_a_limited_area_model_maps_its_domain_rather_than_reading_as_dead(
            self, monkeypatch):
        """Without the fallback, gfs_hrrr reads as 0/51 — 'NO DATA ANYWHERE' —
        and would be dropped from the candidate list as an invalid ID."""
        def fake(url, params=None, timeout=None):
            lats = params["latitude"].split(",")

            class _R:
                pass
            r = _R()
            if any(float(l) > 50 or float(l) < 20 for l in lats):
                r.status_code = 400
                r.text = "No data is available for this location"
                r.json = lambda: {"reason": "No data"}
            else:
                r.status_code = 200
                r.json = lambda: [{"hourly": {"temperature_2m": [70.0] * 96}}
                                  for _ in lats]
            return r

        monkeypatch.setattr(CM.requests, "get", fake)
        monkeypatch.setattr(CM.time, "sleep", lambda *a: None)
        out = CM.probe_model("gfs_hrrr", ["Chicago", "Austin", "Helsinki", "Singapore"])
        assert out["Chicago"]["ok"] and out["Austin"]["ok"]
        assert not out["Helsinki"]["ok"] and not out["Singapore"]["ok"]


class TestErrorsDoNotCrashOrLie:
    def test_a_non_json_error_page_does_not_crash(self, monkeypatch):
        """The first full run called .json() on an error page, crashed on model
        20 of 32, and lost every result before it."""
        class _R:
            status_code = 503
            text = "<html>gateway timeout</html>"

            def json(self):
                raise ValueError("not json")

        monkeypatch.setattr(CM.requests, "get", lambda *a, **k: _R())
        monkeypatch.setattr(CM.time, "sleep", lambda *a: None)
        out = CM.probe_model("ecmwf_ifs025", ["Chicago"])
        assert out["Chicago"]["ok"] is False
        assert out["Chicago"]["status"] == 503

    def test_a_timeout_is_recorded_as_unknown_not_as_absent(self, monkeypatch):
        """jma_gsm timed out on a 51-coordinate request and is very much in
        production. A timeout must never be written down as 'no data'."""
        monkeypatch.setattr(CM.requests, "get",
                            lambda *a, **k: (_ for _ in ()).throw(
                                CM.requests.ReadTimeout("slow")))
        monkeypatch.setattr(CM.time, "sleep", lambda *a: None)
        out = CM.probe_model("jma_gsm", ["Tokyo"])
        assert out["Tokyo"]["status"] == "ReadTimeout"
        assert out["Tokyo"]["status"] != "no_series"

    def test_partial_results_are_written_as_it_goes(self, monkeypatch, tmp_path):
        """The first run spent an hour and lost everything to a crash on the
        twentieth model."""
        path = str(tmp_path / "partial.json")
        monkeypatch.setattr(CM, "probe_model",
                            lambda m, c, **k: {x: {"ok": True, "leads": {},
                                                   "status": 200} for x in c})
        monkeypatch.setattr(CM.time, "sleep", lambda *a: None)
        CM.run(["a", "b"], ["Chicago"], partial_path=path)
        import json
        assert set(json.load(open(path))) == {"a", "b"}


class TestHorizon:
    def test_a_horizon_limited_model_reports_where_it_stops(self, monkeypatch):
        """icon_d2 simply will not return past 48h, and that is a fact the
        per-city model lists need rather than a reason to drop it."""
        class _R:
            status_code = 200

            def json(self):
                return [{"hourly": {"temperature_2m":
                                    [70.0] * 49 + [None] * 47}}]

        monkeypatch.setattr(CM.requests, "get", lambda *a, **k: _R())
        monkeypatch.setattr(CM.time, "sleep", lambda *a: None)
        out = CM.probe_model("icon_d2", ["Berlin"])["Berlin"]
        assert out["leads"][24] is True
        assert out["leads"][48] is True
        assert out["leads"][72] is False
        assert out["horizon_h"] == 48
