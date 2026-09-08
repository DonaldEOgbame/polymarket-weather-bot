"""
tests/test_quant_core.py - Unit test suite for low-latency quantitative core.
"""

import pytest
import numpy as np
from unittest.mock import patch

from quant.qmath import (
    norm_cdf, student_t_cdf_nu4, bucket_probabilities_batch,
    platt_scale, normalize_partition_lattice, calculate_kelly_fraction
)
from quant.book import OrderBookL2, DualOrderBookL2
from quant.sniper import MetarSettlementSniper, SnipeSignal
from quant.qrisk import InstitutionalRiskManager, RiskCheckResult
from quant.engine import QuantSniperEngine


# ==========================================
# 1. QUANT MATH TESTS
# ==========================================
def test_student_t_nu4_symmetry():
    # Symmetry around mean: CDF(mean) == 0.5
    assert abs(student_t_cdf_nu4(70.0, loc=70.0, scale=2.5) - 0.5) < 1e-6
    # CDF(-inf) -> 0, CDF(+inf) -> 1
    assert abs(student_t_cdf_nu4(0.0, loc=70.0, scale=2.5)) < 1e-5
    assert abs(student_t_cdf_nu4(140.0, loc=70.0, scale=2.5) - 1.0) < 1e-5


def test_bucket_probabilities_batch_and_normalization():
    buckets = [(None, 68.0), (68.0, 70.0), (70.0, 72.0), (72.0, 74.0), (74.0, None)]
    raw_probs = bucket_probabilities_batch(ensemble_mean=71.0, ensemble_std=2.0, buckets=buckets)
    assert len(raw_probs) == 5
    assert all(p >= 0.0 for p in raw_probs)

    normed = normalize_partition_lattice(raw_probs)
    assert abs(np.sum(normed) - 1.0) < 1e-6
    # The middle bucket (70-72) should have the highest probability since mean is 71.0
    assert np.argmax(normed) == 2


def test_platt_scale_monotonicity():
    p1 = platt_scale(0.10)
    p2 = platt_scale(0.50)
    p3 = platt_scale(0.90)
    assert p1 < p2 < p3
    assert 0.05 <= p1 <= 0.98


def test_kelly_fraction_clamping():
    # Negative edge gives zero
    assert calculate_kelly_fraction(edge=-0.05, price=0.70) == 0.0
    # Price >= 1 gives zero
    assert calculate_kelly_fraction(edge=0.05, price=1.0) == 0.0
    # Capped at kelly_cap
    k = calculate_kelly_fraction(edge=0.20, price=0.50, kelly_cap=0.10)
    assert k == 0.10


# ==========================================
# 2. ORDER BOOK L2 TESTS
# ==========================================
def test_order_book_l2_basic():
    book = OrderBookL2("tok_test")
    book.update_snapshot(
        bids=[(0.70, 100), (0.68, 200)],
        asks=[(0.72, 50), (0.75, 100), (0.80, 500)]
    )
    assert book.best_bid == 0.70
    assert book.best_ask == 0.72
    assert book.mid == 0.71
    assert abs(book.spread - 0.02) < 1e-6
    assert abs(book.depth_at_or_below(0.75) - (0.72 * 50 + 0.75 * 100)) < 1e-6
    assert abs(book.depth_at_or_above(0.70) - 70.0) < 1e-6


def test_order_book_walk_buy_and_sell():
    book = OrderBookL2("tok_test")
    book.update_snapshot(
        bids=[(0.70, 100)],
        asks=[(0.72, 50), (0.75, 100)]  # $36 at 0.72, $75 at 0.75
    )
    # Buy $36: should exactly fill level 1 at 0.72
    vwap, shares, spent = book.walk_buy(36.0)
    assert abs(vwap - 0.72) < 1e-6
    assert abs(shares - 50.0) < 1e-6

    # Buy $50: should fill 50 shares @ 0.72 ($36) + $14 at 0.75 (18.67 shares)
    vwap2, shares2, spent2 = book.walk_buy(50.0)
    expected_shares = 50.0 + (14.0 / 0.75)
    expected_vwap = 50.0 / expected_shares
    assert abs(vwap2 - expected_vwap) < 1e-4
    assert abs(spent2 - 50.0) < 1e-6


