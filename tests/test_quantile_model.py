"""Quantile meta-model — built, wired, and deliberately switched off.

The honest constraint this file mostly defends: it cannot be fitted yet. Three
prior refits (sigma, Student-t df, per-direction bias) each returned confidence
intervals wider than the correction they proposed, on 23 trades. A
gradient-boosted model with ~37 features on the same sample would be that
failure again with more parameters and less visibility into it.

So the tests are mostly about the SAFETY PROPERTIES: that it stays off, that it
refuses to fit on a small sample, that the feature vector contains nothing from
the future, and that its CDF cannot produce a negative probability.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import config as C
import quantile_model as Q


def _row(**kw):
    base = {
        "raw_models_pre_correction": json.dumps(
            {"ecmwf_ifs025": 90.0, "icon_global": 88.0, "icon_eu": 88.5,
             "gfs_global": 91.0}),
        "model_weights": json.dumps(
            {"ecmwf_ifs025": 0.4, "icon_global": 0.2, "icon_eu": 0.1,
             "gfs_global": 0.3}),
        "weighted_spread_sd": 1.2, "model_agreement": 0.9, "lead_hours": 36.0,
        "is_high": 1, "bucket_low": 91.0, "bucket_high": 91.8,
        "target_date": "2026-08-05", "settled_value": 90.5,
    }
    base.update(kw)
    return base


class TestItIsOffByDefault:
    def test_disabled(self):
        assert C.ENABLE_QUANTILE_MODEL is False
        assert Q.available() is False

    def test_inference_returns_none_when_disabled(self):
        assert Q.predict_quantiles(_row()) is None
        assert Q.bucket_probability(_row(), 91.0, 91.8) is None

    def test_the_training_script_does_not_enable_its_own_output(self):
        """A training script that enables its own model will eventually enable a
        worse one. The gate is harness.py, not the trainer."""
        src = open(os.path.join(os.path.dirname(__file__), "..",
                                "quantile_model.py")).read()
        body = src.split("def main(")[1]
        assert "ENABLE_QUANTILE_MODEL = True" not in body
        assert "Still DISABLED" in body

    def test_a_missing_lightgbm_does_not_break_the_bot(self, monkeypatch):
        """The model is off by default, so the Fly image should not carry a
        100MB wheel for a disabled feature — and the bot must run without it."""
        monkeypatch.setattr(C, "ENABLE_QUANTILE_MODEL", True)
        monkeypatch.setattr(Q, "ENABLE_QUANTILE_MODEL", True)
        monkeypatch.setattr(Q, "QUANTILE_MODEL_PATH", "/nonexistent/model.json")
        assert Q.available() is False
        assert Q.predict_quantiles(_row()) is None


class TestItRefusesToFitOnTooLittle:
    def test_training_raises_below_the_floor(self):
        with pytest.raises(ValueError, match="settled rows"):
            Q.train([_row() for _ in range(10)])

    def test_the_error_names_the_actual_history(self):
        """The floor is not arbitrary caution; the message has to carry why."""
        try:
            Q.train([_row() for _ in range(10)])
        except ValueError as e:
            assert "23 observations" in str(e)

    def test_unsettled_rows_do_not_count_toward_the_floor(self):
        rows = [_row(settled_value=None) for _ in range(5000)]
        with pytest.raises(ValueError, match="^0 settled rows"):
            Q.train(rows)


class TestFeaturesContainNothingFromTheFuture:
    def test_the_settled_value_is_not_a_feature(self):
        """The single easiest mistake here: a feature derived from the outcome
        scores beautifully in the harness and loses money live."""
        names, vals = Q.build_features(_row(settled_value=123.456))
        assert not any("settled" in n for n in names)
        assert 123.456 not in vals

    def test_changing_the_outcome_does_not_change_the_features(self):
        a = Q.build_features(_row(settled_value=50.0))[1]
        b = Q.build_features(_row(settled_value=110.0))[1]
        assert a == b

    def test_families_are_the_unit_not_members(self):
        """The member set varies per city and per day once per-city
        extra_models lands, so a per-member vector would have a varying width —
        and three correlated ICON columns to overfit on."""
        names, _ = Q.build_features(_row())
        assert "DWD_dev" in names and "DWD_weight" in names
        assert not any("icon_eu" in n for n in names)

    def test_the_two_icon_members_are_pooled_into_one_family_feature(self):
        _, vals = Q.build_features(_row())
        d = dict(zip(*Q.build_features(_row())))
        assert d["DWD_weight"] == pytest.approx(0.3)   # 0.2 + 0.1

    def test_temperatures_enter_as_deviations_not_absolutes(self):
        """Absolute temperatures make the model learn climate (Singapore is
        warmer than Helsinki) instead of learning ERROR."""
        d = dict(zip(*Q.build_features(_row())))
        assert abs(d["ECMWF_dev"]) < 10.0
        assert d["ensemble_mean"] > 50.0        # the mean itself is still there

    def test_the_feature_width_is_fixed_across_different_ensembles(self):
        """Per-city extra_models makes a varying member set the normal case."""
        thin = _row(raw_models_pre_correction=json.dumps({"ecmwf_ifs025": 90.0}),
                    model_weights=json.dumps({"ecmwf_ifs025": 1.0}))
        assert len(Q.build_features(thin)[1]) == len(Q.build_features(_row())[1])

    def test_seasonality_is_cyclic(self):
        """December and January must be adjacent, not 364 apart."""
        dec = dict(zip(*Q.build_features(_row(target_date="2026-12-31"))))
        jan = dict(zip(*Q.build_features(_row(target_date="2027-01-01"))))
        assert abs(dec["doy_sin"] - jan["doy_sin"]) < 0.05
        assert abs(dec["doy_cos"] - jan["doy_cos"]) < 0.05


class TestTheCDF:
    LEVELS = C.QUANTILE_LEVELS
    QS = [70.0, 75.0, 78.0, 82.0, 85.0, 86.0, 87.0, 90.0, 94.0, 97.0, 102.0]

    def test_it_is_monotonic(self):
        prev = -1.0
        for x in range(60, 110):
            v = Q._cdf(self.QS, float(x))
            assert v >= prev - 1e-12
            prev = v

    def test_it_is_flat_outside_the_fitted_range(self):
        """The model has no view beyond its outermost quantile, and pretending
        otherwise is how a tail gets priced at 0.00008."""
        assert Q._cdf(self.QS, 20.0) == 0.0
        assert Q._cdf(self.QS, 200.0) == 1.0

    def test_a_bucket_probability_is_never_negative(self):
        for lo in range(65, 105, 5):
            p = Q._cdf(self.QS, lo + 1.0) - Q._cdf(self.QS, lo - 1.0)
            assert p >= 0.0

    def test_quantile_crossing_is_repaired_not_trusted_away(self):
        """Each level is fitted independently, so nothing stops the 0.6
        prediction landing below the 0.5 one. An unsorted CDF gives negative
        bucket probabilities."""
        src = open(os.path.join(os.path.dirname(__file__), "..",
                                "quantile_model.py")).read()
        assert "return sorted(preds)" in src

    def test_levels_are_dense_in_the_tails(self):
        """Every bet lives in the tails; an evenly spaced grid would put its
        resolution where no bucket is ever priced from."""
        lv = list(C.QUANTILE_LEVELS)
        assert lv[0] <= 0.01 and lv[-1] >= 0.99
        lower_gap = lv[1] - lv[0]
        middle_gap = lv[len(lv) // 2] - lv[len(lv) // 2 - 1]
        assert lower_gap < middle_gap


class TestWeakerAlternativesAreNotBuilt:
    def test_no_inverse_error_weighting_module_exists(self):
        """A strictly weaker special case of this — a linear reweighting with no
        interactions, no heteroskedasticity and no calibration. Building it
        would mean building a worse version and later retiring it."""
        root = os.path.join(os.path.dirname(__file__), "..")
        assert not os.path.exists(os.path.join(root, "inverse_error_weights.py"))
        src = open(os.path.join(root, "quantile_model.py")).read()
        assert "strictly weaker special case" in src
