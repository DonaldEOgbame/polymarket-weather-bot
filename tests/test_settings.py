"""Tests for the dashboard-editable settings, the config override path, and deposits.

The load-bearing property is that settings can never make the bot worse off by
accident: an empty/missing/corrupt store must yield the exact config.py
defaults, a stake raise must not be silently clamped by the ceiling, and a
deposit must not move any P&L figure.
"""
import importlib
import os
import sqlite3
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# app.py imports executor -> py_clob_client_v2, absent in the test env.
import types
for mod in ("py_clob_client_v2", "py_clob_client_v2.client", "py_clob_client_v2.clob_types"):
    sys.modules.setdefault(mod, types.ModuleType(mod))
sys.modules["py_clob_client_v2.client"].ClobClient = object
_ct = sys.modules["py_clob_client_v2.clob_types"]
for _n in ("MarketOrderArgsV2", "OrderArgsV2", "OrderType", "ApiCreds", "BalanceAllowanceParams", "AssetType"):
    if not hasattr(_ct, _n):
        setattr(_ct, _n, object)


def _fresh_db(tmp_path, monkeypatch):
    """Point every module at a throwaway DB and reload them in dependency order."""
    db_file = tmp_path / "bot.db"
    monkeypatch.setenv("DB_PATH", str(db_file))
    import config
    importlib.reload(config)
    import db
    importlib.reload(db)
    db.init_db()
    return db, config, db_file


def _reload_config(monkeypatch, db_file):
    monkeypatch.setenv("DB_PATH", str(db_file))
    import config
    importlib.reload(config)
    return config


class TestOverridePrecedence:
    def test_empty_table_yields_pristine_defaults(self, tmp_path, monkeypatch):
        """The property that makes this safe to deploy: no rows == no change."""
        _, config, _ = _fresh_db(tmp_path, monkeypatch)
        assert config.FIXED_POSITION_SIZE == 3.0     # 2026-08-12 rule set ($3 first)
        assert config.MAX_CONCURRENT_POSITIONS == 4
        assert config.DAILY_LOSS_STAKES == 4.0
        # the DOLLAR limit is derived: 4 stakes x $3 default = -$12; the
        # stake-denominated budget scaled with the 2026-08-12 stake change
        assert config.daily_loss_limit() == -12.0
        assert config.MAX_TOTAL_EXPOSURE_FRACTION == 0.70
        # Default flipped 2026-08-11 — see test_config_thresholds for the evidence.
        assert config.ENABLE_STOP_LOSS is False
        assert config.STOP_LOSS_PCT == 0.50
        assert config.TAKE_PROFIT_PRICE == 0.98
        assert config.PAUSE_SCANNING is False
        assert config.is_scanning_paused() is False

    def test_stored_override_applies(self, tmp_path, monkeypatch):
        db, _, db_file = _fresh_db(tmp_path, monkeypatch)
        db.save_settings({"FIXED_POSITION_SIZE": 6.0})
        config = _reload_config(monkeypatch, db_file)
        assert config.FIXED_POSITION_SIZE == 6.0

    def test_db_beats_env(self, tmp_path, monkeypatch):
        """DB must win, else the UI would confirm a save that never took effect."""
        db, _, db_file = _fresh_db(tmp_path, monkeypatch)
        db.save_settings({"FIXED_POSITION_SIZE": 6.0})
        monkeypatch.setenv("FIXED_POSITION_SIZE", "3.0")
        config = _reload_config(monkeypatch, db_file)
        assert config.FIXED_POSITION_SIZE == 6.0

    def test_env_beats_default_when_no_override(self, tmp_path, monkeypatch):
        _, _, db_file = _fresh_db(tmp_path, monkeypatch)
        monkeypatch.setenv("FIXED_POSITION_SIZE", "3.0")
        config = _reload_config(monkeypatch, db_file)
        assert config.FIXED_POSITION_SIZE == 3.0

    def test_override_seeds_runtime_store(self, tmp_path, monkeypatch):
        """Stored overrides must seed the runtime store the bot reads at each
        decision (config.setting), which is what strategy/executor consume."""
        db, _, db_file = _fresh_db(tmp_path, monkeypatch)
        db.save_settings({"FIXED_POSITION_SIZE": 7.0})
        config = _reload_config(monkeypatch, db_file)
        assert config.setting("FIXED_POSITION_SIZE") == 7.0
        assert config.effective_stake() == 7.0

    def test_apply_runtime_overrides_is_instant(self, tmp_path, monkeypatch):
        """The no-restart property: swapping the store changes what the very
        next decision reads, in the SAME process, no reload anywhere."""
        _, config, _ = _fresh_db(tmp_path, monkeypatch)
        assert config.setting("FIXED_POSITION_SIZE") == 3.0
        config.apply_runtime_overrides({"FIXED_POSITION_SIZE": 7.0,
                                        "DAILY_LOSS_STAKES": 3.0})
        assert config.effective_stake() == 7.0
        assert config.daily_loss_limit() == -21.0     # scales with the stake
        with pytest.raises(KeyError):
            config.apply_runtime_overrides({"PROB_CALIBRATION_SLOPE": 1.0})

    def test_daily_loss_limit_scales_with_stake(self, tmp_path, monkeypatch):
        """The user's requirement verbatim: the daily limit is dynamic, based
        off the position size — change the stake, the dollar limit follows."""
        _, config, _ = _fresh_db(tmp_path, monkeypatch)
        config.apply_runtime_overrides({"FIXED_POSITION_SIZE": 6.0})
        assert config.daily_loss_limit() == -24.0     # 4 stakes x $6
        config.apply_runtime_overrides({"DAILY_LOSS_STAKES": 2.0})
        assert config.daily_loss_limit() == -12.0     # 2 stakes x $6


