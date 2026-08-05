"""Acceptance tests for the off-box backup.

The contract: a file that this module calls a backup must be restorable and must
not have lost rows. Everything else here follows from that one sentence.

The failure this defends against is not "the backup did not run" — that is loud.
It is "the backup ran, wrote a file, rotated the previous one away, and the file
is a faithful copy of a wiped database". Every check below exists because
something in that sentence has to be false.
"""
import gzip
import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """A throwaway DB plus a throwaway backup directory, wired into both modules."""
    path = str(tmp_path / "bot.db")
    import config as C
    monkeypatch.setattr(C, "DB_PATH", path)
    import db as dbmod
    monkeypatch.setattr(dbmod, "DB_PATH", path)
    dbmod.init_db()

    import backup as B
    bdir = str(tmp_path / "backups")
    os.makedirs(bdir, exist_ok=True)
    monkeypatch.setattr(B, "BACKUP_DIR", bdir)
    monkeypatch.setattr(B, "BACKUP_PUT_URL_TEMPLATE", "")
    return dbmod, B


def _seed_trades(dbmod, n=3):
    for i in range(n):
        dbmod.execute_query(
            "INSERT INTO trades (market_id, side, size_usdc, fill_price, status, mode) "
            "VALUES (?,?,?,?,?,?)",
            (f"0x{i}", "NO", 6.0, 0.62, "OPEN", "live"))