# ==========================================
# 3. METAR SNIPER TESTS
# ==========================================
def test_sniper_identifies_locked_win():
    sniper = MetarSettlementSniper(max_snipe_price=0.96, min_edge=0.03)
    book = OrderBookL2("tok_no")
    book.update_snapshot(bids=[(0.90, 100)], asks=[(0.92, 200)])

    # Observed extreme 99F guarantees High bucket 90-91F is LOCKED_WIN on NO
    with patch("quant.sniper.resolved_extreme_f", return_value=99.0):
        sig = sniper.evaluate_market(
            market_id="m1",
            token_id_no="tok_no",
            city_key="Dallas",
            target_date="2026-09-08",
            is_high=True,
            bucket_low=90.0,
            bucket_high=91.0,
            book=book,
            stake_usd=10.0
        )
        assert sig is not None
        assert sig.side == "NO"
        assert sig.best_ask == 0.92
        assert sig.edge >= 0.07


def test_sniper_rejects_above_ceiling():
    sniper = MetarSettlementSniper(max_snipe_price=0.96, min_edge=0.03)
    book = OrderBookL2("tok_no")
    book.update_snapshot(bids=[(0.95, 100)], asks=[(0.98, 200)])  # 0.98 > 0.96

    with patch("quant.sniper.resolved_extreme_f", return_value=99.0):
        sig = sniper.evaluate_market(
            market_id="m1",
            token_id_no="tok_no",
            city_key="Dallas",
            target_date="2026-09-08",
            is_high=True,
            bucket_low=90.0,
            bucket_high=91.0,
            book=book,
            stake_usd=10.0
        )
        assert sig is None


# ==========================================
# 4. MARKET MAKER TESTS
# ==========================================
# 4. QUANT SNIPER ENGINE TESTS
# ==========================================
def test_quant_sniper_engine_setup_and_book():
    engine = QuantSniperEngine(paper_mode=True, max_snipe_price=0.96, min_edge=0.03)
    engine.update_book_from_market_data(token_id="tok_test", best_bid=0.60, best_ask=0.75, depth_usd=100.0)
    book = engine.get_or_create_book("tok_test")
    assert book.best_bid == 0.60
    assert book.best_ask == 0.75
    assert book.mid == 0.675


# ==========================================
# 6. RISK MANAGER TESTS
# ==========================================
def test_risk_manager_circuit_breakers():
    risk = InstitutionalRiskManager(max_daily_loss_pct=0.15, max_portfolio_drawdown_pct=0.20)

    # Normal pass
    res_pass = risk.evaluate_entry(
        strategy_name="SWING", city="Dallas", target_date="2026-09-09",
        requested_stake_usd=5.0, bankroll_usd=50.0, locked_cash_usd=0.0,
        high_water_mark_usd=50.0, daily_realized_pnl_usd=0.0, open_positions=[]
    )
    assert res_pass.allowed is True

    # Daily loss limit breach
    res_daily = risk.evaluate_entry(
        strategy_name="SWING", city="Dallas", target_date="2026-09-09",
        requested_stake_usd=5.0, bankroll_usd=50.0, locked_cash_usd=0.0,
        high_water_mark_usd=50.0, daily_realized_pnl_usd=-10.0, open_positions=[]
    )
    assert res_daily.allowed is False
    assert "Daily Loss Limit Breached" in res_daily.reason

    # High-water mark drawdown breach (down >20%)
    res_dd = risk.evaluate_entry(
        strategy_name="SWING", city="Dallas", target_date="2026-09-09",
        requested_stake_usd=5.0, bankroll_usd=35.0, locked_cash_usd=0.0,
        high_water_mark_usd=50.0, daily_realized_pnl_usd=0.0, open_positions=[]
    )
    assert res_dd.allowed is False
    assert "Drawdown Breaker Fired" in res_dd.reason


