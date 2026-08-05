"""Guards on the settings that decide whether real money moves.

Two changes reached production without a single test noticing: the paper -> live
switch, and the 3x stake increase. Neither was invisible by accident. Every
existing test reads `config.FIXED_POSITION_SIZE` and `config.PAPER_MODE`, which
are the CODE DEFAULTS captured at import; production runs on the settings table,
which overrides both. A test suite that asserts on the defaults is asserting on
values production does not use, and will stay green through any change made in
the dashboard.

Three layers here, because no single one covers it:

1. MECHANISM (always runs). The settings table must beat the code default, and
   the accessors must read the live store rather than an import-time copy.
   Fails if anyone "simplifies" `paper_mode()` back to the constant.

2. PIN (runs wherever a real DB is reachable; skips in CI). The deployed values
   are written down in DEPLOYED_PIN below. Changing production without changing
   this file makes the test fail. That is the whole point — it converts a
   dashboard click into something that has to be committed.

3. DECLARED-vs-DEPLOYED (always runs). fly.toml is checked in and is the only
   deployed configuration CI can actually see, so a contradiction between it and
   the pin is caught here even when the DB is not reachable.
"""
import os
import re
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import config as C

# ---------------------------------------------------------------------------
# What production is running RIGHT NOW. Update deliberately, in a commit, with
# the date and reason — that record is the feature, not the values.
#
# 2026-07-18: PAPER_MODE -> false ($18.86 real bankroll).
# 2026-08-05: FIXED_POSITION_SIZE 2.0 -> 6.0 after the $120.83 deposit.
DEPLOYED_PIN = {
    "PAPER_MODE": "false",
    "FIXED_POSITION_SIZE": 6.0,
}
# ---------------------------------------------------------------------------