class TestSnapshotIsConsistentAndVerified:
    def test_snapshot_round_trips_with_every_row(self, wired):
        dbmod, B = wired
        _seed_trades(dbmod, 5)
        gz = B.compress_snapshot(B.create_snapshot())
        res = B.restore_check(gz)
        assert res["ok"], res
        assert res["counts"]["trades"] == 5

    def test_snapshot_is_taken_while_the_db_is_open_for_writing(self, wired):
        """`cp` on a live SQLite file can tear a transaction. The online backup
        API cannot, which is the only reason this module exists rather than a
        one-line shell command."""
        dbmod, B = wired
        _seed_trades(dbmod, 2)
        live = sqlite3.connect(dbmod.DB_PATH)
        live.execute("BEGIN")
        live.execute("INSERT INTO trades (market_id, side, status, mode) VALUES (?,?,?,?)",
                     ("0xuncommitted", "NO", "OPEN", "live"))
        try:
            gz = B.compress_snapshot(B.create_snapshot())
        finally:
            live.rollback()
            live.close()
        res = B.restore_check(gz)
        assert res["ok"]
        # The uncommitted row must not be in the snapshot: it was never committed.
        assert res["counts"]["trades"] == 2

    def test_row_loss_is_rejected_not_recorded(self, wired, monkeypatch):
        """A snapshot holding fewer rows than the live DB is the exact artifact
        that must never be allowed to overwrite the previous good one."""
        dbmod, B = wired
        _seed_trades(dbmod, 4)
        with pytest.raises(B.BackupError, match="lost rows"):
            B._verify_snapshot_file(dbmod.DB_PATH, {"trades": 99})

    def test_a_foreign_database_is_rejected(self, wired, tmp_path):
        """Structurally valid SQLite that is not this bot's DB is not a backup."""
        dbmod, B = wired
        alien = str(tmp_path / "alien.db")
        conn = sqlite3.connect(alien)
        conn.execute("CREATE TABLE unrelated (x INT)")
        conn.commit()
        conn.close()
        with pytest.raises(B.BackupError, match="missing table"):
            B._verify_snapshot_file(alien, {"trades": 0})

    def test_corrupt_snapshot_fails_integrity_check(self, wired, tmp_path):
        dbmod, B = wired
        _seed_trades(dbmod, 2)
        snap = B.create_snapshot()
        with open(snap, "r+b") as fh:      # scribble over the middle of the file
            fh.seek(os.path.getsize(snap) // 2)
            fh.write(b"\xff" * 4096)
        with pytest.raises(B.BackupError):
            B._verify_snapshot_file(snap, {"trades": 2})


class TestRotationCannotEatTheLastGoodCopy:
    def test_prune_keeps_the_newest_n(self, wired):
        dbmod, B = wired
        _seed_trades(dbmod, 1)
        for i in range(5):
            B.compress_snapshot(B.create_snapshot(dest_path=os.path.join(
                B.BACKUP_DIR, f"bot-2026080{i}T120000Z.db")))
        B.prune_local(keep=2)
        left = sorted(f for f in os.listdir(B.BACKUP_DIR) if f.endswith(".db.gz"))
        assert left == ["bot-20260803T120000Z.db.gz", "bot-20260804T120000Z.db.gz"]

    def test_a_hand_made_backup_is_neither_pruned_nor_counted_as_fresh(self, wired):
        """/data/backups already holds a manual `bot-db-2026-07-24.db.gz`. Under
        a loose `bot-*.db.gz` glob that name sorts after every generated one, so
        it would be reported as the latest backup forever AND would eventually
        be rotated away by the automation that did not create it."""
        dbmod, B = wired
        _seed_trades(dbmod, 1)
        manual = os.path.join(B.BACKUP_DIR, "bot-db-2026-07-24.db.gz")
        open(manual, "wb").close()
        for i in range(4):
            B.compress_snapshot(B.create_snapshot(dest_path=os.path.join(
                B.BACKUP_DIR, f"bot-2026080{i}T120000Z.db")))
        B.prune_local(keep=1)
        assert os.path.exists(manual), "rotation deleted a human's deliberate backup"
        assert B.latest_local_snapshot().endswith("bot-20260803T120000Z.db.gz")

    def test_age_comes_from_the_name_not_the_mtime(self, wired):
        """A restore, an rsync or a plain copy resets mtime, which would make a
        months-old snapshot look minutes fresh at exactly the wrong moment."""
        dbmod, B = wired
        _seed_trades(dbmod, 1)
        old = B.compress_snapshot(B.create_snapshot(dest_path=os.path.join(
            B.BACKUP_DIR, "bot-20260101T000000Z.db")))
        os.utime(old, None)                    # mtime = now
        st = B.backup_status()
        assert st["latest_age_hours"] > 24 * 100
        assert any("old" in p for p in st["problems"])

    def test_a_failed_snapshot_leaves_previous_backups_intact(self, wired, monkeypatch):
        """Pruning runs only after a verified snapshot. If verification fails,
        the previous copy is still the newest thing on disk."""
        dbmod, B = wired
        _seed_trades(dbmod, 1)
        good = B.compress_snapshot(B.create_snapshot())
        monkeypatch.setattr(B, "_verify_snapshot_file",
                            lambda *a, **k: (_ for _ in ()).throw(B.BackupError("nope")))
        res = B.run_backup()
        assert not res["ok"] and "nope" in res["error"]
        assert os.path.exists(good), "the last good backup was deleted by a failing run"

    def test_a_failed_snapshot_leaves_no_partial_file(self, wired, monkeypatch):
        dbmod, B = wired
        _seed_trades(dbmod, 1)
        monkeypatch.setattr(B, "_verify_snapshot_file",
                            lambda *a, **k: (_ for _ in ()).throw(B.BackupError("nope")))
        with pytest.raises(B.BackupError):
            B.create_snapshot()
        assert [f for f in os.listdir(B.BACKUP_DIR)] == []


class TestOffBoxIsNotAssumed:
    def test_local_only_is_reported_as_a_problem(self, wired):
        """A snapshot next to the database is not an off-box backup, and the
        status must say so rather than showing a reassuring green tick."""
        dbmod, B = wired
        _seed_trades(dbmod, 1)
        res = B.run_backup()
        assert res["ok"] and res["pushed"] is False
        st = B.backup_status()
        assert st["off_box_configured"] is False
        assert any("off-box" in p for p in st["problems"])

    def test_configured_destination_receives_the_bytes(self, wired, monkeypatch):
        dbmod, B = wired
        _seed_trades(dbmod, 3)
        monkeypatch.setattr(B, "BACKUP_PUT_URL_TEMPLATE",
                            "https://example.invalid/bucket/{name}?sig=x")
        sent = {}

        class _Resp:
            status_code = 200
            text = ""

        class _Sess:
            def put(self, url, data=None, headers=None, timeout=None):
                sent["url"] = url
                sent["body"] = data.read()
                return _Resp()

        import utils
        monkeypatch.setattr(utils, "get_session", lambda: _Sess())
        res = B.run_backup()
        assert res["ok"] and res["pushed"] is True
        assert "/bucket/bot-" in sent["url"] and sent["url"].endswith(".db.gz?sig=x")
        # What arrived must be the actual snapshot, not an empty or truncated body.
        assert sent["body"][:2] == b"\x1f\x8b"          # gzip magic
        assert len(gzip.decompress(sent["body"])) > 0

    def test_a_rejected_push_is_a_failure_not_a_silent_local_backup(self, wired, monkeypatch):
        """The dangerous state is a configured destination that quietly stopped
        accepting uploads — indistinguishable, in a log, from never having been
        configured."""
        dbmod, B = wired
        _seed_trades(dbmod, 1)
        monkeypatch.setattr(B, "BACKUP_PUT_URL_TEMPLATE", "https://example.invalid/{name}")

        class _Resp:
            status_code = 403
            text = "denied"

        class _Sess:
            def put(self, *a, **k):
                return _Resp()

        import utils
        monkeypatch.setattr(utils, "get_session", lambda: _Sess())
        res = B.run_backup()
        assert res["ok"] is False
        assert "403" in res["error"]

    def test_stale_backup_is_reported(self, wired, monkeypatch):
        dbmod, B = wired
        _seed_trades(dbmod, 1)
        B.run_backup()
        monkeypatch.setattr(B, "BACKUP_MAX_AGE_HOURS", -1.0)   # everything is stale
        st = B.backup_status()
        assert any("old" in p or "absent" in p for p in st["problems"])


class TestScheduledWiring:
    def test_backup_is_scheduled_before_the_purge(self):
        """_daily_purge deletes signal rows and VACUUMs. A backup taken after it
        has already lost the day's skip trail — the largest calibration sample
        this system produces."""
        import re
        src = open(os.path.join(os.path.dirname(__file__), "..", "main.py")).read()
        backup_at = re.search(r'day\.at\("(\d\d:\d\d)"\)\.do\(_daily_backup\)', src)
        purge_at = re.search(r'day\.at\("(\d\d:\d\d)"\)\.do\(_daily_purge\)', src)
        assert backup_at and purge_at, "both jobs must be scheduled"
        assert backup_at.group(1) < purge_at.group(1)
