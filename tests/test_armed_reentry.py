"""Armed re-entry (2026-08-08).

A market that passes EVERY entry gate except the MIN_ENTRY_PRICE floor is
"armed": while the arm lives, the narrow-bucket edge surcharge
(NARROW_BUCKET_EDGE_THRESHOLD, 0.12) is waived down to the base EDGE_THRESHOLD
(0.08) for that market. The design constraint these tests pin:

    The waiver may only ever drop the SURCHARGE, never the base threshold.
    "Price reached the floor" cannot distinguish market-confirmed-the-model
    from model-quietly-gave-up; the current-edge check and the
    confidence-revocation rail are what tell them apart.

Numbers used throughout (defaults: TAKER_FEE_RATE=0.05, EDGE_THRESHOLD=0.08,
NARROW_BUCKET_EDGE_THRESHOLD=0.12, MIN_ENTRY_PRICE=0.65), with bucket
probability pinned to 0.22 so p_side = 0.78:

    no_price 0.55 -> edge 0.218  (>= 0.12, fill below floor: ARMS)
    no_price 0.66 -> edge 0.109  (in [0.08, 0.12): enters ONLY with the waiver)
    no_price 0.72 -> edge 0.050  (< 0.08: refused even WITH the waiver)
"""
import importlib
import os
import sys
import tempfile
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


def _fresh_db(monkeypatch):
    tmpdir = tempfile.mkdtemp()
    db_path = os.path.join(tmpdir, "test.db")
    monkeypatch.setenv("DB_PATH", db_path)
    import config
    importlib.reload(config)
    import db
    importlib.reload(db)
    db.init_db()
    return db


# ---------------------------------------------------------------------------
# DB lifecycle
# ---------------------------------------------------------------------------

class TestArmLifecycle:
    def test_arm_then_read_back(self, monkeypatch):
        db = _fresh_db(monkeypatch)
        db.arm_signal("0xm", "Dallas", "2026-08-09", 98.0, 99.0,
                      fill=0.64, edge=0.132, p_side=0.78, threshold=0.12,
                      ttl_hours=24)
        arm = db.get_active_arm("0xm")
        assert arm is not None
        assert arm["status"] == "armed"
        assert arm["arm_edge"] == pytest.approx(0.132)

    def test_rearming_refreshes_last_seen_but_not_expiry(self, monkeypatch):
        """The TTL runs from the FIRST qualification. A market hovering below
        the floor for days must not stay armed forever through re-sighting."""
        db = _fresh_db(monkeypatch)
        db.arm_signal("0xm", "Dallas", "2026-08-09", 98.0, 99.0,
                      fill=0.64, edge=0.132, p_side=0.78, threshold=0.12,
                      ttl_hours=24)
        first = db.get_active_arm("0xm")
        db.arm_signal("0xm", "Dallas", "2026-08-09", 98.0, 99.0,
                      fill=0.60, edge=0.15, p_side=0.79, threshold=0.12,
                      ttl_hours=24)
        second = db.get_active_arm("0xm")
        assert second["id"] == first["id"]          # refreshed, not duplicated
        assert second["expires_at"] == first["expires_at"]
        assert second["last_seen"] >= first["last_seen"]

    def test_ttl_elapsed_arm_is_retired_on_read(self, monkeypatch):
        db = _fresh_db(monkeypatch)
        db.arm_signal("0xm", "Dallas", "2026-08-09", 98.0, 99.0,
                      fill=0.64, edge=0.132, p_side=0.78, threshold=0.12,
                      ttl_hours=-1)  # already expired
        assert db.get_active_arm("0xm") is None
        row = db.fetch_query("SELECT status, resolved_reason FROM armed_signals")[0]
        assert row["status"] == "expired"
        assert "ttl" in row["resolved_reason"]

    def test_resolve_consumes_the_arm(self, monkeypatch):
        db = _fresh_db(monkeypatch)
        db.arm_signal("0xm", "Dallas", "2026-08-09", 98.0, 99.0,
                      fill=0.64, edge=0.132, p_side=0.78, threshold=0.12,
                      ttl_hours=24)
        db.resolve_arm("0xm", "entered", "position opened @ 0.630")
        assert db.get_active_arm("0xm") is None
        row = db.fetch_query("SELECT status FROM armed_signals")[0]
        assert row["status"] == "entered"

    def test_resolve_without_an_arm_is_a_noop(self, monkeypatch):
        db = _fresh_db(monkeypatch)
        db.resolve_arm("0xnothing", "entered", "no arm exists")
        assert db.fetch_query("SELECT * FROM armed_signals") == []

    def test_purge_deletes_old_rows(self, monkeypatch):
        db = _fresh_db(monkeypatch)
        db.execute_query(
            "INSERT INTO armed_signals (market_id, armed_at, expires_at, status) "
            "VALUES ('0xold', '2026-01-01T00:00:00+00:00', "
            "'2026-01-02T00:00:00+00:00', 'entered')")
        db.arm_signal("0xnew", "Dallas", "2026-08-09", 98.0, 99.0,
                      fill=0.64, edge=0.132, p_side=0.78, threshold=0.12,
                      ttl_hours=24)
        db.purge_old_armed_signals(keep_days=30)
        rows = db.fetch_query("SELECT market_id FROM armed_signals")
        assert [r["market_id"] for r in rows] == ["0xnew"]