# ==========================================
# 7. NANOSECOND SNIPER CORE TESTS
# ==========================================
def test_nanosecond_sniper_hot_path():
    from quant.fast_sniper import NanosecondSniperCore, PrebuiltSnipeOrder

    core = NanosecondSniperCore()
    order = PrebuiltSnipeOrder(
        token_id="tok_hot",
        price=0.92,
        size=10.0,
        raw_payload_bytes=b'{"mock": "wire_bytes"}',
        target_date="2026-09-08",
        city="Dallas"
    )
    core.register_market(
        icao="KDAL",
        bucket_id="b1",
        is_high=True,
        bucket_low=90.0,
        bucket_high=91.0,
        prebuilt_order=order
    )

    # 90.0F is below threshold (91.5) -> No trigger
    assert core.evaluate_observation_hot_path("KDAL", 90.0) == []

    # 92.0F breaches threshold (91.5) -> Immediate trigger
    triggered = core.evaluate_observation_hot_path("KDAL", 92.0)
    assert len(triggered) == 1
    assert triggered[0].token_id == "tok_hot"
    assert triggered[0].raw_payload_bytes == b'{"mock": "wire_bytes"}'


def test_dual_order_book_synthetic_liquidity():
    from quant.book import DualOrderBookL2

    dual = DualOrderBookL2("tok_yes", "tok_no")
    # Book NO has direct asks at 0.70 ($70)
    # Book YES has resting bids at 0.35 ($35), which translate to synthetic NO asks at 0.65 ($35)!
    dual.update_snapshots(
        bids_yes=[(0.35, 100.0)],
        asks_yes=[(0.40, 50.0)],
        bids_no=[(0.60, 50.0)],
        asks_no=[(0.70, 100.0)]
    )

    # Consolidated NO asks should sort: synthetic ask 0.65 first, then direct ask 0.70
    asks = dual.consolidated_asks("NO")
    assert len(asks) == 2
    assert asks[0][0] == 0.65
    assert asks[0][2] == "SELL_COUNTERPARTY"
    assert asks[1][0] == 0.70
    assert asks[1][2] == "BUY_DIRECT"

    # Walk buy NO for $80: fills 100 shares at 0.65 ($65) and remainder from 0.70 ($15 = 21.43 shares)
    vwap, shares, spent, fills = dual.walk_buy_consolidated("NO", 80.0)
    assert spent == 80.0
    assert len(fills) == 2
    assert vwap < 0.70


def test_nanosecond_sniper_yes_triggers():
    from quant.fast_sniper import NanosecondSniperCore, PrebuiltSnipeOrder

    core = NanosecondSniperCore()
    order_yes_tail = PrebuiltSnipeOrder(
        token_id="tok_yes_tail",
        price=0.85,
        size=20.0,
        raw_payload_bytes=b'{"action": "BUY_YES_TAIL"}',
        target_date="2026-09-08",
        city="Houston",
        side="YES"
    )

    # Register open-ended tail: "98°F or higher" (is_high=True, bucket_high=None)
    core.register_yes_trigger(
        icao="KIAH",
        bucket_id="b_high_tail",
        is_high=True,
        bucket_low=98.0,
        bucket_high=None,
        prebuilt_order=order_yes_tail
    )

    # 97.5°F -> no trigger
    assert core.evaluate_observation_hot_path("KIAH", 97.5) == []

    # 98.2°F -> reaches bound -> instant LOCKED YES trigger
    triggered = core.evaluate_observation_hot_path("KIAH", 98.2)
    assert len(triggered) == 1
    assert triggered[0].token_id == "tok_yes_tail"
    assert triggered[0].side == "YES"


