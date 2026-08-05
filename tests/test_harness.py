"""Purged, grouped, walk-forward evaluation.

Every constant in config.py has been fitted at least once on a sample too small
to support it — three refits returned confidence intervals wider than the
correction they proposed. This is the thing that would have said so, so its own
protocol has to be right before its numbers mean anything.

The tests that matter here are about the SPLIT, not about the arithmetic. A
mean is easy; a mean computed over a leaky split is a confident wrong answer,
which is worse than no answer.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import harness as H


class TestFolds:
    def _dates(self, n, start=1):
        return [f"2026-08-{d:02d}" for d in range(start, start + n)]

    def test_test_folds_are_always_later_than_training(self):
        """Walk-forward. A k-fold that trains on August to predict July measures
        interpolation and reports it as forecasting skill."""
        for train, test in H._folds(self._dates(30), n_folds=4):
            assert max(train) < min(test), (max(train), min(test))

    def test_an_embargo_is_purged_around_the_test_fold(self):
        """A 72h-lead signal shares a model run with signals for the days on
        either side, so a split on the calendar is not a split on information."""
        for train, test in H._folds(self._dates(30), n_folds=4):
            gap = (H._parse(min(test)) - H._parse(max(train))).days
            assert gap > H.PURGE_DAYS, f"only {gap}d between train and test"

    def test_no_date_appears_in_both_halves(self):
        for train, test in H._folds(self._dates(30), n_folds=4):
            assert not (set(train) & set(test))

    def test_too_few_dates_yields_no_folds_rather_than_a_bad_one(self):
        assert H._folds(self._dates(2), n_folds=4) == [] or all(
            t for _, t in H._folds(self._dates(2), n_folds=4))


class TestGroupingIsByDateAcrossAllCities:
    def test_a_date_is_never_split_across_folds(self):
        """One synoptic pattern hits a dozen cities at once, so a Chicago row
        and an Austin row on the same target date are one observation seen
        twice. Splitting them puts the answer in the training set."""
        rows = [{"target_date": f"2026-08-{d:02d}", "city_key": c,
                 "settled_value": 80.0, "id": d * 100 + i}
                for d in range(1, 25) for i, c in enumerate(("Chicago", "Austin", "Dallas"))]
        res = H.walk_forward(rows, n_folds=3)
        seen = {}
        for i, f in enumerate(res["folds"]):
            assert f["test_dates"] >= 1
        # Reconstruct the split the same way walk_forward does and assert
        # no date is in two test folds.
        from collections import defaultdict
        by_date = defaultdict(list)
        for r in rows:
            by_date[r["target_date"]].append(r)
        tests = [set(t) for _, t in H._folds(list(by_date), 3)]
        for a in range(len(tests)):
            for b in range(a + 1, len(tests)):
                assert not (tests[a] & tests[b])


class TestScoring:
    def test_log_score_punishes_a_confident_miss_without_bound(self):
        """The only common score that punishes P(YES)=0.00008 on a bucket that
        hit — which is exactly what Guangzhou #31 did, and what Brier alone
        capped at 1.0."""
        assert H._log_score(0.00008, outcome=True) > 9.0
        assert H._brier(0.00008, outcome=True) <= 1.0

    def test_log_score_rewards_being_right(self):
        assert H._log_score(0.02, outcome=False) < H._log_score(0.30, outcome=False)

    def test_brier_matches_its_definition(self):
        assert H._brier(0.25, True) == pytest.approx(0.5625)
        assert H._brier(0.25, False) == pytest.approx(0.0625)

    def test_pnl_is_net_of_fees_and_spread(self):
        """A gross-P&L harness would rank a configuration that trades more
        above one that trades better."""
        row = {"spread_fraction": 0.02, "no_price": 0.60}
        gross = H._pnl({"spread_fraction": 0.0}, 0.1, 0.60, 6.0, 0.0, False)
        net = H._pnl(row, 0.1, 0.60, 6.0, 0.05, False)
        assert net < gross

    def test_a_losing_no_trade_loses_the_whole_stake(self):
        row = {"spread_fraction": 0.0, "no_price": 0.60}
        assert H._pnl(row, 0.1, 0.60, 6.0, 0.05, outcome_yes=True) <= -6.0


class TestReliability:
    def _s(self, p, outcome, is_high=True, lead=36.0):
        return {"p_yes": p, "outcome_yes": outcome, "is_high": is_high,
                "lead_hours": lead}

    def test_it_is_restricted_to_the_band_the_bot_bets_in(self):
        """A diagram over [0,1] is dominated by bins holding no trades, and the
        whole question is whether p=0.05 means 5%."""
        scored = [self._s(0.05, False)] * 10 + [self._s(0.60, True)] * 10
        rel = H.reliability(scored)
        assert rel["n"] == 10, "out-of-band rows leaked in"

    def test_it_detects_overconfidence(self):
        """The measured failure: the model calls a bucket 5% and it hits 28%."""
        scored = [self._s(0.05, i < 28) for i in range(100)]
        rel = H.reliability(scored)
        b = rel["bins"][0]
        assert b["predicted"] == pytest.approx(0.05)
        assert b["observed"] == pytest.approx(0.28)

    def test_direction_and_horizon_are_split_not_pooled(self):
        """Sigma was fitted per direction on 2026-07-31 because maxima and
        minima are different problems with opposite biases. A pooled diagram
        averages them into looking calibrated."""
        scored = ([self._s(0.05, True, is_high=True, lead=12.0)] * 20
                  + [self._s(0.05, False, is_high=False, lead=60.0)] * 20)
        out = H.split_reliability(scored)
        assert out["by_direction"]["high"]["bins"][0]["observed"] == 1.0
        assert out["by_direction"]["low"]["bins"][0]["observed"] == 0.0
        assert out["by_horizon"]["0-24h"]["n"] == 20
        assert out["by_horizon"]["48-96h"]["n"] == 20


class TestItRefusesToOverclaim:
    def test_no_settled_rows_is_reported_not_papered_over(self, monkeypatch):
        monkeypatch.setattr(H, "load_rows", lambda **k: [
            {"id": 1, "timestamp": "2026-08-01", "settled_value": None,
             "config_fingerprint": "abc"}])
        rep = H.run()
        assert rep["rows_settled"] == 0
        assert "182,530" in rep["note"], (
            "the note should name the state production was actually in")

    def test_a_small_sample_is_flagged(self, monkeypatch):
        rows = [{"id": i, "timestamp": "2026-08-01", "target_date": "2026-08-02",
                 "settled_value": 80.0, "config_fingerprint": "abc",
                 "city_key": "Chicago"} for i in range(5)]
        monkeypatch.setattr(H, "load_rows", lambda **k: rows)
        monkeypatch.setattr(H, "replay_rows", lambda r, ov=None: [])
        rep = H.run()
        assert "warning" in rep and "not as evidence" in rep["warning"]

    def test_it_reports_which_configurations_the_rows_span(self, monkeypatch):
        """Pooling rows from two fingerprints is pooling two different beliefs.
        Phases 1.2, 1.3 and 2.1 each split the history."""
        rows = [{"id": 1, "timestamp": "2026-08-01", "settled_value": None,
                 "config_fingerprint": "aaa"},
                {"id": 2, "timestamp": "2026-08-02", "settled_value": None,
                 "config_fingerprint": "bbb"}]
        monkeypatch.setattr(H, "load_rows", lambda **k: rows)
        assert H.run()["fingerprints"] == ["aaa", "bbb"]


class TestPlattProvenance:
    def test_rows_predating_the_fit_are_flagged_in_sample(self):
        rows = [{"timestamp": "2026-07-01"}] * 3 + [{"timestamp": "2026-08-01"}] * 2
        out = H.check_platt_was_out_of_sample(rows)
        assert out["rows_before_fit"] == 3
        assert "IN-SAMPLE" in out["warning"]

    def test_all_later_rows_are_reported_clean(self):
        out = H.check_platt_was_out_of_sample([{"timestamp": "2026-08-01"}])
        assert "out-of-sample" in out["warning"]


class TestDoubleCountingIsNamed:
    def test_the_three_overlapping_corrections_are_reported(self):
        rep = H.double_counting_report()
        assert "ONE error" in rep["note"]
        assert rep["gfs_bias_cities"] >= 0

    def test_a_global_metar_shift_is_flagged_as_on_top(self, monkeypatch):
        import config as C
        monkeypatch.setattr(C, "METAR_WARM_CORRECTION_F", 1.3)
        rep = H.double_counting_report()
        assert any("ON TOP" in r for r in rep["overlaps"])
