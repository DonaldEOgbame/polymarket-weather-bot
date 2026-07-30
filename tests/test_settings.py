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
for _n in ("MarketOrderArgsV2", "OrderType", "ApiCreds", "BalanceAllowanceParams", "AssetType"):
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
        assert config.FIXED_POSITION_SIZE == 2.0
        assert config.HARD_MAX_POSITION_SIZE == 2.0
        assert config.MAX_CONCURRENT_POSITIONS == 4
        assert config.DAILY_LOSS_STAKES == 4.0
        # the DOLLAR limit is derived: 4 stakes x $2 default = -$8, identical to
        # the old fixed default, so behavior is continuous across the redesign
        assert config.daily_loss_limit() == -8.0
        assert config.MAX_TOTAL_EXPOSURE_FRACTION == 0.70
        assert config.ENABLE_STOP_LOSS is True
        assert config.STOP_LOSS_PCT == 0.50
        assert config.TAKE_PROFIT_PRICE == 0.98

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
        db.save_settings({"FIXED_POSITION_SIZE": 7.0, "HARD_MAX_POSITION_SIZE": 7.0})
        config = _reload_config(monkeypatch, db_file)
        assert config.setting("FIXED_POSITION_SIZE") == 7.0
        assert config.effective_stake() == 7.0

    def test_apply_runtime_overrides_is_instant(self, tmp_path, monkeypatch):
        """The no-restart property: swapping the store changes what the very
        next decision reads, in the SAME process, no reload anywhere."""
        _, config, _ = _fresh_db(tmp_path, monkeypatch)
        assert config.setting("FIXED_POSITION_SIZE") == 2.0
        config.apply_runtime_overrides({"FIXED_POSITION_SIZE": 5.0,
                                        "HARD_MAX_POSITION_SIZE": 5.0,
                                        "DAILY_LOSS_STAKES": 3.0})
        assert config.effective_stake() == 5.0
        assert config.daily_loss_limit() == -15.0     # scales with the stake
        with pytest.raises(KeyError):
            config.apply_runtime_overrides({"PROB_CALIBRATION_SLOPE": 1.0})

    def test_daily_loss_limit_scales_with_stake(self, tmp_path, monkeypatch):
        """The user's requirement verbatim: the daily limit is dynamic, based
        off the position size — change the stake, the dollar limit follows."""
        _, config, _ = _fresh_db(tmp_path, monkeypatch)
        config.apply_runtime_overrides({"FIXED_POSITION_SIZE": 6.0,
                                        "HARD_MAX_POSITION_SIZE": 6.0})
        assert config.daily_loss_limit() == -24.0     # 4 stakes x $6
        config.apply_runtime_overrides({"DAILY_LOSS_STAKES": 2.0})
        assert config.daily_loss_limit() == -12.0     # 2 stakes x $6