def test_fast_sniper_batch_vectorized():
    from quant.fast_sniper import NanosecondSniperCore, PrebuiltSnipeOrder

    core = NanosecondSniperCore()
    order1 = PrebuiltSnipeOrder("tok1", 0.90, 10.0, b'1', "2026-09-08", "Dallas", "NO")
    order2 = PrebuiltSnipeOrder("tok2", 0.85, 10.0, b'2', "2026-09-08", "Austin", "YES")

    core.register_no_trigger("KDAL", "b1", is_high=True, bucket_low=90.0, bucket_high=95.0, prebuilt_order=order1)
    core.register_yes_trigger("KAUS", "b2", is_high=True, bucket_low=100.0, bucket_high=None, prebuilt_order=order2)

    # Batch evaluation of multiple stations simultaneously
    station_readings = {
        "KDAL": 96.0,  # Breaches 95.5 -> triggers NO order1
        "KAUS": 101.0, # Breaches 100.0 -> triggers YES order2
        "KORD": 75.0   # No triggers registered
    }

    triggered = core.evaluate_batch_vectorized(station_readings)
    assert len(triggered) == 2
    ids = {o.token_id for o in triggered}
    assert ids == {"tok1", "tok2"}


def test_event_driven_sniper_flow():
    from unittest.mock import MagicMock, patch
    from quant.event_sniper import EventDrivenSniper
    from quant.fast_sniper import PrebuiltSnipeOrder

    mock_executor = MagicMock()
    mock_executor.execute_trade.return_value = {"status": "FILLED"}

    sniper = EventDrivenSniper(mock_executor, poll_interval_sec=0.1)
    
    # Setup station mock market
    sniper._active_markets_by_station["KDAL"] = [{
        "market_id": "mkt_dallas_100",
        "city": "Dallas",
        "station_icao": "KDAL",
        "date": "2026-09-08",
        "is_high": True,
        "bucket_low": 98.0,
        "bucket_high": 99.0,
        "tokens": {"NO": "tok_no_99"},
        "bucket_label": "98.0-99.0"
    }]
    
    order = PrebuiltSnipeOrder(
        token_id="tok_no_99",
        price=0.96,
        size=10.0,
        raw_payload_bytes=b"",
        target_date="2026-09-08",
        city="Dallas",
        side="NO"
    )
    sniper.core.register_no_trigger("KDAL", "mkt_dallas_100", is_high=True, bucket_low=98.0, bucket_high=99.0, prebuilt_order=order, pad=0.0)
    sniper._registered_orders["mkt_dallas_100"] = order
    sniper._market_by_token["tok_no_99"] = sniper._active_markets_by_station["KDAL"][0]

    # 1. Below threshold (97.0°F = ~36.1°C) -> No trade
    with patch("quant.event_sniper.get_book") as mock_book:
        mock_book.return_value = {
            "asks": [{"price": "0.75", "size": "100"}],
            "bids": [{"price": "0.70", "size": "100"}]
        }
        sniper._evaluate_metar_tick({"icaoId": "KDAL", "obsTime": "2026-09-08T15:00:00Z", "temp": 36.1})
        mock_executor.execute_trade.assert_not_called()

    # 2. Above threshold (101.0°F = ~38.3°C) -> Breaches 99.0°F -> Physical certainty snipe fires!
    with patch("quant.event_sniper.get_book") as mock_book, \
         patch("quant.event_sniper.get_portfolio_state") as mock_port:
        mock_book.return_value = {
            "asks": [{"price": "0.75", "size": "100"}],
            "bids": [{"price": "0.70", "size": "100"}]
        }
        mock_port.return_value = {"available_cash": 100.0, "locked_cash": 0.0, "total_equity": 100.0}
        
        sniper._evaluate_metar_tick({"icaoId": "KDAL", "obsTime": "2026-09-08T16:00:00Z", "temp": 38.33})
        assert mock_executor.execute_trade.called
        call_arg = mock_executor.execute_trade.call_args[0][0]
        assert call_arg["side"] == "NO"
        assert call_arg["price"] == 0.75
        assert call_arg["token_id"] == "tok_no_99"
        assert "tok_no_99" in sniper._sniped_buckets


