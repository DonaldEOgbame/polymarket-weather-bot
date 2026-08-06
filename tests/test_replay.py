"""Acceptance tests for the replay log.

The contract is narrow and worth stating: a replay must reconstruct the deployed
probability and every gate outcome from STORED COLUMNS ALONE, with no hardcoded
historical constants. If it cannot, the log is not a replay log — it is just
more telemetry, and the next calibration question needs another deploy.

The 2026-07-31 audit is the counterexample this exists to prevent: `signals`
stored post-correction model temperatures, so reconstructing what the models
actually said required knowing which corrections shipped on which date.
MODEL_BIAS_CORRECTIONS changed twice in one afternoon with no record.
"""
import json
import os
import sys
import tempfile
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


@pytest.fixture
def wired(monkeypatch):
    """A throwaway DB with the replay tables, wired into db.py."""
    path = os.path.join(tempfile.mkdtemp(), "replay.db")
    import config as C
    monkeypatch.setattr(C, "DB_PATH", path)
    import db as dbmod
    monkeypatch.setattr(dbmod, "DB_PATH", path)
    dbmod.init_db()
    return dbmod


class _Opp:
    def __init__(self, **kw):
        defaults = dict(
            market_id="0xabc", token_id_yes="ty", token_id_no="tn",
            city="Tokyo", date="2026-08-02", bucket_low=88.0, bucket_high=88.8,
            yes_price=0.30, no_price=0.70, volume=50000.0,
            hours_to_resolution=24.0, question="q", is_high=True,
        )
        defaults.update(kw)
        for k, v in defaults.items():
            setattr(self, k, v)


def _engine(is_high=True, city="Tokyo"):
    """A real engine result, built by the production code path."""
    import weather as W
    raw = {"ecmwf_ifs025": 90.0, "jma_gsm": 91.2, "icon_global": 89.4, "gem_global": 90.6}
    corr = W.applied_corrections(city, is_high, raw)
    temps = {m: v + corr[m] for m, v in raw.items()}
    return W._build_engine_result(temps, "AP", city, 24.0, is_high,
                                  raw_models=raw, corrections=corr)


def _evaluate(monkeypatch, dbmod, opp, engine):
    """Run evaluate_opportunity with the market-data calls stubbed."""
    import strategy as S
    monkeypatch.setattr(S, "get_live_spread_fraction", lambda t: 0.02)
    monkeypatch.setattr(S, "get_orderbook_depth_usd", lambda t: (500.0, 400.0))
    monkeypatch.setattr(S, "estimate_fill",
                        lambda tok, usd, cap=None: {"vwap": 0.70, "filled_usd": usd,
                                                    "exhausted": False,
                                                    "usable_depth_usd": 5000.0,
                                                    "best_ask": 0.70})
    monkeypatch.setattr(S, "execute_query", dbmod.execute_query)
    return S.evaluate_opportunity(
        opp, {"available_cash": 100.0, "total_equity": 100.0, "locked_cash": 0.0},
        engine_res=engine)


class TestReplayReproducesLiveOutput:
    def test_probability_and_sigma_match_to_1e9(self, wired, monkeypatch):
        opp, eng = _Opp(), _engine()
        _evaluate(monkeypatch, wired, opp, eng)

        import replay
        monkeypatch.setattr(replay, "fetch_query", wired.fetch_query)
        rows = replay.load_rows()
        assert len(rows) == 1, "one evaluated opportunity should log exactly one row"
        got = replay.replay_row(rows[0])
        stored = rows[0]

        for field in ("ensemble_mean", "raw_weighted_mean", "weighted_spread_sd",
                      "model_agreement", "sigma_final", "prob_raw",
                      "prob_post_platt", "prob_post_floor", "edge_post_fee",
                      "edge_threshold"):
            assert abs(got[field] - stored[field]) < 1e-9, (
                f"{field}: replay {got[field]!r} != logged {stored[field]!r}")

    def test_every_gate_outcome_matches(self, wired, monkeypatch):
        opp, eng = _Opp(), _engine()
        _evaluate(monkeypatch, wired, opp, eng)
        import replay
        monkeypatch.setattr(replay, "fetch_query", wired.fetch_query)
        rows = replay.load_rows()
        got = replay.replay_row(rows[0])

        logged = {g["gate"]: g for g in wired.fetch_query(
            "SELECT gate, observed, threshold, passed FROM replay_gates "
            "WHERE signal_id=?", (rows[0]["id"],))}
        assert logged, "gates must be persisted per signal"
        assert {g["gate"] for g in got["gates"]} == set(logged)
        for g in got["gates"]:
            lg = logged[g["gate"]]
            assert bool(lg["passed"]) == bool(g["passed"]), f"{g['gate']} pass/fail differs"
            if g["observed"] is not None and lg["observed"] is not None:
                assert abs(g["observed"] - lg["observed"]) < 1e-9, g["gate"]

    def test_replay_needs_no_historical_constants(self, wired, monkeypatch):
        """Replay must survive the live config changing under it.

        This is the actual regression: corrections changed twice on 2026-07-31
        and rows written before the change became unreadable without a lookup
        table of what shipped when. Stored raw + stored weights means the row
        replays identically regardless of what config.py says now."""
        opp, eng = _Opp(), _engine()
        _evaluate(monkeypatch, wired, opp, eng)
        import replay
        monkeypatch.setattr(replay, "fetch_query", wired.fetch_query)
        rows = replay.load_rows()
        baseline = replay.replay_row(rows[0])

        # Mutate the live corrections wildly; the row must be unaffected when
        # replayed under an explicit override, because raw values are raw.
        import config as C
        monkeypatch.setattr(C, "MODEL_BIAS_CORRECTIONS",
                            {(m, d): 99.0 for m in ("ecmwf_ifs025", "jma_gsm",
                                                    "icon_global", "gem_global")
                             for d in (True, False)})
        ov = replay.ConfigOverride(model_bias={
            "ecmwf_ifs025": (2.29, 0.29), "jma_gsm": (3.99, 1.55),
            "icon_global": (1.74, 0.03), "gem_global": (2.46, 1.32)})
        again = replay.replay_row(rows[0], ov)
        assert abs(again["ensemble_mean"] - baseline["ensemble_mean"]) < 1e-9