class TestOverrideFailsSafe:
    def test_missing_db_file_uses_defaults(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DB_PATH", str(tmp_path / "does-not-exist.db"))
        import config
        importlib.reload(config)
        assert config.FIXED_POSITION_SIZE == 2.0

    def test_corrupt_db_uses_defaults(self, tmp_path, monkeypatch):
        bad = tmp_path / "corrupt.db"
        bad.write_bytes(os.urandom(512))
        monkeypatch.setenv("DB_PATH", str(bad))
        import config
        importlib.reload(config)
        assert config.FIXED_POSITION_SIZE == 2.0

    def test_missing_table_uses_defaults(self, tmp_path, monkeypatch):
        """init_db() runs AFTER config is imported, so on a fresh volume the
        table genuinely does not exist yet at config-load time."""
        empty = tmp_path / "empty.db"
        sqlite3.connect(str(empty)).close()
        monkeypatch.setenv("DB_PATH", str(empty))
        import config
        importlib.reload(config)
        assert config.FIXED_POSITION_SIZE == 2.0

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
        assert d["values"]["FIXED_POSITION_SIZE"] == 2.0
        assert d["values"]["DAILY_LOSS_STAKES"] == 4.0
        assert "total_equity" in d["context"]
        assert d["context"]["daily_loss_limit"] == -8.0

    def test_stake_above_ceiling_rejected(self, client):
        """The silent-no-op trap: strategy.py takes min() of the two."""
        c, _, _ = client
        r = c.post("/api/settings", json={"settings": {"FIXED_POSITION_SIZE": 6.0}})
        assert r.status_code == 400
        assert "HARD_MAX_POSITION_SIZE" in r.get_json()["field_errors"]

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
        r = c.post("/api/settings", json={"settings": {"FIXED_POSITION_SIZE": 0.5,
                                                      "HARD_MAX_POSITION_SIZE": 0.5}})
        assert r.status_code == 400

    def test_valid_save_persists_both_size_keys(self, client):
        c, _, db = client
        r = c.post("/api/settings", json={"settings": {"FIXED_POSITION_SIZE": 4.0,
                                                       "HARD_MAX_POSITION_SIZE": 4.0}})
        assert r.status_code == 200
        stored = db.get_settings()
        assert stored["FIXED_POSITION_SIZE"] == "4.0"
        assert stored["HARD_MAX_POSITION_SIZE"] == "4.0"

    def test_save_applies_live_without_restart(self, client):
        """The whole point of the redesign: POST returns, and the runtime store
        the bot reads is ALREADY updated — same process, no reload, no restart."""
        c, app_mod, db = client
        import config
        assert config.setting("FIXED_POSITION_SIZE") == 2.0
        r = c.post("/api/settings", json={"settings": {"FIXED_POSITION_SIZE": 4.0,
                                                       "HARD_MAX_POSITION_SIZE": 4.0,
                                                       "DAILY_LOSS_STAKES": 3.0}})
        assert r.status_code == 200
        d = r.get_json()
        assert "restarting" not in d                      # the old contract is gone
        assert config.setting("FIXED_POSITION_SIZE") == 4.0   # live, instantly
        assert config.daily_loss_limit() == -12.0             # 3 stakes x $4
        assert d["daily_loss_limit"] == -12.0
        assert db.get_settings()["FIXED_POSITION_SIZE"] == "4.0"  # and persisted

    def test_archive_view_blocks_mutations(self, client, monkeypatch, tmp_path):
        """Writing while pointed at an archive would hit a read-only connection;
        both mutating endpoints must refuse cleanly instead."""
        c, app_mod, _ = client
        fake_archive = tmp_path / "era_001_paper.db"
        sqlite3.connect(str(fake_archive)).close()
        monkeypatch.setattr(app_mod, "_selected_archive_path", lambda: str(fake_archive))
        assert c.post("/api/settings", json={"settings": {"FIXED_POSITION_SIZE": 4.0}}).status_code == 409
        assert c.post("/api/deposit", json={"amount": 10, "confirm": True}).status_code == 409


class TestEras:
    """Multiple funded runs: paper era, live era 1, then live era 2 after a full
    withdrawal and re-fund. The old design had one archive at a fixed path with
    a boolean toggle and could not express this."""

    def test_no_eras_initially(self, tmp_path, monkeypatch):
        db, _, _ = _fresh_db(tmp_path, monkeypatch)
        assert db.get_eras() == []
        assert db.get_current_era() is None

    def test_start_and_close_era(self, tmp_path, monkeypatch):
        db, _, _ = _fresh_db(tmp_path, monkeypatch)
        era_id = db.start_era("live-1", "live", 18.86)
        cur = db.get_current_era()
        assert cur["id"] == era_id and cur["label"] == "live-1"
        assert cur["ended_at"] is None          # open eras have no end
        db.close_era(era_id, 20.91, "/data/era_001_live-1.db")
        assert db.get_current_era() is None     # sealed, nothing running
        sealed = db.get_eras()[0]
        assert sealed["final_balance"] == 20.91
        assert sealed["archive_path"] == "/data/era_001_live-1.db"

    def test_multiple_eras_coexist(self, tmp_path, monkeypatch):
        db, _, _ = _fresh_db(tmp_path, monkeypatch)
        e1 = db.start_era("paper", "paper", 40.0)
        db.close_era(e1, 62.85, "/data/era_001_paper.db")
        e2 = db.start_era("live-1", "live", 18.86)
        db.close_era(e2, 20.91, "/data/era_002_live-1.db")
        e3 = db.start_era("live-2", "live", 130.0)
        eras = db.get_eras()
        assert [e["label"] for e in eras] == ["live-2", "live-1", "paper"]
        assert db.get_current_era()["id"] == e3   # only the newest is open

    def test_only_one_era_open_at_a_time(self, tmp_path, monkeypatch):
        db, _, _ = _fresh_db(tmp_path, monkeypatch)
        e1 = db.start_era("live-1", "live", 10.0)
        db.close_era(e1, 12.0, "/data/a.db")
        db.start_era("live-2", "live", 100.0)
        open_eras = [e for e in db.get_eras() if e["ended_at"] is None]
        assert len(open_eras) == 1


class TestEraArchiveSelection:
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
        return c, app_mod, db, tmp_path

    def test_lists_only_eras_with_existing_files(self, client):
        c, app_mod, db, tmp_path = client
        real = tmp_path / "era_001_paper.db"
        sqlite3.connect(str(real)).close()
        e1 = db.start_era("paper", "paper", 40.0)
        db.close_era(e1, 62.85, str(real))
        e2 = db.start_era("live-1", "live", 18.86)
        db.close_era(e2, 20.91, str(tmp_path / "deleted.db"))   # file never created
        d = c.get("/api/eras").get_json()
        labels = [e["label"] for e in d["archived"]]
        assert labels == ["paper"]      # the missing-file era is not offered

    def test_select_and_clear_era(self, client):
        """_selected_archive_path reads flask.session, so it is exercised through
        real requests rather than called bare (no request context outside one)."""
        c, app_mod, db, tmp_path = client
        real = tmp_path / "era_001_paper.db"
        sqlite3.connect(str(real)).close()
        e1 = db.start_era("paper", "paper", 40.0)
        db.close_era(e1, 62.85, str(real))

        assert c.post("/api/view-era", json={"era_id": e1}).get_json()["viewing_era"] == e1
        assert c.get("/api/eras").get_json()["viewing_era"] == e1
        # while pointed at an archive the dashboard is read-only
        assert c.post("/api/deposit", json={"amount": 5, "confirm": True}).status_code == 409

        assert c.post("/api/view-era", json={"era_id": None}).get_json()["viewing_era"] is None
        assert c.get("/api/eras").get_json()["viewing_era"] is None
        assert c.get("/api/settings").get_json()["archive_view"] is False

    def test_unknown_era_rejected(self, client):
        c, _, _, _ = client
        assert c.post("/api/view-era", json={"era_id": 999}).status_code == 404

    def test_stale_selection_falls_back_to_live(self, client):
        """A session pointing at a deleted archive must read the LIVE db, never
        crash and never silently show nothing."""
        c, app_mod, _, _ = client
        with c.session_transaction() as s:
            s["view_era"] = 12345
        assert app_mod._selected_archive_path() is None
        assert c.get("/api/data").status_code == 200


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


class TestCutoverEra:
    """db.cutover_era — the shared core behind the Settings button and the CLI."""

    def _populate(self, db, db_file):
        with sqlite3.connect(str(db_file)) as conn:
            conn.execute("INSERT INTO trades (market_id, side, size_usdc, fill_price, "
                         "model_prob, edge, pnl, status, city, target_date) "
                         "VALUES ('0xa','NO',2.0,0.6,0.2,0.2,1.0,'CLOSED','Tokyo','2026-07-01')")
            conn.execute("INSERT INTO signals (timestamp, market_id, city, signal_type) "
                         "VALUES ('2026-07-01T00:00:00','0xa','Tokyo','BUY_NO')")
            conn.commit()

    def test_cutover_archives_wipes_and_reseeds(self, tmp_path, monkeypatch):
        db, _, db_file = _fresh_db(tmp_path, monkeypatch)
        self._populate(db, db_file)
        summary = db.cutover_era("live-2", "live", 130.0)
        assert summary["archived_trades"] == 1
        assert os.path.exists(summary["archive_path"])
        # live DB: money wiped, ledger re-seeded, research kept
        assert db.fetch_query("SELECT COUNT(*) AS c FROM trades")[0]["c"] == 0
        assert db.get_current_bankroll() == 130.0
        assert db.fetch_query("SELECT COUNT(*) AS c FROM signals")[0]["c"] == 1
        # archive still holds the old trade
        arch = sqlite3.connect(summary["archive_path"])
        assert arch.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1
        # era rows: retro row sealed + new one open
        assert db.get_current_era()["label"] == "live-2"
        sealed = [e for e in db.get_eras() if e["ended_at"]]
        assert len(sealed) == 1 and sealed[0]["archive_path"] == summary["archive_path"]

    def test_cutover_refuses_with_open_position(self, tmp_path, monkeypatch):
        db, _, db_file = _fresh_db(tmp_path, monkeypatch)
        with sqlite3.connect(str(db_file)) as conn:
            conn.execute("INSERT INTO positions (market_id, token_id, side, entry_price, "
                         "size_usdc, entry_time, question) "
                         "VALUES ('0xa','t','NO',0.6,2.0,'2026-07-01T00:00:00','q')")
            conn.commit()
        with pytest.raises(RuntimeError):
            db.cutover_era("live-2", "live", 100.0)
        assert db.fetch_query("SELECT COUNT(*) AS c FROM positions")[0]["c"] == 1  # untouched

    def test_settings_survive_cutover(self, tmp_path, monkeypatch):
        db, _, db_file = _fresh_db(tmp_path, monkeypatch)
        db.save_settings({"FIXED_POSITION_SIZE": 5.0})
        self._populate(db, db_file)
        db.cutover_era("live-2", "live", 100.0)
        assert db.get_settings()["FIXED_POSITION_SIZE"] == "5.0"

    def test_second_cutover_coexists(self, tmp_path, monkeypatch):
        db, _, db_file = _fresh_db(tmp_path, monkeypatch)
        self._populate(db, db_file)
        db.cutover_era("live-2", "live", 100.0)
        db.record_deposit(10.0)
        s2 = db.cutover_era("live-3", "live", 200.0)
        assert db.get_current_era()["label"] == "live-3"
        assert len([e for e in db.get_eras() if e["archive_path"]]) == 2
        assert os.path.exists(s2["archive_path"])


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


class TestNewEraAPI:
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
        return c, app_mod, db, db_file

    def test_requires_confirm(self, client):
        c, _, _, _ = client
        assert c.post("/api/new-era", json={}).status_code == 400

    def test_cutover_via_api(self, client, monkeypatch):
        c, app_mod, db, db_file = client
        with sqlite3.connect(str(db_file)) as conn:
            conn.execute("INSERT INTO trades (market_id, side, size_usdc, fill_price, "
                         "model_prob, edge, pnl, status) "
                         "VALUES ('0xa','NO',2.0,0.6,0.2,0.2,1.0,'CLOSED')")
            conn.commit()
        import executor as ex
        monkeypatch.setattr(ex, "get_wallet_collateral", lambda c=None: 130.0)
        r = c.post("/api/new-era", json={"confirm": True, "label": "live-2"})
        assert r.status_code == 200
        d = r.get_json()
        assert d["new_label"] == "live-2" and d["seed"] == 130.0
        assert d["archived_trades"] == 1
        assert db.get_current_bankroll() == pytest.approx(130.0)
        assert db.get_current_era()["label"] == "live-2"

    def test_open_positions_block_cutover(self, client):
        c, _, db, db_file = client
        with sqlite3.connect(str(db_file)) as conn:
            conn.execute("INSERT INTO positions (market_id, token_id, side, entry_price, "
                         "size_usdc, entry_time, question) VALUES ('0xa','t','NO',0.6,2.0,'t','q')")
            conn.commit()
        r = c.post("/api/new-era", json={"confirm": True})
        assert r.status_code == 409
        assert db.get_current_era() is None     # nothing happened

    def test_unreadable_wallet_refuses_zero_seed(self, client, monkeypatch):
        """A $0 era is a wiped ledger the bot can never trade out of — in paper
        mode nothing ever books cash into it. Refuse instead of zeroing."""
        c, _, db, _ = client
        import executor as ex
        monkeypatch.setattr(ex, "get_wallet_collateral", lambda c=None: None)
        r = c.post("/api/new-era", json={"confirm": True})
        assert r.status_code == 409
        body = r.get_json()
        assert body["needs_seed"] is True
        assert db.get_current_era() is None          # nothing happened

    def test_explicit_seed_overrides_unreadable_wallet(self, client, monkeypatch):
        """The refusal is not a dead end: an explicit seed still opens the era."""
        c, _, db, _ = client
        import executor as ex
        monkeypatch.setattr(ex, "get_wallet_collateral", lambda c=None: None)
        r = c.post("/api/new-era", json={"confirm": True, "seed": 25.0})
        assert r.status_code == 200
        assert r.get_json()["seed"] == 25.0
        assert db.get_current_bankroll() == 25.0

    def test_explicit_zero_seed_still_refused(self, client, monkeypatch):
        """Even asked for directly, $0 is refused — it is never a useful era."""
        c, _, db, _ = client
        import executor as ex
        monkeypatch.setattr(ex, "get_wallet_collateral", lambda c=None: None)
        r = c.post("/api/new-era", json={"confirm": True, "seed": 0})
        assert r.status_code == 409
        assert db.get_current_era() is None


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
    def _open_position(db_file):
        with sqlite3.connect(str(db_file)) as conn:
            conn.execute("INSERT INTO positions (market_id, token_id, side, entry_price, "
                         "size_usdc, entry_time) VALUES ('0xa','t1','NO',0.6,2.0,'2026-07-30T00:00:00')")
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
        self._open_position(db_file)
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
