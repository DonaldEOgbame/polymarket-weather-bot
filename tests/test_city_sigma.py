"""Per-city sigma scales (CITY_SIGMA_SCALES, 2026-08-12).

The properties that matter: the parser is all-or-nothing (a typo refuses to
boot rather than silently leaving a city on the global scale), an unfitted
side falls back to the global scale, a fitted city skips the convective
multiplier (the fit absorbed it), and the table participates in the config
fingerprint so a scale change splits replay history.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import config as C
import weather as W


class TestParser:
    def test_empty_string_is_valid_and_empty(self):
        assert C._parse_city_sigma("") == {}
        assert C._parse_city_sigma("  ") == {}

    def test_parses_direction_split_entries(self):
        out = C._parse_city_sigma("Dallas:1.31:0,Seoul:0.94:1.10")
        assert out == {("Dallas", True): 1.31,
                       ("Seoul", True): 0.94, ("Seoul", False): 1.10}

    def test_zero_field_means_global_fallback(self):
        out = C._parse_city_sigma("Dallas:1.31:0")
        assert ("Dallas", False) not in out

    def test_two_field_entry_refuses(self):
        with pytest.raises(ValueError):
            C._parse_city_sigma("Dallas:1.31")

    def test_unparseable_scale_refuses(self):
        with pytest.raises(ValueError):
            C._parse_city_sigma("Dallas:big:0")

    def test_trailing_comma_refuses(self):
        with pytest.raises(ValueError):
            C._parse_city_sigma("Dallas:1.31:0,")

    def test_empty_city_refuses(self):
        with pytest.raises(ValueError):
            C._parse_city_sigma(":1.31:0")


class TestSigmaLookup:
    def _stages(self, monkeypatch, table, city, is_high=True, spread=0.5):
        monkeypatch.setattr(W, "CITY_SIGMA_SCALES", table)
        return W.compute_sigma_stages(spread, 12.0, is_high, city_key=city)

    def test_fitted_city_uses_its_scale(self, monkeypatch):
        s = self._stages(monkeypatch, {("Seoul", True): 1.5}, "Seoul")
        assert s["direction_scale"] == 1.5 and s["city_scaled"] is True

    def test_unfitted_city_falls_back_to_global(self, monkeypatch):
        s = self._stages(monkeypatch, {("Seoul", True): 1.5}, "Tokyo")
        assert s["direction_scale"] == W.SIGMA_SCALE_HIGH
        assert s["city_scaled"] is False

    def test_unfitted_direction_falls_back_to_global(self, monkeypatch):
        s = self._stages(monkeypatch, {("Seoul", True): 1.5}, "Seoul",
                         is_high=False)
        assert s["direction_scale"] == W.SIGMA_SCALE_LOW

    def test_fitted_convective_city_skips_inflation(self, monkeypatch):
        assert "Houston" in W.CONVECTIVE_CITIES
        s = self._stages(monkeypatch, {("Houston", True): 1.4}, "Houston")
        assert s["convective"] is False
        assert s["post_convective"] == pytest.approx(s["post_direction"])

    def test_unfitted_convective_city_keeps_inflation(self, monkeypatch):
        s = self._stages(monkeypatch, {}, "Houston")
        assert s["convective"] is True
        assert s["post_convective"] == pytest.approx(
            s["post_direction"] * W.CONVECTIVE_STD_INFLATION)


class TestCouplings:
    def test_fingerprint_changes_with_a_scale(self, monkeypatch):
        before = C.config_fingerprint()
        monkeypatch.setattr(C, "CITY_SIGMA_SCALES", {("Seoul", True): 1.5})
        assert C.config_fingerprint() != before

    def test_phantom_city_is_caught_by_the_validator(self, monkeypatch):
        monkeypatch.setattr(W, "CITY_SIGMA_SCALES", {("Tampa", True): 1.2})
        problems = W.validate_city_tables()
        assert any("CITY_SIGMA_SCALES" in p and "Tampa" in p for p in problems)

    def test_out_of_range_scale_is_flagged(self, monkeypatch):
        monkeypatch.setattr(C, "CITY_SIGMA_SCALES", {("Seoul", True): 3.7})
        assert any("CITY_SIGMA_SCALES" in p for p in C.validate_env_ranges())

    def test_replay_mirrors_the_lookup(self):
        """replay.ConfigOverride must expose the same knob, or a replay of a
        per-city config would silently use the global scales."""
        import replay
        assert hasattr(replay.ConfigOverride(), "city_sigma_scales")