class TestReplayUnderAlternativeConfig:
    def test_alternative_sigma_changes_probability(self, wired, monkeypatch):
        opp, eng = _Opp(), _engine()
        _evaluate(monkeypatch, wired, opp, eng)
        import replay
        monkeypatch.setattr(replay, "fetch_query", wired.fetch_query)
        rows = replay.load_rows()
        base = replay.replay_row(rows[0])
        wide = replay.replay_row(rows[0], replay.ConfigOverride(sigma_scale_high=3.0))
        assert wide["sigma_final"] > base["sigma_final"]
        # A wider sigma spreads mass off a narrow bucket, so P(YES) must fall.
        assert wide["prob_raw"] < base["prob_raw"]

    def test_alternative_threshold_changes_the_gate_not_the_probability(self, wired, monkeypatch):
        opp, eng = _Opp(), _engine()
        _evaluate(monkeypatch, wired, opp, eng)
        import replay
        monkeypatch.setattr(replay, "fetch_query", wired.fetch_query)
        rows = replay.load_rows()
        base = replay.replay_row(rows[0])
        loose = replay.replay_row(
            rows[0], replay.ConfigOverride(narrow_bucket_edge_threshold=0.0,
                                           edge_threshold=0.0))
        assert abs(loose["prob_post_floor"] - base["prob_post_floor"]) < 1e-9
        assert loose["edge_threshold"] == 0.0
        gate = next(g for g in loose["gates"] if g["gate"] == "edge_threshold")
        assert gate["passed"]

    def test_alternative_bias_shifts_the_mean_by_the_weighted_delta(self, wired, monkeypatch):
        opp, eng = _Opp(), _engine()
        _evaluate(monkeypatch, wired, opp, eng)
        import replay
        monkeypatch.setattr(replay, "fetch_query", wired.fetch_query)
        rows = replay.load_rows()
        base = replay.replay_row(rows[0])
        import config as C
        shifted = replay.replay_row(rows[0], replay.ConfigOverride(model_bias={
            m: (C.model_bias_correction(m, True) + 1.0,
                C.model_bias_correction(m, False) + 1.0)
            for m in ("ecmwf_ifs025", "jma_gsm", "icon_global", "gem_global")}))
        # +1F on every member is +1F on the weighted mean, exactly.
        assert abs((shifted["ensemble_mean"] - base["ensemble_mean"]) - 1.0) < 1e-9
        # ...and cannot move the spread at all.
        assert abs(shifted["weighted_spread_sd"] - base["weighted_spread_sd"]) < 1e-9


