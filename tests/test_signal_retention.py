"""Regression tests for the SKIP-signal retention carve-outs.

SKIP_SIGNAL_RETENTION_DAYS=3 was purging ~40,000 scored counterfactual rows a
day to save disk on a volume that has since been extended. That trail is the
largest calibration sample this system produces — every constant in config.py is
currently fitted on 27 settled trades — so two classes of SKIP row now survive
the window indefinitely.
"""
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import db as dbmod


def _iso(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


class _Harness:
    """Points db.py's module-level helpers at a throwaway database."""

    def __init__(self, tmp_path):
        self.path = tmp_path
        conn = sqlite3.connect(self.path)
        conn.execute("CREATE TABLE signals (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                     " timestamp TEXT, signal_type TEXT, edge REAL, raw_models TEXT)")
        conn.commit()
        conn.close()

    def add(self, signal_type, days_ago, edge=None, n=1, raw_models='{"m": 80.0}'):
        conn = sqlite3.connect(self.path)
        for _ in range(n):
            conn.execute(
                "INSERT INTO signals (timestamp, signal_type, edge, raw_models) VALUES (?,?,?,?)",
                (_iso(days_ago), signal_type, edge, raw_models))
        conn.commit()
        conn.close()

    def count(self, where="1=1"):
        conn = sqlite3.connect(self.path)
        n = conn.execute(f"SELECT COUNT(*) FROM signals WHERE {where}").fetchone()[0]
        conn.close()
        return n


def _harness(monkeypatch):
    h = _Harness(os.path.join(tempfile.mkdtemp(), "sig.db"))

    def execute_query(query, params=()):
        conn = sqlite3.connect(h.path)
        conn.execute(query, params)
        conn.commit()
        conn.close()

    monkeypatch.setattr(dbmod, "execute_query", execute_query)
    return h


class TestSkipRetention:
    def test_old_skips_are_still_purged_by_default(self, monkeypatch):
        h = _harness(monkeypatch)
        h.add("SKIP_LOW_EDGE", days_ago=10, edge=0.01, n=50)
        dbmod.purge_old_signals(14, 3)
        assert h.count() == 0

    def test_sample_survives_the_skip_window(self, monkeypatch):
        """A fixed ~5% cohort must outlive the 3-day window."""
        h = _harness(monkeypatch)
        h.add("SKIP_LOW_EDGE", days_ago=10, edge=0.01, n=200)
        dbmod.purge_old_signals(14, 3, sample_pct=5)
        remaining = h.count()
        assert 0 < remaining < 200

    def test_sample_is_stable_across_repeated_purges(self, monkeypatch):
        """Deterministic on id, not random: a fresh draw each run would erode
        the retained sample to nothing over successive purges."""
        h = _harness(monkeypatch)
        h.add("SKIP_LOW_EDGE", days_ago=10, edge=0.01, n=200)
        dbmod.purge_old_signals(14, 3, sample_pct=5)
        first = h.count()
        for _ in range(5):
            dbmod.purge_old_signals(14, 3, sample_pct=5)
        assert h.count() == first

    def test_near_miss_signals_are_kept(self, monkeypatch):
        """Skips that cleared the edge bar but failed another gate are the
        counterfactuals that say whether the gates earn their keep."""
        h = _harness(monkeypatch)
        h.add("SKIP_SPREAD", days_ago=10, edge=0.25, n=7)     # near-miss
        h.add("SKIP_LOW_EDGE", days_ago=10, edge=0.01, n=93)  # noise
        dbmod.purge_old_signals(14, 3, sample_pct=0, near_miss_edge=0.08)
        assert h.count() == 7
        assert h.count("edge >= 0.08") == 7

    def test_null_edge_rows_are_not_treated_as_near_misses(self, monkeypatch):
        h = _harness(monkeypatch)
        h.add("SKIP_NO_BOOK", days_ago=10, edge=None, n=20)
        dbmod.purge_old_signals(14, 3, sample_pct=0, near_miss_edge=0.08)
        assert h.count() == 0

    def test_recent_skips_are_untouched(self, monkeypatch):
        h = _harness(monkeypatch)
        h.add("SKIP_LOW_EDGE", days_ago=1, edge=0.01, n=40)
        dbmod.purge_old_signals(14, 3, sample_pct=5, near_miss_edge=0.08)
        assert h.count() == 40

    def test_carve_outs_survive_the_outer_window_too(self, monkeypatch):
        """Retained rows must outlive SIGNAL_RETENTION_DAYS as well. A sample
        that self-deletes after two weeks never accumulates enough to fit on,
        which is the entire problem this carve-out exists to solve."""
        h = _harness(monkeypatch)
        h.add("SKIP_SPREAD", days_ago=400, edge=0.99, n=10)
        dbmod.purge_old_signals(14, 3, sample_pct=0, near_miss_edge=0.08)
        assert h.count() == 10

    def test_non_carve_out_rows_still_purged_at_outer_window(self, monkeypatch):
        h = _harness(monkeypatch)
        h.add("SKIP_LOW_EDGE", days_ago=400, edge=0.01, n=200)
        dbmod.purge_old_signals(14, 3, sample_pct=0, near_miss_edge=0.08)
        assert h.count() == 0

    def test_retained_rows_shed_raw_models_json(self, monkeypatch):
        """The JSON is ~2.5KB of a ~2.7KB row and is only useful for debugging a
        recent scan. Dropping it is what makes indefinite retention affordable;
        the scalar features the calibration fits on are columns and survive."""
        h = _harness(monkeypatch)
        h.add("SKIP_SPREAD", days_ago=30, edge=0.99, n=5)
        dbmod.purge_old_signals(14, 3, sample_pct=0, near_miss_edge=0.08)
        assert h.count() == 5
        assert h.count("raw_models IS NULL") == 5
        assert h.count("edge IS NOT NULL") == 5, "scalar features must survive"

    def test_recent_rows_keep_their_raw_models(self, monkeypatch):
        h = _harness(monkeypatch)
        h.add("SKIP_SPREAD", days_ago=1, edge=0.99, n=5)
        dbmod.purge_old_signals(14, 3, sample_pct=0, near_miss_edge=0.08)
        assert h.count("raw_models IS NOT NULL") == 5

    def test_non_skip_signals_unaffected_by_carve_outs(self, monkeypatch):
        h = _harness(monkeypatch)
        h.add("ENTRY", days_ago=5, edge=0.2, n=10)
        dbmod.purge_old_signals(14, 3, sample_pct=5, near_miss_edge=0.08)
        assert h.count() == 10
