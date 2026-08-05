"""n-invariance of the entry gates.

Phase 2 roughly triples the ensemble: 4 members becomes 10-14, and per-city
extra_models makes the count vary from city to city within a single scan. Two
gates decide almost every trade — MIN_MODEL_AGREEMENT and MAX_MODEL_SPREAD_STD —
and both are computed FROM the ensemble. If either statistic moves merely
because the ensemble got bigger, then adding members retunes the gates as a side
effect, and the thresholds fitted against a 4-member ensemble quietly start
meaning something else.

That has already happened once here, in the other direction: model_spread was
max-min, which can only grow with member count, so MAX_MODEL_SPREAD was a de
facto cap on ensemble SIZE rather than on disagreement. It was changed to a
weighted standard deviation on 2026-07-31 and nothing has defended the property
since.

"n-invariance" needs to be exact to be testable, so it is pinned to a property
that admits no judgement:

    Splitting one member into two members that carry the same value and share
    its weight describes the same ensemble, and must produce identical
    statistics.

Any statistic computed over member COUNT fails this. Any statistic computed over
member WEIGHT passes it. That is precisely the distinction the gates depend on.

Adding a genuinely new member with new information SHOULD move the statistics —
that is signal, not drift — so the tests below assert bounded, weight-
proportional movement there rather than no movement at all.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import weather as W

# A fixed 4-member ensemble, matching the shipped US blend's weights.
BASE_W = {"ecmwf_ifs025": 0.40, "gfs_global": 0.30, "icon_global": 0.20, "gem_global": 0.10}
BASE_T = {"ecmwf_ifs025": 90.0, "gfs_global": 91.2, "icon_global": 89.4, "gem_global": 90.6}


def _engine(temps, weights, region="US", city="Chicago", is_high=True, lead=36.0):
    """Build an engine result against an arbitrary weight table."""
    saved = W.WEIGHTS.get(region)
    W.WEIGHTS[region] = weights
    try:
        return W._build_engine_result(temps, region, city, lead, is_high)
    finally:
        if saved is None:
            W.WEIGHTS.pop(region, None)
        else:
            W.WEIGHTS[region] = saved


class TestSplittingAMemberChangesNothing:
    """The exact property. A 5th member that duplicates an existing one, sharing
    its weight, is the same ensemble described differently."""

    def test_weighted_spread_is_unchanged(self):
        w5 = dict(BASE_W, ecmwf_ifs025=0.20, ecmwf_clone=0.20)
        t5 = dict(BASE_T, ecmwf_clone=BASE_T["ecmwf_ifs025"])
        assert W._weighted_pstdev(t5, w5) == pytest.approx(
            W._weighted_pstdev(BASE_T, BASE_W), abs=1e-12)

    def test_every_gated_statistic_is_unchanged(self):
        """Not just the spread — the agreement fraction and the ensemble mean
        are gated on too, and all three have to survive the split together."""
        four = _engine(BASE_T, BASE_W)
        w5 = dict(BASE_W, ecmwf_ifs025=0.20, ecmwf_clone=0.20)
        t5 = dict(BASE_T, ecmwf_clone=BASE_T["ecmwf_ifs025"])
        five = _engine(t5, w5)

        assert five["model_spread_std"] == pytest.approx(four["model_spread_std"], abs=1e-12)
        assert five["model_agreement"] == pytest.approx(four["model_agreement"], abs=1e-12)
        assert five["ensemble_mean"] == pytest.approx(four["ensemble_mean"], abs=1e-12)
        assert five["ensemble_std"] == pytest.approx(four["ensemble_std"], abs=1e-12)
        # The member count legitimately changes; the STATISTICS must not.
        assert five["model_count"] == 5 and four["model_count"] == 4

    def test_an_unweighted_spread_would_fail_this(self):
        """Proves the test has teeth: the statistic the gate USED to use does
        move under the split, so this file would have caught the max-min
        regression that shipped."""
        w5 = dict(BASE_W, ecmwf_ifs025=0.20, ecmwf_clone=0.20)
        t5 = dict(BASE_T, ecmwf_clone=BASE_T["ecmwf_ifs025"])
        assert W._pstdev(list(t5.values())) != pytest.approx(
            W._pstdev(list(BASE_T.values())), abs=1e-9)


class TestAgreementIsAWeightFractionNotACountFraction:
    def test_a_low_weight_dissenter_moves_agreement_by_its_weight_only(self):
        """A 5th member carrying 2% of the blend must be able to move the
        agreement gate by at most ~2 points — not by 1/5 of it. Under a count
        fraction, adding one dissenter to four members drops agreement from
        1.00 to 0.80 and fails the 0.75 gate on the next one."""
        four = _engine(BASE_T, BASE_W)
        w5 = dict(BASE_W, tiny=0.02)
        t5 = dict(BASE_T, tiny=BASE_T["ecmwf_ifs025"] + 25.0)   # wildly out of line
        five = _engine(t5, w5)

        drop = four["model_agreement"] - five["model_agreement"]
        assert drop <= 0.05, (
            f"a 2%-weight dissenter moved agreement by {drop:.3f} — the gate is "
            f"counting members, not weighting them"
        )

    def test_a_heavy_dissenter_does_move_the_gate(self):
        """The complement: the statistic must still respond to real
        disagreement, or n-invariance has been bought by making it inert."""
        four = _engine(BASE_T, BASE_W)
        w5 = dict(BASE_W, heavy=0.50)
        t5 = dict(BASE_T, heavy=BASE_T["ecmwf_ifs025"] + 25.0)
        five = _engine(t5, w5)
        assert five["model_agreement"] < four["model_agreement"] - 0.15


class TestSpreadDoesNotDriftWithEnsembleSize:
    def test_adding_members_from_the_same_distribution_does_not_inflate_spread(self):
        """The Phase 2 risk in one test. MAX_MODEL_SPREAD_STD already rejects
        78% of evaluations; if the statistic grows with n, going from 4 to 12
        members pushes that toward 100% for reasons that have nothing to do with
        forecast disagreement.

        Members are spread symmetrically about the mean, so the underlying
        dispersion is held constant while the count triples."""
        offsets = [-1.2, -0.6, 0.6, 1.2]
        base = {f"m{i}": 90.0 + d for i, d in enumerate(offsets)}
        bw = {k: 1.0 / len(base) for k in base}
        sd4 = W._weighted_pstdev(base, bw)

        big = dict(base)
        for i, d in enumerate(offsets):          # same values again, and again
            big[f"x{i}"] = 90.0 + d
            big[f"y{i}"] = 90.0 + d
        gw = {k: 1.0 / len(big) for k in big}
        sd12 = W._weighted_pstdev(big, gw)

        assert sd12 == pytest.approx(sd4, abs=1e-12)

    def test_the_unweighted_range_does_drift_and_is_not_gated_on(self):
        """model_spread_range is still reported for diagnostics. It grows with
        n, which is exactly why the gate reads model_spread_std instead — and
        why `model_spread` must remain an alias of the sd, not of the range."""
        eng4 = _engine(BASE_T, BASE_W)
        w5 = dict(BASE_W, outlier=0.05)
        t5 = dict(BASE_T, outlier=99.0)
        eng5 = _engine(t5, w5)

        assert eng5["model_spread_range"] > eng4["model_spread_range"]
        assert eng5["model_spread"] == eng5["model_spread_std"], (
            "the gated field must be the weighted sd, not the range"
        )


class TestTheGateReadsTheInvariantStatistic:
    def test_strategy_gates_on_model_spread_not_on_the_range(self):
        """Asserts the call site. The invariance proved above is worthless if
        the entry path reads a different field."""
        src = open(os.path.join(os.path.dirname(__file__), "..", "strategy.py")).read()
        assert 'spread = engine_res["model_spread"]' in src
        assert 'engine_res["model_spread_range"]' not in src

    def test_min_model_count_is_a_floor_not_a_target(self):
        """MIN_MODEL_COUNT rejects thin ensembles. It must not also be an upper
        bound — Phase 2 pushes member counts well past it, and a `== ` or a cap
        anywhere in that comparison would start dropping cities as the ensemble
        grows."""
        big_w = {f"m{i}": 1.0 / 12 for i in range(12)}
        big_t = {f"m{i}": 90.0 + (i % 3) * 0.4 for i in range(12)}
        eng = _engine(big_t, big_w)
        assert eng is not None, "a 12-member ensemble was rejected"
        assert eng["model_count"] == 12