# ---------------------------------------------------------------------------
# Strategy behaviour
# ---------------------------------------------------------------------------

def _deep_book(price):
    return {
        "asks": [{"price": str(price), "size": "10000"}],
        "bids": [{"price": str(round(price - 0.02, 2)), "size": "10000"}],
    }


def _evaluate(monkeypatch, db, no_price, prob=0.22, market_id="0xdallas",
              ensemble_mean=103.1):
    """Run evaluate_opportunity on a narrow (1°F) bucket with a deep book at
    `no_price` and the bucket probability pinned to `prob`."""
    import config as C
    import scanner as SC
    import strategy as S

    opp = SimpleNamespace(
        city="Dallas", date="2026-08-09", is_high=True, hours_to_resolution=24.0,
        bucket_low=98.0, bucket_high=99.0,
        yes_price=round(1.0 - no_price, 2), no_price=no_price,
        token_id_yes="y", token_id_no="n", market_id=market_id, volume=50000.0,
        question="Will the highest temperature in Dallas be between 98-99F?",
    )
    engine_res = {
        "ensemble_mean": ensemble_mean, "ensemble_std": 1.2, "model_agreement": 1.0,
        "model_spread": 0.43, "model_count": 4, "raw_weighted_mean": ensemble_mean,
        "raw_models": {"ecmwf_ifs025": 103.2, "icon_global": 102.8,
                       "gfs_global": 103.8, "gem_global": 102.6},
    }
    book = _deep_book(no_price)
    monkeypatch.setattr(S, "get_bucket_probability", lambda er, lo, hi: prob)
    monkeypatch.setattr(S, "bucket_probability_stages",
                        lambda er, lo, hi: {"raw": prob, "post_platt": prob,
                                            "post_floor": prob})
    monkeypatch.setattr(S, "get_realtime_price",
                        lambda t: (no_price + 0.01, no_price - 0.01))
    monkeypatch.setattr(S, "get_orderbook_depth_usd", lambda t: (10000.0, 10000.0))
    monkeypatch.setattr(S, "estimate_fill", lambda tok, usd, cap=None: {
        "vwap": SC._walk_asks(book, usd, cap)[0],
        "filled_usd": SC._walk_asks(book, usd, cap)[1],
        "exhausted": SC._walk_asks(book, usd, cap)[2],
        "usable_depth_usd": SC._usable_ask_depth_usd(book, cap),
        "best_ask": no_price})
    monkeypatch.setattr(C, "_RUNTIME", dict(C._RUNTIME, FIXED_POSITION_SIZE=2.0))
    return S.evaluate_opportunity(
        opp, {"available_cash": 100.0, "total_equity": 100.0, "locked_cash": 0.0},
        engine_res=engine_res)