class TestReplayLogContents:
    def test_raw_values_are_pre_correction(self, wired, monkeypatch):
        """The single most important column. If this stores post-correction
        values the whole log is as unreplayable as `signals` was."""
        opp, eng = _Opp(), _engine()
        _evaluate(monkeypatch, wired, opp, eng)
        row = wired.fetch_query("SELECT * FROM replay_signals")[0]
        raw = json.loads(row["raw_models_pre_correction"])
        corr = json.loads(row["corrections_applied"])
        assert raw["ecmwf_ifs025"] == 90.0, "raw must be the untouched API value"
        import config as C
        assert abs(corr["ecmwf_ifs025"] - C.model_bias_correction("ecmwf_ifs025", True)) < 1e-12
        # raw + correction must reconstruct what was actually used
        import weather as W
        weights = json.loads(row["model_weights"])
        tw = sum(weights.values())
        recon = sum((raw[m] + corr[m]) * weights[m] / tw for m in weights)
        assert abs(recon - row["raw_weighted_mean"]) < 1e-9

    def test_gate_rows_carry_observed_and_threshold(self, wired, monkeypatch):
        opp, eng = _Opp(), _engine()
        _evaluate(monkeypatch, wired, opp, eng)
        gates = wired.fetch_query("SELECT * FROM replay_gates")
        by = {g["gate"]: g for g in gates}
        # Every gate, not just the first failure. Counted against the live gate
        # list rather than a literal, so adding a gate does not silently turn
        # this into a test of a stale number — it was `== 8` until the
        # independent veto added two, and the fix then is to check the property,
        # not to bump the constant.
        import strategy as S
        expected = {g["gate"] for g in S._no_side_gates(
            opp, eng, 0.1, 0.08, 1.0, 0.5, 0.02)}
        assert set(by) == expected
        assert len(gates) == len(expected)
        assert by["max_entry_price"]["observed"] == 0.70
        assert by["max_entry_price"]["threshold"] == pytest.approx(0.85)

    def test_skips_are_logged_too(self, wired, monkeypatch):
        """A log that only records trades cannot answer counterfactuals, which
        is the entire purpose of the shadow run."""
        opp = _Opp(no_price=0.99)   # fails the price gate
        _evaluate(monkeypatch, wired, opp, _engine())
        rows = wired.fetch_query("SELECT decision, skip_reason FROM replay_signals")
        assert len(rows) == 1
        assert rows[0]["decision"] == "SKIP" and rows[0]["skip_reason"]

    def test_fingerprint_is_recorded_and_changes_with_config(self, wired, monkeypatch):
        opp, eng = _Opp(), _engine()
        _evaluate(monkeypatch, wired, opp, eng)
        fp1 = wired.fetch_query(
            "SELECT config_fingerprint FROM replay_signals")[0]["config_fingerprint"]
        assert fp1
        import config as C
        monkeypatch.setattr(C, "SIGMA_SCALE_HIGH", 9.9)
        assert C.config_fingerprint() != fp1

    def test_logging_failure_never_breaks_evaluation(self, wired, monkeypatch):
        """The replay log is a research artifact. It must not be able to stop a
        scan or block a trade."""
        import db as dbmod
        monkeypatch.setattr(dbmod, "log_replay_signal",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        opp, eng = _Opp(), _engine()
        # Must not raise.
        _evaluate(monkeypatch, wired, opp, eng)


class TestOutcomeBackfill:
    def test_outcome_derives_from_the_rows_own_bucket(self, wired, monkeypatch):
        """An untraded market's bucket is not the bucket any resolution scored,
        so the outcome has to be derived, not copied."""
        opp, eng = _Opp(bucket_low=88.0, bucket_high=88.8), _engine()
        _evaluate(monkeypatch, wired, opp, eng)
        wired.execute_query(
            "INSERT INTO resolutions (market_id, city, target_date, actual_value) "
            "VALUES (?,?,?,?)", ("0xother", "Tokyo", "2026-08-02", 88.5))
        wired.backfill_replay_outcomes()
        row = wired.fetch_query("SELECT settled_value, settled_outcome FROM replay_signals")[0]
        assert row["settled_value"] == 88.5
        assert row["settled_outcome"] == "YES"   # 88.5 is inside [87.5, 89.3]

    def test_outcome_no_when_actual_misses_the_bucket(self, wired, monkeypatch):
        opp, eng = _Opp(bucket_low=70.0, bucket_high=70.8), _engine()
        _evaluate(monkeypatch, wired, opp, eng)
        wired.execute_query(
            "INSERT INTO resolutions (market_id, city, target_date, actual_value) "
            "VALUES (?,?,?,?)", ("0xother", "Tokyo", "2026-08-02", 88.5))
        wired.backfill_replay_outcomes()
        row = wired.fetch_query("SELECT settled_outcome FROM replay_signals")[0]
        assert row["settled_outcome"] == "NO"


class TestBackfillIsWiredIntoProduction:
    """The backfill existed, was correct, was tested — and was never called.

    Every replay row written since the shadow run began therefore sat with
    settled_value NULL, which makes the entire log unscorable. Correctness tests
    could not catch that, because the function they exercised was fine. Only a
    wiring test can, so these assert the call site rather than the behaviour."""

    def test_check_resolutions_calls_the_backfill(self, wired, monkeypatch):
        import main as M
        called = []
        monkeypatch.setattr(M, "backfill_replay_outcomes", lambda *a, **k: called.append(1))
        monkeypatch.setattr(M, "fetch_query", lambda *a, **k: [])
        monkeypatch.setattr(M, "executor", types.SimpleNamespace(
            settle_unscored_trades=lambda: None))
        M.check_resolutions()
        assert called, "check_resolutions must settle the replay log"

    def test_backfill_runs_even_when_trade_scoring_raises(self, wired, monkeypatch):
        """The replay log is the shadow run's whole output and must not be held
        hostage by an exception raised while scoring one traded market."""
        import main as M
        called = []
        monkeypatch.setattr(M, "backfill_replay_outcomes", lambda *a, **k: called.append(1))
        monkeypatch.setattr(M, "fetch_query",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db boom")))
        monkeypatch.setattr(M, "executor", types.SimpleNamespace(
            settle_unscored_trades=lambda: None))
        M.check_resolutions()          # must not raise
        assert called, "a failure scoring trades must not skip the replay backfill"