def test_sniper_audit_telemetry_missed_and_filled():
    from unittest.mock import MagicMock, patch
    from quant.event_sniper import EventDrivenSniper
    from quant.fast_sniper import PrebuiltSnipeOrder
    from db import init_db, get_sniper_audit_records
    from quant.audit_missed import analyze_sniper_audit

    init_db()

    mock_executor = MagicMock()
    mock_executor.execute_trade.return_value = {"status": "FILLED"}

    sniper = EventDrivenSniper(mock_executor, poll_interval_sec=0.1, max_snipe_price=0.96)

    # 1. Test REPRICED_ABOVE_CEILING
    order1 = PrebuiltSnipeOrder(
        token_id="tok_repriced",
        price=0.96,
        size=10.0,
        raw_payload_bytes=b"",
        target_date="2026-09-08",
        city="Dallas",
        side="NO"
    )
    sniper._market_by_token["tok_repriced"] = {
        "market_id": "mkt_1", "city": "Dallas", "station_icao": "KDAL",
        "date": "2026-09-08", "bucket_label": "98-99", "is_high": True
    }
    with patch("quant.event_sniper.get_book") as mock_book:
        mock_book.return_value = {
            "asks": [{"price": "0.99", "size": "100"}],
            "bids": [{"price": "0.95", "size": "100"}]
        }
        sniper._attempt_snipe(order1, 100.5)

    recs = get_sniper_audit_records(limit=5, outcome="REPRICED_ABOVE_CEILING")
    assert len(recs) >= 1
    assert recs[0]["city"] == "Dallas"
    assert recs[0]["best_ask"] == 0.99

    # 2. Test ZERO_DEPTH
    order2 = PrebuiltSnipeOrder(
        token_id="tok_zero_depth",
        price=0.96,
        size=10.0,
        raw_payload_bytes=b"",
        target_date="2026-09-08",
        city="Austin",
        side="NO"
    )
    sniper._market_by_token["tok_zero_depth"] = {
        "market_id": "mkt_2", "city": "Austin", "station_icao": "KAUS",
        "date": "2026-09-08", "bucket_label": "95-96", "is_high": True
    }
    with patch("quant.event_sniper.get_book") as mock_book:
        mock_book.return_value = {
            "asks": [{"price": "0.90", "size": "0"}],
            "bids": []
        }
        sniper._attempt_snipe(order2, 97.0)

    recs_zd = get_sniper_audit_records(limit=5, outcome="ZERO_DEPTH")
    assert len(recs_zd) >= 1
    assert recs_zd[0]["city"] == "Austin"

    # 3. Test INSUFFICIENT_FUNDS
    order3 = PrebuiltSnipeOrder(
        token_id="tok_no_cash",
        price=0.96,
        size=10.0,
        raw_payload_bytes=b"",
        target_date="2026-09-08",
        city="Chicago",
        side="NO"
    )
    sniper._market_by_token["tok_no_cash"] = {
        "market_id": "mkt_3", "city": "Chicago", "station_icao": "KORD",
        "date": "2026-09-08", "bucket_label": "80-81", "is_high": True
    }
    with patch("quant.event_sniper.get_book") as mock_book, \
         patch("quant.event_sniper.get_portfolio_state") as mock_port:
        mock_book.return_value = {
            "asks": [{"price": "0.80", "size": "100"}],
            "bids": [{"price": "0.75", "size": "100"}]
        }
        mock_port.return_value = {"available_cash": 0.50}
        sniper._attempt_snipe(order3, 82.0)

    recs_funds = get_sniper_audit_records(limit=5, outcome="INSUFFICIENT_FUNDS")
    assert len(recs_funds) >= 1
    assert recs_funds[0]["city"] == "Chicago"

    # 4. Verify analyze_sniper_audit CLI runs without errors
    analyze_sniper_audit(limit=10)