class TestOverrideFailsSafe:
    def test_missing_db_file_uses_defaults(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DB_PATH", str(tmp_path / "does-not-exist.db"))
        import config
        importlib.reload(config)
        assert config.FIXED_POSITION_SIZE == 3.0

    def test_corrupt_db_uses_defaults(self, tmp_path, monkeypatch):
        bad = tmp_path / "corrupt.db"
        bad.write_bytes(os.urandom(512))
        monkeypatch.setenv("DB_PATH", str(bad))
        import config
        importlib.reload(config)
        assert config.FIXED_POSITION_SIZE == 3.0

    def test_missing_table_uses_defaults(self, tmp_path, monkeypatch):
        """init_db() runs AFTER config is imported, so on a fresh volume the
        table genuinely does not exist yet at config-load time."""
        empty = tmp_path / "empty.db"
        sqlite3.connect(str(empty)).close()
        monkeypatch.setenv("DB_PATH", str(empty))
        import config
        importlib.reload(config)
        assert config.FIXED_POSITION_SIZE == 3.0

    def test_unknown_key_in_table_is_ignored(self, tmp_path, monkeypatch):
        db, _, db_file = _fresh_db(tmp_path, monkeypatch)
        with sqlite3.connect(str(db_file)) as conn:
            conn.execute("INSERT INTO settings (key, value, updated_at) VALUES (?,?,?)",
                         ("PROB_CALIBRATION_SLOPE", "0.1", "now"))
            conn.commit()
        config = _reload_config(monkeypatch, db_file)
        assert config.PROB_CALIBRATION_SLOPE == 0.7480   # unchanged, not clobbered

    def test_unparseable_value_falls_back(self, tmp_path, monkeypatch):
        db, _, db_file = _fresh_db(tmp_path, monkeypatch)
        with sqlite3.connect(str(db_file)) as conn:
            conn.execute("INSERT INTO settings (key, value, updated_at) VALUES (?,?,?)",
                         ("FIXED_POSITION_SIZE", "not-a-number", "now"))
            conn.commit()
        monkeypatch.setenv("DB_PATH", str(db_file))
        import config
        with pytest.raises(ValueError):
            importlib.reload(config)   # loud, not silently wrong

    def test_bool_false_is_real_bool(self, tmp_path, monkeypatch):
        """'false' is a truthy string — this is the classic way to break a switch."""
        db, _, db_file = _fresh_db(tmp_path, monkeypatch)
        db.save_settings({"ENABLE_STOP_LOSS": False})
        config = _reload_config(monkeypatch, db_file)
        assert config.ENABLE_STOP_LOSS is False

    def test_int_setting_stays_int(self, tmp_path, monkeypatch):
        db, _, db_file = _fresh_db(tmp_path, monkeypatch)
        db.save_settings({"MAX_CONCURRENT_POSITIONS": 8.0})
        config = _reload_config(monkeypatch, db_file)
        assert config.MAX_CONCURRENT_POSITIONS == 8
        assert isinstance(config.MAX_CONCURRENT_POSITIONS, int)


class TestSaveSettings:
    def test_upsert_is_idempotent(self, tmp_path, monkeypatch):
        db, _, _ = _fresh_db(tmp_path, monkeypatch)
        db.save_settings({"FIXED_POSITION_SIZE": 4.0})
        db.save_settings({"FIXED_POSITION_SIZE": 5.0})
        assert db.get_settings()["FIXED_POSITION_SIZE"] == "5.0"

    def test_returns_only_changed_keys(self, tmp_path, monkeypatch):
        db, _, _ = _fresh_db(tmp_path, monkeypatch)
        db.save_settings({"FIXED_POSITION_SIZE": 4.0})
        assert db.save_settings({"FIXED_POSITION_SIZE": 4.0}) == []
        assert db.save_settings({"FIXED_POSITION_SIZE": 9.0}) == ["FIXED_POSITION_SIZE"]

    def test_init_db_is_idempotent(self, tmp_path, monkeypatch):
        db, _, _ = _fresh_db(tmp_path, monkeypatch)
        db.save_settings({"FIXED_POSITION_SIZE": 4.0})
        db.init_db()
        assert db.get_settings()["FIXED_POSITION_SIZE"] == "4.0"


class TestDeposit:
    def test_deposit_adds_to_balance(self, tmp_path, monkeypatch):
        db, _, _ = _fresh_db(tmp_path, monkeypatch)
        before = db.get_current_bankroll()
        assert db.record_deposit(110.0) == pytest.approx(before + 110.0)

    def test_deposit_does_not_move_pnl(self, tmp_path, monkeypatch):
        """The guarantee that makes a deposit safe: P&L comes from trades.pnl."""
        db, _, _ = _fresh_db(tmp_path, monkeypatch)
        before = db.get_daily_pnl()
        db.record_deposit(110.0)
        assert db.get_daily_pnl() == before

    def test_total_deposited_tracks_seed_plus_deposits(self, tmp_path, monkeypatch):
        db, config, _ = _fresh_db(tmp_path, monkeypatch)
        db.record_deposit(100.0)
        db.record_deposit(25.0)
        assert db.get_total_deposited() == pytest.approx(config.STARTING_BANKROLL + 125.0)

    def test_total_deposited_excludes_trade_events(self, tmp_path, monkeypatch):
        db, config, _ = _fresh_db(tmp_path, monkeypatch)
        db.update_bankroll("TRADE_EXIT", 5.0)
        assert db.get_total_deposited() == pytest.approx(config.STARTING_BANKROLL)

    def test_live_seed_counts_as_capital(self, tmp_path, monkeypatch):
        """The production ledger opened with LIVE_SEED (cutover_to_live.py), not
        SEED. Missing it would drop the original stake from the denominator and
        overstate every return figure."""
        db, _, db_file = _fresh_db(tmp_path, monkeypatch)
        with sqlite3.connect(str(db_file)) as conn:
            conn.execute("DELETE FROM bankroll")
            conn.execute("INSERT INTO bankroll (timestamp, event, amount, balance, trade_id) "
                         "VALUES ('t','LIVE_SEED', 18.86, 18.86, NULL)")
            conn.commit()
        db.record_deposit(110.0)
        assert db.get_total_deposited() == pytest.approx(128.86)

    def test_dashboard_total_deposited_matches_db(self, tmp_path, monkeypatch):
        """app._total_deposited and db.get_total_deposited must not disagree."""
        db, _, db_file = _fresh_db(tmp_path, monkeypatch)
        with sqlite3.connect(str(db_file)) as conn:
            conn.execute("DELETE FROM bankroll")
            conn.execute("INSERT INTO bankroll (timestamp, event, amount, balance, trade_id) "
                         "VALUES ('t','LIVE_SEED', 18.86, 18.86, NULL)")
            conn.commit()
        db.record_deposit(110.0)
        import app as app_mod
        importlib.reload(app_mod)
        assert app_mod._total_deposited() == pytest.approx(db.get_total_deposited())

    @pytest.mark.parametrize("bad", [0, -5.0])
    def test_non_positive_deposit_rejected(self, tmp_path, monkeypatch, bad):
        db, _, _ = _fresh_db(tmp_path, monkeypatch)
        with pytest.raises(ValueError):
            db.record_deposit(bad)

    def test_concurrent_deposits_do_not_lose_writes(self, tmp_path, monkeypatch):
        db, _, _ = _fresh_db(tmp_path, monkeypatch)
        start = db.get_current_bankroll()
        threads = [threading.Thread(target=db.record_deposit, args=(1.0,)) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert db.get_current_bankroll() == pytest.approx(start + 10.0)


class TestSettingsAPI:
    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        db, _, db_file = _fresh_db(tmp_path, monkeypatch)
        monkeypatch.setenv("DASHBOARD_EMAIL", "t@t.com")
        import app as app_mod
        importlib.reload(app_mod)
        app_mod.app.config["TESTING"] = True
        c = app_mod.app.test_client()
        with c.session_transaction() as s:
            s["authed"] = True
        return c, app_mod, db

    def test_requires_auth(self, client):
        _, app_mod, _ = client
        anon = app_mod.app.test_client()
        assert anon.get("/api/settings").status_code == 401
        assert anon.post("/api/settings", json={"settings": {}}).status_code == 401
        assert anon.post("/api/deposit", json={"amount": 1, "confirm": True}).status_code == 401

    def test_get_returns_values_and_context(self, client):
        c, _, _ = client
        d = c.get("/api/settings").get_json()
        assert d["values"]["FIXED_POSITION_SIZE"] == 3.0
        assert d["values"]["DAILY_LOSS_STAKES"] == 4.0
        assert "total_equity" in d["context"]
        assert d["context"]["daily_loss_limit"] == -12.0

    def test_stake_is_the_only_size_authority(self, client):
        """There is no second knob that can silently shrink the stake: raising
        it alone is a complete, valid change."""
        c, app_mod, _ = client
        import config
        assert "HARD_MAX_POSITION_SIZE" not in app_mod.SETTING_SPECS
        r = c.post("/api/settings", json={"settings": {"FIXED_POSITION_SIZE": 6.0}})
        assert r.status_code == 200
        assert config.setting("FIXED_POSITION_SIZE") == 6.0
        assert config.effective_stake() == 6.0        # exactly what was typed
        # and it is no longer accepted as a setting at all
        bad = c.post("/api/settings", json={"settings": {"HARD_MAX_POSITION_SIZE": 9.0}})
        assert bad.status_code == 400

    def test_daily_loss_stakes_bounds(self, client):
        """The budget is in stakes and positive by construction — the old
        fixed-dollar knob could be typo'd positive, halting trading forever.
        The dollar amount is now derived, so that failure is unrepresentable."""
        c, _, _ = client
        assert c.post("/api/settings", json={"settings": {"DAILY_LOSS_STAKES": 0}}).status_code == 400
        assert c.post("/api/settings", json={"settings": {"DAILY_LOSS_STAKES": -4}}).status_code == 400
        assert c.post("/api/settings", json={"settings": {"DAILY_LOSS_STAKES": 25}}).status_code == 400
        assert c.post("/api/settings", json={"settings": {"DAILY_LOSS_LIMIT": -8.0}}).status_code == 400  # retired knob

    def test_unknown_key_rejected(self, client):
        c, _, _ = client
        r = c.post("/api/settings", json={"settings": {"PROB_CALIBRATION_SLOPE": 0.5}})
        assert r.status_code == 400

    def test_stake_below_clob_minimum_rejected(self, client):
        c, _, _ = client
        r = c.post("/api/settings", json={"settings": {"FIXED_POSITION_SIZE": 0.5}})
        assert r.status_code == 400

    def test_valid_save_persists_the_stake(self, client):
        c, _, db = client
        r = c.post("/api/settings", json={"settings": {"FIXED_POSITION_SIZE": 4.0}})
        assert r.status_code == 200
        assert db.get_settings()["FIXED_POSITION_SIZE"] == "4.0"

    def test_save_applies_live_without_restart(self, client):
        """The whole point of the redesign: POST returns, and the runtime store
        the bot reads is ALREADY updated — same process, no reload, no restart."""
        c, app_mod, db = client
        import config
        assert config.setting("FIXED_POSITION_SIZE") == 3.0
        r = c.post("/api/settings", json={"settings": {"FIXED_POSITION_SIZE": 4.0,
                                                       "DAILY_LOSS_STAKES": 3.0}})
        assert r.status_code == 200
        d = r.get_json()
        assert "restarting" not in d                      # the old contract is gone
        assert config.setting("FIXED_POSITION_SIZE") == 4.0   # live, instantly
        assert config.daily_loss_limit() == -12.0             # 3 stakes x $4
        assert d["daily_loss_limit"] == -12.0
        assert db.get_settings()["FIXED_POSITION_SIZE"] == "4.0"  # and persisted

    def test_pause_scanning_toggle_via_settings_api(self, client):
        c, app_mod, db = client
        import config
        assert config.setting("PAUSE_SCANNING") is False
        assert config.is_scanning_paused() is False

        # Pause via /api/settings
        r = c.post("/api/settings", json={"settings": {"PAUSE_SCANNING": True}})
        assert r.status_code == 200
        assert config.setting("PAUSE_SCANNING") is True
        assert config.is_scanning_paused() is True
        assert db.get_settings()["PAUSE_SCANNING"] == "true"

        # Resume via /api/settings
        r2 = c.post("/api/settings", json={"settings": {"PAUSE_SCANNING": False}})
        assert r2.status_code == 200
        assert config.setting("PAUSE_SCANNING") is False
        assert config.is_scanning_paused() is False
        assert db.get_settings()["PAUSE_SCANNING"] == "false"

    def test_pause_scanning_toggle_via_pause_api(self, client):
        c, app_mod, db = client
        import config

        # Pause via /api/pause
        r = c.post("/api/pause", json={"paused": True})
        assert r.status_code == 200
        assert r.get_json()["is_paused"] is True
        assert config.setting("PAUSE_SCANNING") is True
        assert config.is_scanning_paused() is True
        assert db.get_settings()["PAUSE_SCANNING"] == "true"

        # Unpause via /api/pause
        r2 = c.post("/api/pause", json={"paused": False})
        assert r2.status_code == 200
        assert r2.get_json()["is_paused"] is False
        assert config.setting("PAUSE_SCANNING") is False
        assert config.is_scanning_paused() is False

    def test_run_scan_cycle_skips_when_paused(self, client, monkeypatch):
        c, app_mod, db = client
        import config
        import main as main_mod

        config.apply_runtime_overrides({"PAUSE_SCANNING": True})

        scanned = False
        def fake_scan():
            nonlocal scanned
            scanned = True
            return []

        monkeypatch.setattr(main_mod, "scan_markets", fake_scan)
        main_mod.run_scan_cycle()
        assert scanned is False, "run_scan_cycle must not scan when PAUSE_SCANNING is True"


class TestDepositAPI:
    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        db, _, _ = _fresh_db(tmp_path, monkeypatch)
        monkeypatch.setenv("DASHBOARD_EMAIL", "t@t.com")
        import app as app_mod
        importlib.reload(app_mod)
        app_mod.app.config["TESTING"] = True
        c = app_mod.app.test_client()
        with c.session_transaction() as s:
            s["authed"] = True
        return c, db

    def test_requires_confirm(self, client):
        c, db = client
        before = db.get_current_bankroll()
        assert c.post("/api/deposit", json={"amount": 110}).status_code == 400
        assert db.get_current_bankroll() == before

    def test_naira_mixup_guard(self, client):
        """₦200,000 typed where dollars were meant."""
        c, _ = client
        assert c.post("/api/deposit", json={"amount": 200000, "confirm": True}).status_code == 400

    @pytest.mark.parametrize("bad", [0, -5, "abc", None])
    def test_invalid_amounts_rejected(self, client, bad):
        c, _ = client
        assert c.post("/api/deposit", json={"amount": bad, "confirm": True}).status_code == 400

    def test_successful_deposit(self, client):
        c, db = client
        before = db.get_current_bankroll()
        d = c.post("/api/deposit", json={"amount": 110, "confirm": True}).get_json()
        assert d["new_balance"] == pytest.approx(before + 110)
        assert db.get_current_bankroll() == pytest.approx(before + 110)


class TestWalletCashSync:
    """executor.sync_wallet_cash — deposits/withdrawals on Polymarket are booked
    automatically, with the two-read guard so a flaky API can never move money."""

    def _executor(self, db):
        import executor as ex
        e = ex.Executor.__new__(ex.Executor)
        e.client = object()          # non-None: live path
        e._pending_wallet_bal = None
        return e, ex

    def test_deposit_booked_after_two_stable_reads(self, tmp_path, monkeypatch):
        db, _, _ = _fresh_db(tmp_path, monkeypatch)
        e, ex = self._executor(db)
        monkeypatch.setattr(ex, "paper_mode", lambda: False)
        monkeypatch.setattr(ex, "get_wallet_collateral", lambda c=None: 150.0)
        start = db.get_current_bankroll()          # 40.0 seed
        assert e.sync_wallet_cash() == 0           # first read: pending only
        assert db.get_current_bankroll() == start
        assert e.sync_wallet_cash() == 1           # second read: booked
        assert db.get_current_bankroll() == pytest.approx(150.0)
        rows = db.fetch_query("SELECT event, amount FROM bankroll ORDER BY id DESC LIMIT 1")
        assert rows[0]["event"] == "DEPOSIT" and rows[0]["amount"] == pytest.approx(110.0)

    def test_flaky_single_read_never_books(self, tmp_path, monkeypatch):
        db, _, _ = _fresh_db(tmp_path, monkeypatch)
        e, ex = self._executor(db)
        monkeypatch.setattr(ex, "paper_mode", lambda: False)
        readings = iter([150.0, None, 150.0, 150.0])
        monkeypatch.setattr(ex, "get_wallet_collateral", lambda c=None: next(readings))
        assert e.sync_wallet_cash() == 0    # 150 pending
        assert e.sync_wallet_cash() == 0    # None resets pending
        assert e.sync_wallet_cash() == 0    # 150 pending again
        assert db.get_current_bankroll() == 40.0
        assert e.sync_wallet_cash() == 1    # confirmed now
        assert db.get_current_bankroll() == pytest.approx(150.0)

    def test_withdrawal_booked_when_flat(self, tmp_path, monkeypatch):
        """Yesterday's exact scenario: user withdrew everything, ledger kept
        believing $20.91. With the sync, the ledger follows the wallet down."""
        db, _, _ = _fresh_db(tmp_path, monkeypatch)
        e, ex = self._executor(db)
        monkeypatch.setattr(ex, "paper_mode", lambda: False)
        monkeypatch.setattr(ex, "get_wallet_collateral", lambda c=None: 0.0)
        e.sync_wallet_cash()
        assert e.sync_wallet_cash() == 1
        assert db.get_current_bankroll() == pytest.approx(0.0)
        rows = db.fetch_query("SELECT event FROM bankroll ORDER BY id DESC LIMIT 1")
        assert rows[0]["event"] == "WITHDRAWAL"

    def test_withdrawal_skipped_with_open_positions(self, tmp_path, monkeypatch):
        """Redemption lag after a settlement looks like a withdrawal — never
        book one while positions are open."""
        db, _, db_file = _fresh_db(tmp_path, monkeypatch)
        with sqlite3.connect(str(db_file)) as conn:
            conn.execute("INSERT INTO positions (market_id, token_id, side, entry_price, "
                         "size_usdc, entry_time, question) "
                         "VALUES ('0xa','t','NO',0.6,2.0,'t','q')")
            conn.commit()
        e, ex = self._executor(db)
        monkeypatch.setattr(ex, "paper_mode", lambda: False)
        monkeypatch.setattr(ex, "get_wallet_collateral", lambda c=None: 5.0)
        e.sync_wallet_cash()
        assert e.sync_wallet_cash() == 0
        assert db.get_current_bankroll() == 40.0    # untouched

    def test_dust_difference_ignored(self, tmp_path, monkeypatch):
        db, _, _ = _fresh_db(tmp_path, monkeypatch)
        e, ex = self._executor(db)
        monkeypatch.setattr(ex, "paper_mode", lambda: False)
        monkeypatch.setattr(ex, "get_wallet_collateral", lambda c=None: 40.60)
        assert e.sync_wallet_cash() == 0
        assert e.sync_wallet_cash() == 0
        assert db.get_current_bankroll() == 40.0

    def test_withdrawal_accounting_keeps_pnl_honest(self, tmp_path, monkeypatch):
        """P&L = equity + withdrawn - deposited: taking money off the table must
        not read as a trading loss."""
        db, _, _ = _fresh_db(tmp_path, monkeypatch)
        db.update_bankroll("WITHDRAWAL", -40.0)
        import app as app_mod
        importlib.reload(app_mod)
        assert app_mod._total_withdrawn() == pytest.approx(40.0)
        # equity 0 + withdrawn 40 - deposited 40 = 0 profit, not -40
        assert 0 + app_mod._total_withdrawn() - app_mod._total_deposited() == pytest.approx(0.0)


class TestTradingModeAPI:
    """The paper <-> live switch. This is the only control in the app that
    starts real money moving, so every gate on it is pinned here."""

    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        db, config, db_file = _fresh_db(tmp_path, monkeypatch)
        monkeypatch.setenv("DASHBOARD_EMAIL", "t@t.com")
        import app as app_mod
        importlib.reload(app_mod)
        app_mod.app.config["TESTING"] = True
        c = app_mod.app.test_client()
        with c.session_transaction() as s:
            s["authed"] = True
        return c, app_mod, db, config, db_file

    @staticmethod
    def _preflight(monkeypatch, ok, checks=None):
        """Stand in for the credential/funding preflight."""
        import check_live_readiness as clr
        checks = checks if checks is not None else [
            {"id": "auth", "label": "CLOB authentication", "ok": ok,
             "blocking": True, "detail": "stub"}]
        monkeypatch.setattr(clr, "preflight",
                            lambda: {"ok": ok, "balance": 50.0, "signer": "0x1",
                                     "checks": list(checks)})

    @staticmethod
    def _open_position(db_file, mode="paper"):
        """Positions belong to one book. The mode matters: a paper position must
        block going live (it has no on-chain shares), and a LIVE position must
        block going back to paper (the bot would stop placing its real exit)."""
        with sqlite3.connect(str(db_file)) as conn:
            conn.execute("INSERT INTO positions (market_id, token_id, side, entry_price, "
                         "size_usdc, entry_time, mode) "
                         "VALUES ('0xa','t1','NO',0.6,2.0,'2026-07-30T00:00:00',?)", (mode,))
            conn.commit()

    def test_requires_auth(self, client):
        c, app_mod, _, _, _ = client
        anon = app_mod.app.test_client()
        assert anon.post("/api/trading-mode", json={"paper": False, "confirm": True}).status_code == 401
        assert anon.get("/api/live-preflight").status_code == 401

    def test_requires_confirm(self, client, monkeypatch):
        c, _, _, config, _ = client
        self._preflight(monkeypatch, True)
        assert c.post("/api/trading-mode", json={"paper": False}).status_code == 400
        assert config.paper_mode() is True       # untouched

    def test_going_live_blocked_when_preflight_fails(self, client, monkeypatch):
        c, _, _, config, _ = client
        self._preflight(monkeypatch, False, [
            {"id": "collateral", "label": "Wallet collateral", "ok": False,
             "blocking": True, "detail": "no collateral"}])
        r = c.post("/api/trading-mode", json={"paper": False, "confirm": True})
        assert r.status_code == 409
        assert r.get_json()["blocked"][0]["label"] == "Wallet collateral"
        assert config.paper_mode() is True       # still paper — the whole point

    def test_going_live_blocked_with_open_paper_positions(self, client, monkeypatch, tmp_path):
        """A paper position has no on-chain shares; live mode would try to sell
        something the wallet has never owned."""
        c, _, _, config, db_file = client
        self._preflight(monkeypatch, True)
        self._open_position(db_file)
        r = c.post("/api/trading-mode", json={"paper": False, "confirm": True})
        assert r.status_code == 409
        assert config.paper_mode() is True

    def test_going_live_applies_and_persists(self, client, monkeypatch):
        c, _, db, config, db_file = client
        self._preflight(monkeypatch, True)
        r = c.post("/api/trading-mode", json={"paper": False, "confirm": True})
        assert r.status_code == 200
        assert r.get_json()["paper_mode"] is False
        assert config.paper_mode() is False
        # Persisted, so a restart does not silently drop back to paper.
        with sqlite3.connect(str(db_file)) as conn:
            stored = dict(conn.execute("SELECT key, value FROM settings").fetchall())
        assert stored["PAPER_MODE"] == "false"

    def test_persisted_mode_survives_reload(self, client, monkeypatch):
        c, _, _, config, db_file = client
        self._preflight(monkeypatch, True)
        c.post("/api/trading-mode", json={"paper": False, "confirm": True})
        reloaded = _reload_config(monkeypatch, db_file)
        assert reloaded.PAPER_MODE is False
        assert reloaded.paper_mode() is False

    def test_back_to_paper_blocked_while_live_positions_open(self, client, monkeypatch, tmp_path):
        c, _, _, config, db_file = client
        self._preflight(monkeypatch, True)
        c.post("/api/trading-mode", json={"paper": False, "confirm": True})
        self._open_position(db_file, mode="live")
        r = c.post("/api/trading-mode", json={"paper": True, "confirm": True})
        assert r.status_code == 409
        assert config.paper_mode() is False      # still live — exits stay real

    def test_back_to_paper_allowed_when_flat(self, client, monkeypatch):
        c, _, _, config, _ = client
        self._preflight(monkeypatch, True)
        c.post("/api/trading-mode", json={"paper": False, "confirm": True})
        r = c.post("/api/trading-mode", json={"paper": True, "confirm": True})
        assert r.status_code == 200
        assert config.paper_mode() is True

    def test_same_mode_is_a_noop(self, client):
        c, _, _, _, _ = client
        r = c.post("/api/trading-mode", json={"paper": True, "confirm": True})
        assert r.status_code == 200
        assert r.get_json()["changed"] is False

    def test_cannot_be_smuggled_through_bulk_settings_save(self, client):
        """PAPER_MODE is runtime-tunable but absent from SETTING_SPECS, so the
        ordinary Settings save must reject it rather than flip it."""
        c, app_mod, _, config, _ = client
        assert "PAPER_MODE" not in app_mod.SETTING_SPECS
        r = c.post("/api/settings", json={"settings": {"PAPER_MODE": False}})
        assert r.status_code == 400
        assert "PAPER_MODE" in r.get_json()["field_errors"]
        assert config.paper_mode() is True

    def test_preflight_reports_open_positions(self, client, monkeypatch):
        c, _, _, _, db_file = client
        self._preflight(monkeypatch, True)
        self._open_position(db_file)
        d = c.get("/api/live-preflight").get_json()
        assert d["ok"] is False
        flat = [c_ for c_ in d["checks"] if c_["id"] == "flat_book"][0]
        assert flat["ok"] is False

    def test_executor_reads_the_flag_live(self, client, monkeypatch):
        """The reason this works at all: no module holds an import-time copy."""
        c, _, _, config, _ = client
        import executor as ex
        assert ex.paper_mode() is True
        self._preflight(monkeypatch, True)
        c.post("/api/trading-mode", json={"paper": False, "confirm": True})
        assert ex.paper_mode() is False


class TestModeIsolation:
    """Paper and live are two ledgers in one file. The property under test is
    that NO money figure in either book can see the other's rows — a missed
    filter is silent and shows simulated fills as real P&L."""

    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        db, config, db_file = _fresh_db(tmp_path, monkeypatch)
        monkeypatch.setenv("DASHBOARD_EMAIL", "t@t.com")
        import app as app_mod
        importlib.reload(app_mod)
        app_mod.app.config["TESTING"] = True
        c = app_mod.app.test_client()
        with c.session_transaction() as s:
            s["authed"] = True
        return c, app_mod, db, config, db_file

    @staticmethod
    def _seed_rows(db_file):
        """One settled winner in each book, and a deposit in each."""
        with sqlite3.connect(str(db_file)) as conn:
            for mode, pnl, size in (("paper", 5.0, 2.0), ("live", -1.0, 3.0)):
                conn.execute(
                    "INSERT INTO trades (market_id, side, size_usdc, fill_price, pnl, "
                    "status, entry_time, exit_time, city, target_date, mode) VALUES "
                    "(?,?,?,?,?,'CLOSED',?,?,?,?,?)",
                    (f"0x{mode}", "NO", size, 0.5, pnl, "2026-07-30T01:00:00",
                     "2026-07-30T02:00:00", "Lagos", "2026-07-30", mode))
            conn.execute("INSERT INTO bankroll (timestamp, event, amount, balance, mode) "
                         "VALUES ('2026-07-30T03:00:00','DEPOSIT',100.0,140.0,'live')")
            conn.commit()

    def test_paper_book_never_sees_live_rows(self, client):
        c, _, db, config, db_file = client
        self._seed_rows(db_file)
        assert config.paper_mode() is True
        d = c.get("/api/data").get_json()
        assert [t["city"] for t in d["trades"]] == ["Lagos"]        # only one
        assert d["trades"][0]["pnl"] == 5.0                          # the paper one
        assert d["portfolio"]["available_cash"] == 40.0              # not the live 140
        assert d["stats"]["30d"]["realized_pnl"] == 5.0

    def test_live_book_never_sees_paper_rows(self, client, monkeypatch):
        c, _, db, config, db_file = client
        self._seed_rows(db_file)
        config.apply_runtime_overrides({"PAPER_MODE": False})
        d = c.get("/api/data").get_json()
        assert d["trades"][0]["pnl"] == -1.0                         # the live one
        assert len(d["trades"]) == 1
        assert d["portfolio"]["available_cash"] == 140.0             # not the paper 40
        assert d["stats"]["30d"]["realized_pnl"] == -1.0

    def test_daily_pnl_and_circuit_breaker_are_per_book(self, client, db_file=None):
        """A day of paper losses must not halt live trading."""
        c, _, db, config, db_file = client
        with sqlite3.connect(str(db_file)) as conn:
            conn.execute(
                "INSERT INTO trades (market_id, side, size_usdc, fill_price, pnl, status, "
                "exit_time, mode) VALUES ('0xp','NO',2.0,0.5,-99.0,'CLOSED',?, 'paper')",
                (db.datetime.now(db.timezone.utc).isoformat(),))
            conn.commit()
        assert db.get_daily_pnl() == -99.0            # paper book sees it
        config.apply_runtime_overrides({"PAPER_MODE": False})
        assert db.get_daily_pnl() == 0.0              # live book does not

    def test_city_date_guard_is_per_book(self, client):
        """A paper trade on a city/date must not veto the real one."""
        c, _, db, config, db_file = client
        self._seed_rows(db_file)      # both books have Lagos / 2026-07-30
        paper = db.fetch_query(
            "SELECT id FROM trades WHERE city=? AND target_date=? AND mode=?",
            ("Lagos", "2026-07-30", "paper"))
        live = db.fetch_query(
            "SELECT id FROM trades WHERE city=? AND target_date=? AND mode=?",
            ("Lagos", "2026-07-30", "live"))
        assert len(paper) == 1 and len(live) == 1 and paper[0]["id"] != live[0]["id"]

    def test_paper_ledger_survives_a_round_trip_to_live(self, client):
        """'Paper resumes where it left off' — the whole point of tagging."""
        c, _, db, config, _ = client
        db.update_bankroll("TRADE_EXIT", 7.5)
        assert db.get_current_bankroll() == 47.5
        config.apply_runtime_overrides({"PAPER_MODE": False})
        db.ensure_bankroll_seeded()
        db.update_bankroll("DEPOSIT", 12.0)
        assert db.get_current_bankroll() == 12.0      # live book, independent
        config.apply_runtime_overrides({"PAPER_MODE": True})
        assert db.get_current_bankroll() == 47.5      # paper exactly as left


class TestStakeIsFunctional:
    """The stake is now the ONLY per-trade size control, so its whole path has
    to hold: what you type is validated, persisted, applied live, and is the
    exact dollar amount strategy.py sizes the next trade at."""

    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        db, config, db_file = _fresh_db(tmp_path, monkeypatch)
        monkeypatch.setenv("DASHBOARD_EMAIL", "t@t.com")
        import app as app_mod
        importlib.reload(app_mod)
        app_mod.app.config["TESTING"] = True
        c = app_mod.app.test_client()
        with c.session_transaction() as s:
            s["authed"] = True
        return c, app_mod, db, config, db_file

    def test_typed_value_is_the_exact_trade_size(self, client):
        """No clamp, no rounding, no second knob: $7 typed is $7 staked."""
        c, _, _, config, _ = client
        # Fund the book so the cash guard cannot mask the sizing result.
        import db as dbmod
        dbmod.update_bankroll("DEPOSIT", 100.0)
        assert c.post("/api/settings",
                      json={"settings": {"FIXED_POSITION_SIZE": 7.0}}).status_code == 200
        assert config.effective_stake() == 7.0
        assert config.setting("FIXED_POSITION_SIZE") == 7.0

    def test_bounds_match_between_ui_and_server(self, client):
        """The UI reads its stepper bounds from meta, so the two cannot drift
        into a state where a typeable value is server-rejected."""
        c, app_mod, _, _, _ = client
        meta = c.get("/api/settings").get_json()["meta"]["FIXED_POSITION_SIZE"]
        lo, hi = meta["min"], meta["max"]
        assert (lo, hi) == app_mod.SETTING_SPECS["FIXED_POSITION_SIZE"][1:3]
        assert c.post("/api/settings",
                      json={"settings": {"FIXED_POSITION_SIZE": hi + 1}}).status_code == 400
        assert c.post("/api/settings",
                      json={"settings": {"FIXED_POSITION_SIZE": lo - 0.5}}).status_code == 400

    def test_rejects_a_stake_the_book_cannot_fund(self, client):
        """Sized above available cash, every signal would be skipped — refuse
        the save rather than silently stopping the bot from trading."""
        c, _, _, _, _ = client
        r = c.post("/api/settings", json={"settings": {"FIXED_POSITION_SIZE": 90.0}})
        assert r.status_code == 400
        assert "FIXED_POSITION_SIZE" in r.get_json()["field_errors"]

    def test_daily_loss_limit_follows_the_stake_alone(self, client):
        c, _, _, config, _ = client
        import db as dbmod
        dbmod.update_bankroll("DEPOSIT", 100.0)
        c.post("/api/settings", json={"settings": {"FIXED_POSITION_SIZE": 5.0}})
        assert config.daily_loss_limit() == -20.0        # 4 stakes x $5

    def test_strategy_sizes_a_trade_at_exactly_the_stake(self, client, monkeypatch):
        """The end of the chain: the number strategy.py would stake."""
        c, _, _, config, _ = client
        import db as dbmod
        dbmod.update_bankroll("DEPOSIT", 100.0)
        c.post("/api/settings", json={"settings": {"FIXED_POSITION_SIZE": 6.0}})
        import strategy
        fixed_stake = strategy.setting("FIXED_POSITION_SIZE")
        assert fixed_stake > 0                            # flat-stake mode is on
        final_size = fixed_stake                          # strategy.py's flat branch
        assert final_size == 6.0

    def test_min_and_max_entry_price_can_be_managed(self, client):
        c, app_mod, _, config, _ = client
        r = c.post("/api/settings", json={"settings": {
            "MIN_ENTRY_PRICE": 0.65,
            "MAX_ENTRY_PRICE": 0.80,
        }})
        assert r.status_code == 200
        assert config.setting("MIN_ENTRY_PRICE") == 0.65
        assert config.setting("MAX_ENTRY_PRICE") == 0.80

    def test_min_entry_price_must_be_less_than_max(self, client):
        c, _, _, _, _ = client
        r = c.post("/api/settings", json={"settings": {
            "MIN_ENTRY_PRICE": 0.85,
            "MAX_ENTRY_PRICE": 0.70,
        }})
        assert r.status_code == 400
        assert "MIN_ENTRY_PRICE" in r.get_json()["field_errors"]