class TestArming:
    def test_floor_only_failure_arms_the_market(self, monkeypatch):
        db = _fresh_db(monkeypatch)
        res = _evaluate(monkeypatch, db, no_price=0.55)
        assert res is None                       # still refused this cycle
        arm = db.get_active_arm("0xdallas")
        assert arm is not None
        assert arm["arm_fill"] == pytest.approx(0.55)
        assert arm["arm_edge"] > 0.12            # passed the FULL surcharge

    def test_floor_plus_other_failures_says_why_it_did_not_arm(self, monkeypatch):
        """The reported skip reason is the FIRST failing gate, which can be the
        floor even when later gates also failed (Helsinki 71.2-72 on
        2026-08-08: floor + forecast_margin + forecast_direction, dashboard
        showed only the floor). No arm — and the reason must say so."""
        db = _fresh_db(monkeypatch)
        # mean 0.1°F above the padded bucket edge: margin gate fails, floor fails
        res = _evaluate(monkeypatch, db, no_price=0.55, ensemble_mean=99.6)
        assert res is None
        assert db.get_active_arm("0xdallas") is None
        logged = db.fetch_query(
            "SELECT signal_type FROM signals ORDER BY id DESC LIMIT 1"
        )[0]["signal_type"]
        assert "not armed" in logged and "forecast_margin" in logged

    def test_insufficient_edge_does_not_arm(self, monkeypatch):
        """Two failing gates (edge + floor) is not 'qualified but for the
        floor' — nothing may be armed."""
        db = _fresh_db(monkeypatch)
        res = _evaluate(monkeypatch, db, no_price=0.55, prob=0.40)  # p_side 0.60
        assert res is None
        assert db.get_active_arm("0xdallas") is None


class TestTheWaiver:
    def test_unarmed_market_in_the_surcharge_band_is_refused(self, monkeypatch):
        """edge 0.109 is above base 0.08 but below the narrow 0.12 — without an
        arm this must skip. The control for the test below."""
        db = _fresh_db(monkeypatch)
        assert _evaluate(monkeypatch, db, no_price=0.66) is None

    def test_armed_market_in_the_surcharge_band_enters(self, monkeypatch):
        db = _fresh_db(monkeypatch)
        db.arm_signal("0xdallas", "Dallas", "2026-08-09", 98.0, 99.0,
                      fill=0.55, edge=0.218, p_side=0.78, threshold=0.12,
                      ttl_hours=24)
        res = _evaluate(monkeypatch, db, no_price=0.66)
        assert res is not None and res["signal"] == "BUY_NO"

    def test_the_waiver_never_goes_below_the_base_threshold(self, monkeypatch):
        """edge 0.050 < base 0.08: an armed market with no CURRENT edge is
        refused. Entering on the remembered arm-time edge would be chasing."""
        db = _fresh_db(monkeypatch)
        db.arm_signal("0xdallas", "Dallas", "2026-08-09", 98.0, 99.0,
                      fill=0.55, edge=0.218, p_side=0.78, threshold=0.12,
                      ttl_hours=24)
        res = _evaluate(monkeypatch, db, no_price=0.72)
        assert res is None
        # the arm survives the refusal — it dies by TTL or revocation, not by
        # a cycle where the edge happened to be thin
        assert db.get_active_arm("0xdallas") is not None

    def test_confidence_drop_revokes_the_arm_permanently(self, monkeypatch):
        """p_side at/below MIN_MODEL_CONFIDENCE is the model retracting its
        view: the arm must be expired in the store, not merely unused."""
        db = _fresh_db(monkeypatch)
        db.arm_signal("0xdallas", "Dallas", "2026-08-09", 98.0, 99.0,
                      fill=0.55, edge=0.218, p_side=0.78, threshold=0.12,
                      ttl_hours=24)
        res = _evaluate(monkeypatch, db, no_price=0.66, prob=0.45)  # p_side 0.55
        assert res is None
        assert db.get_active_arm("0xdallas") is None
        row = db.fetch_query("SELECT status, resolved_reason FROM armed_signals")[0]
        assert row["status"] == "expired"
        assert "confidence" in row["resolved_reason"]

    def test_expired_arm_grants_no_waiver(self, monkeypatch):
        db = _fresh_db(monkeypatch)
        db.arm_signal("0xdallas", "Dallas", "2026-08-09", 98.0, 99.0,
                      fill=0.55, edge=0.218, p_side=0.78, threshold=0.12,
                      ttl_hours=-1)
        assert _evaluate(monkeypatch, db, no_price=0.66) is None

    def test_disabled_flag_disables_both_halves(self, monkeypatch):
        import strategy as S
        db = _fresh_db(monkeypatch)
        monkeypatch.setattr(S, "ARMED_REENTRY_ENABLED", False)
        db.arm_signal("0xdallas", "Dallas", "2026-08-09", 98.0, 99.0,
                      fill=0.55, edge=0.218, p_side=0.78, threshold=0.12,
                      ttl_hours=24)
        assert _evaluate(monkeypatch, db, no_price=0.66) is None   # no waiver
        assert _evaluate(monkeypatch, db, no_price=0.55,
                         market_id="0xother") is None
        assert db.get_active_arm("0xother") is None                # no arming