def _settings_db(tmp_path, values):
    """A DB shaped like production's, holding `values` in the settings table."""
    path = str(tmp_path / "bot.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
    conn.executemany("INSERT INTO settings (key, value) VALUES (?,?)",
                     [(k, str(v)) for k, v in values.items()])
    conn.commit()
    conn.close()
    return path


class TestTheSettingsTableBeatsTheCodeDefault:
    def test_override_wins_over_the_hardcoded_default(self, tmp_path, monkeypatch):
        monkeypatch.setattr(C, "DB_PATH", _settings_db(tmp_path, {
            "PAPER_MODE": "false", "FIXED_POSITION_SIZE": "6.0"}))
        overrides = C._load_overrides()
        assert overrides["PAPER_MODE"] == "false"
        assert overrides["FIXED_POSITION_SIZE"] == "6.0"

        monkeypatch.setattr(C, "_DB_OVERRIDES", overrides)
        # The default argument is the value the code ships with. It must lose.
        assert C._tunable("FIXED_POSITION_SIZE", "2.0") == "6.0"
        assert C._tunable("PAPER_MODE", "true") == "false"

    def test_override_wins_over_the_environment_too(self, tmp_path, monkeypatch):
        """fly.toml sets PAPER_MODE=true while the deployed bot trades live. The
        DB is intentionally the winner, so a stale fly.toml cannot quietly undo
        a dashboard switch on the next deploy — and a test that read the env
        would report the wrong mode."""
        monkeypatch.setenv("FIXED_POSITION_SIZE", "2.0")
        monkeypatch.setattr(C, "DB_PATH", _settings_db(tmp_path,
                                                       {"FIXED_POSITION_SIZE": "6.0"}))
        monkeypatch.setattr(C, "_DB_OVERRIDES", C._load_overrides())
        assert C._tunable("FIXED_POSITION_SIZE", "2.0") == "6.0"

    def test_only_managed_keys_can_be_overridden(self, tmp_path, monkeypatch):
        """A calibration constant must never become editable by writing a row.
        SIGMA_SCALE_HIGH is fitted, documented and version-tracked."""
        monkeypatch.setattr(C, "DB_PATH", _settings_db(tmp_path, {
            "SIGMA_SCALE_HIGH": "99.0", "FIXED_POSITION_SIZE": "6.0"}))
        overrides = C._load_overrides()
        assert "SIGMA_SCALE_HIGH" not in overrides
        assert "FIXED_POSITION_SIZE" in overrides


class TestAccessorsReadTheLiveStoreNotAnImportTimeCopy:
    def test_paper_mode_and_stake_follow_a_runtime_change(self):
        before_mode, before_stake = C.paper_mode(), C.effective_stake()
        try:
            C.apply_runtime_overrides({"PAPER_MODE": False, "FIXED_POSITION_SIZE": 6.0})
            assert C.paper_mode() is False
            assert C.effective_stake() == 6.0
        finally:
            C.apply_runtime_overrides({"PAPER_MODE": before_mode,
                                       "FIXED_POSITION_SIZE": before_stake})

    def test_the_module_constant_goes_stale_which_is_why_tests_must_not_read_it(self):
        """Not a bug — the documented design. Asserted so the reason the other
        tests in this file exist stays visible."""
        before = C.effective_stake()
        try:
            C.apply_runtime_overrides({"FIXED_POSITION_SIZE": 6.0})
            assert C.effective_stake() == 6.0
            assert C.FIXED_POSITION_SIZE != 6.0 or before == 6.0
        finally:
            C.apply_runtime_overrides({"FIXED_POSITION_SIZE": before})


class TestDeployedValuesMatchThePin:
    """Runs only against a DB explicitly identified as the deployed one.

    DEPLOYED_DB_PATH must be set. Falling back to config.DB_PATH was wrong and
    was caught immediately: on a dev box that resolves to the local scratch
    ./data/bot.db, which has its own stale settings (FIXED_POSITION_SIZE=4.0,
    no PAPER_MODE row) and is not production. A pin check that fails against
    the wrong database teaches people to ignore it.

    On the box, where DB_PATH really is the deployed DB:

        flyctl ssh console -a stormedgev2 \\
          -C "python -m pytest /app/tests/test_deployed_settings.py -k Pin"

    From a laptop, against a pulled snapshot:

        DEPLOYED_DB_PATH=/tmp/prod.db pytest tests/test_deployed_settings.py -k Pin

    check_live_readiness.py runs the same comparison on the box, so the paper ->
    live switch is gated on it too.
    """

    def _deployed(self):
        path = os.getenv("DEPLOYED_DB_PATH")
        if not path:
            pytest.skip("DEPLOYED_DB_PATH unset — the pin check needs the real DB, "
                        "and the local ./data/bot.db is not it (see class docstring)")
        if not os.path.exists(path):
            pytest.skip(f"no deployed DB at {path}")
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
            try:
                rows = dict(conn.execute("SELECT key, value FROM settings").fetchall())
            finally:
                conn.close()
        except sqlite3.Error as e:
            pytest.skip(f"settings table unreadable at {path}: {e}")
        if not rows:
            pytest.skip(f"settings table at {path} is empty (not a deployed DB)")
        return rows

    def test_paper_mode_matches_the_pin(self):
        rows = self._deployed()
        assert rows.get("PAPER_MODE", "").strip().lower() == DEPLOYED_PIN["PAPER_MODE"], (
            "Deployed PAPER_MODE differs from DEPLOYED_PIN. If the switch was "
            "deliberate, update the pin in this file and say why."
        )

    def test_stake_matches_the_pin(self):
        rows = self._deployed()
        assert float(rows.get("FIXED_POSITION_SIZE", 0)) == DEPLOYED_PIN["FIXED_POSITION_SIZE"], (
            "Deployed FIXED_POSITION_SIZE differs from DEPLOYED_PIN. The stake "
            "also scales the circuit breaker — see test_circuit_breaker_limit.py."
        )


class TestCheckedInConfigDoesNotContradictThePin:
    """The one deployed artifact CI can actually read is fly.toml."""

    def _fly_env(self):
        src = open(os.path.join(os.path.dirname(__file__), "..", "fly.toml")).read()
        return dict(re.findall(r"^\s*(\w+)\s*=\s*'([^']*)'\s*$", src, re.M))

    def test_fly_toml_paper_mode_is_flagged_when_it_contradicts_the_pin(self):
        """fly.toml currently declares PAPER_MODE='true' while the bot trades
        live, because the DB override wins. That is correct precedence and a
        genuinely misleading file: anyone reading fly.toml to answer "is this
        live?" gets the wrong answer, and a wipe of the settings table would
        silently drop a live bot back to paper.

        Asserted as an explicit, documented mismatch rather than a failure — the
        precedence is deliberate — so that the day fly.toml IS corrected, this
        test fails and forces the comment to be updated with it."""
        declared = self._fly_env().get("PAPER_MODE")
        pinned = DEPLOYED_PIN["PAPER_MODE"]
        assert declared == "true" and pinned == "false", (
            f"fly.toml PAPER_MODE={declared!r} vs pin {pinned!r} — the known "
            f"mismatch has changed. Update this test and the pin together."
        )

    def test_fly_toml_does_not_pin_a_contradicting_stake(self):
        """A FIXED_POSITION_SIZE in fly.toml would be dead config (the DB wins)
        that still reads as authoritative. There must not be one."""
        assert "FIXED_POSITION_SIZE" not in self._fly_env(), (
            "fly.toml declares a stake that the settings table overrides — "
            "delete it or it will be read as the truth by the next person."
        )
