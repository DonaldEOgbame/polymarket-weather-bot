"""Intraday observation conditioning.

Nothing in this bot looked at what the day had already done. At 15:00 local with
the station reading 91°F, it was still pricing "will the max be below 91?" off a
00Z forecast — a question whose answer is already known and is no.

Two mechanisms, tested separately because they fail differently:

  HARD BOUND — arithmetic. A bucket entirely below an observed maximum must
      price at exactly 0.0, not at something small. This is the half that is
      worth most, and it needs no fitted constant to be correct.
  REMAINING RISE — statistics. How much rise is left, as a fitted fraction of
      the day's diurnal range. Wrong here costs accuracy; wrong in the hard
      bound costs money on a bet that could not win.

And the property that matters more than either: the whole thing must fall
through silently when observations are unavailable, because that is the common
case for a market 48 hours out.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import config as C
import intraday as I
import weather as W


def _engine(mean=85.0, std=3.0, is_high=True, city="Chicago", diurnal=18.0):
    return {"ensemble_mean": mean, "ensemble_std": std, "is_high": is_high,
            "city_key": city, "forecast_diurnal_range_f": diurnal,
            "model_spread_std": 0.5, "model_agreement": 1.0}


class TestTheHardBoundIsArithmeticNotProbability:
    def test_a_bucket_below_the_observed_max_prices_at_exactly_zero(self):
        """The single most valuable consequence. If 91°F has been recorded, the
        market "will the max be 85-86°F" cannot pay YES, and no ensemble
        disagreement makes it a 3% shot."""
        eng = dict(_engine(mean=85.0), hard_bound=91.0, hard_bound_is_floor=True)
        assert W.get_bucket_probability(eng, 85.0, 86.0) == 0.0

    def test_a_bucket_above_the_observed_min_prices_at_exactly_zero(self):
        eng = dict(_engine(mean=60.0, is_high=False),
                   hard_bound=55.0, hard_bound_is_floor=False)
        assert W.get_bucket_probability(eng, 58.0, 59.0) == 0.0

    def test_a_bucket_containing_the_bound_keeps_mass(self):
        eng = dict(_engine(mean=92.0), hard_bound=91.0, hard_bound_is_floor=True)
        p = W.get_bucket_probability(eng, 91.0, 92.0)
        assert 0.0 < p < 1.0

    def test_without_a_bound_nothing_changes(self):
        """The unconditioned path must be bit-identical — it is what every
        market more than a few hours out uses."""
        eng = _engine()
        assert (W.get_bucket_probability(eng, 85.0, 86.0)
                == W.get_bucket_probability(dict(eng, hard_bound=None), 85.0, 86.0))

    def test_the_truncated_distribution_still_integrates_to_one(self):
        """Renormalisation, not just clipping. Clipping alone would leave the
        probabilities summing to less than 1 and quietly understate every
        bucket above the bound."""
        eng = dict(_engine(mean=92.0, std=3.0), hard_bound=90.0,
                   hard_bound_is_floor=True)
        total = W._truncated_cdf(200.0, 92.0, 3.0, 90.0, True)
        assert total == pytest.approx(1.0, abs=1e-6)
        assert W._truncated_cdf(90.0, 92.0, 3.0, 90.0, True) == 0.0

    def test_an_observation_far_beyond_the_model_does_not_divide_by_zero(self):
        """The model said 70°F and the station has already recorded 105°F.
        CDF(bound) is 1.0 to float precision, so naive renormalisation is 0/0.
        It must degrade, not explode."""
        p = W._truncated_cdf(106.0, 70.0, 2.0, 105.0, True)
        assert 0.0 <= p <= 1.0


class TestRemainingRise:
    def test_the_fraction_decreases_through_the_day(self):
        """Whatever the fit produced, more of the diurnal rise must remain at
        08:00 than at 18:00, or the table is upside down."""
        morning = I.remaining_fraction(8.0, is_high=True)
        evening = I.remaining_fraction(18.0, is_high=True)
        assert morning and evening
        assert morning[0] > evening[0]

    def test_it_is_interpolated_between_hours(self):
        """The curve is steep through the late morning; snapping 11:59 back to
        11:00 there is worth a real fraction of a degree."""
        a = I.remaining_fraction(10.0, True)[0]
        b = I.remaining_fraction(11.0, True)[0]
        mid = I.remaining_fraction(10.5, True)[0]
        assert min(a, b) <= mid <= max(a, b)
        assert mid != a

    def test_fractions_are_bounded(self):
        for h in range(24):
            f, fsd = I.remaining_fraction(float(h), True)
            g, gsd = I.remaining_fraction(float(h), False)
            assert 0.0 <= f <= 1.2, f"f({h})={f}"
            assert 0.0 <= g <= 1.2, f"g({h})={g}"
            assert fsd >= 0 and gsd >= 0


class TestConditioning:
    def test_the_mean_is_lifted_to_at_least_the_observation(self, monkeypatch):
        """The model said 85°F; the station has already recorded 91°F. The
        conditioned mean cannot remain below what has happened."""
        monkeypatch.setattr(I, "local_hour", lambda c, d: 14.0)
        monkeypatch.setattr(I, "observed_extreme", lambda c, d, h: 91.0)
        out = I.condition(_engine(mean=85.0), "2026-08-05")
        assert out["ensemble_mean"] >= 91.0
        assert out["hard_bound"] == 91.0
        assert out["intraday"]["applied"] is True

    def test_a_higher_model_forecast_survives(self, monkeypatch):
        """max(), not a blend: the ensemble may know about a late frontal surge
        the climatology cannot, and taking the larger keeps that."""
        monkeypatch.setattr(I, "local_hour", lambda c, d: 14.0)
        monkeypatch.setattr(I, "observed_extreme", lambda c, d, h: 80.0)
        out = I.condition(_engine(mean=98.0), "2026-08-05")
        assert out["ensemble_mean"] == 98.0

    def test_sigma_shrinks_but_never_grows(self, monkeypatch):
        """An observation can only remove uncertainty."""
        monkeypatch.setattr(I, "local_hour", lambda c, d: 16.0)
        monkeypatch.setattr(I, "observed_extreme", lambda c, d, h: 91.0)
        out = I.condition(_engine(mean=85.0, std=3.0), "2026-08-05")
        assert out["ensemble_std"] <= 3.0
        assert out["ensemble_std"] >= C.INTRADAY_SIGMA_FLOOR_F

    def test_sigma_never_reaches_zero(self, monkeypatch):
        """Late in the day the fitted sd approaches zero. A sigma of literally
        zero prices every bucket at 0 or 1 — betting the account on one METAR
        reading and on no correction ever being issued."""
        monkeypatch.setattr(I, "local_hour", lambda c, d: 23.5)
        monkeypatch.setattr(I, "observed_extreme", lambda c, d, h: 91.0)
        out = I.condition(_engine(mean=85.0), "2026-08-05")
        assert out["ensemble_std"] >= C.INTRADAY_SIGMA_FLOOR_F > 0

    def test_low_markets_are_mirrored(self, monkeypatch):
        """Expected to pay most here: an overnight minimum is usually set before
        dawn, so the answer sits in the observations for hours before
        settlement."""
        monkeypatch.setattr(I, "local_hour", lambda c, d: 10.0)
        monkeypatch.setattr(I, "observed_extreme", lambda c, d, h: 55.0)
        out = I.condition(_engine(mean=60.0, is_high=False), "2026-08-05")
        assert out["ensemble_mean"] <= 55.0
        assert out["hard_bound"] == 55.0
        assert out["hard_bound_is_floor"] is False

    def test_the_input_is_not_mutated(self, monkeypatch):
        """prefetch_signal_engines hands one engine result to several markets."""
        monkeypatch.setattr(I, "local_hour", lambda c, d: 14.0)
        monkeypatch.setattr(I, "observed_extreme", lambda c, d, h: 91.0)
        eng = _engine(mean=85.0)
        I.condition(eng, "2026-08-05")
        assert eng["ensemble_mean"] == 85.0 and "hard_bound" not in eng

    def test_every_input_is_recorded_for_the_replay_log(self, monkeypatch):
        monkeypatch.setattr(I, "local_hour", lambda c, d: 14.0)
        monkeypatch.setattr(I, "observed_extreme", lambda c, d, h: 91.0)
        rec = I.condition(_engine(mean=85.0), "2026-08-05")["intraday"]
        for k in ("local_hour", "observed", "remaining_fraction",
                  "forecast_diurnal_range_f", "expected_remaining_f",
                  "mean_before", "mean_after", "sigma_before", "sigma_after"):
            assert k in rec, f"{k} missing — the decision cannot be reconstructed"


class TestItFallsThroughCleanly:
    """The common case is a market 48 hours out with nothing observed. That path
    must be silent, cheap and bit-identical to the old behaviour."""

    def test_a_future_day_is_untouched(self, monkeypatch):
        monkeypatch.setattr(I, "local_hour", lambda c, d: None)
        eng = _engine()
        assert I.condition(eng, "2026-08-09") is eng

    def test_before_dawn_is_untouched(self, monkeypatch):
        """Pre-dawn the running max is just the overnight low and says almost
        nothing about the peak."""
        monkeypatch.setattr(I, "local_hour", lambda c, d: 3.0)
        eng = _engine()
        assert I.condition(eng, "2026-08-05") is eng

    def test_missing_observations_are_untouched(self, monkeypatch):
        monkeypatch.setattr(I, "local_hour", lambda c, d: 14.0)
        monkeypatch.setattr(I, "observed_extreme", lambda c, d, h: None)
        eng = _engine()
        assert I.condition(eng, "2026-08-05") is eng

    def test_a_missing_diurnal_range_is_untouched(self, monkeypatch):
        """Without the range the fitted fraction cannot be turned into degrees.
        Falling through beats guessing an amplitude."""
        monkeypatch.setattr(I, "local_hour", lambda c, d: 14.0)
        monkeypatch.setattr(I, "observed_extreme", lambda c, d, h: 91.0)
        eng = _engine(diurnal=None)
        assert I.condition(eng, "2026-08-05") is eng

    def test_the_flag_disables_it(self, monkeypatch):
        monkeypatch.setattr(I, "ENABLE_INTRADAY_CONDITIONING", False)
        eng = _engine()
        assert I.condition(eng, "2026-08-05") is eng

    def test_a_broken_observation_feed_does_not_stop_trading(self, monkeypatch):
        """intraday.condition reads live METAR on the evaluation path. A slow or
        broken feed must degrade to the unconditioned forecast, not to no
        trading at all."""
        import intraday
        monkeypatch.setattr(intraday, "condition",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        eng = _engine()
        assert W._condition_on_observations(eng, "2026-08-05") is eng


class TestDiurnalRange:
    def test_it_is_max_mean_minus_min_mean(self):
        w = {"a": 0.5, "b": 0.5}
        assert W._diurnal_range({"a": 90.0, "b": 92.0}, {"a": 70.0, "b": 72.0},
                                w, is_high=True) == pytest.approx(20.0)

    def test_it_is_positive_for_the_low_direction_too(self):
        w = {"a": 1.0}
        assert W._diurnal_range({"a": 70.0}, {"a": 90.0}, w,
                                is_high=False) == pytest.approx(20.0)

    def test_only_members_present_in_both_directions_count(self):
        """A member returning a max but no min would shift one end of the range
        and not the other, inventing amplitude out of a coverage gap."""
        w = {"a": 0.5, "b": 0.5}
        r = W._diurnal_range({"a": 90.0, "b": 99.0}, {"a": 70.0}, w, is_high=True)
        assert r == pytest.approx(20.0)

    def test_a_nonsensical_range_is_none(self):
        w = {"a": 1.0}
        assert W._diurnal_range({"a": 70.0}, {"a": 90.0}, w, is_high=True) is None
        assert W._diurnal_range({"a": 70.0}, None, w, is_high=True) is None


class TestWiredIntoTheEvaluationPath:
    def test_both_engine_builders_condition(self):
        src = open(os.path.join(os.path.dirname(__file__), "..", "weather.py")).read()
        assert src.count("_condition_on_observations(") >= 3, (
            "get_signal_engine and prefetch_signal_engines must both condition, "
            "or a scan and a single evaluation would price the same market "
            "differently"
        )

    def test_the_pipeline_version_records_this_phase(self):
        assert C.FORECAST_PIPELINE_VERSION >= 3, (
            "conditioning changes probabilities materially; the replay log must "
            "be able to separate rows before and after"
        )
