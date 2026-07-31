"""Regression tests for the model_accuracy direction key and its partial index.

Background: model_accuracy was keyed (city, target_date, model), so a city's
daily MAX and daily MIN collided under one key and overwrote each other's
`actual`. 24 of 47 verified city-days in the 2026-07-31 export carried two
conflicting actual values because of this. Since highs and lows have opposite
measured biases, a table that cannot tell them apart cannot fit the correction.
"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import db as dbmod

LEGACY_SCHEMA = '''
    CREATE TABLE model_accuracy (
        id INTEGER PRIMARY KEY AUTOINCREMENT, city TEXT, target_date TEXT,
        model TEXT, forecast_temp REAL, actual_temp REAL);
    CREATE TABLE trades (
        id INTEGER PRIMARY KEY, city TEXT, target_date TEXT, is_high INTEGER);
'''

UNIQUE_INDEX = (
    "CREATE UNIQUE INDEX idx_model_acc_unique "
    "ON model_accuracy(city, target_date, is_high, model) "
    "WHERE is_high IS NOT NULL"
)


def _legacy_db():
    path = os.path.join(tempfile.mkdtemp(), "legacy.db")
    conn = sqlite3.connect(path)
    conn.executescript(LEGACY_SCHEMA)
    return conn


def _add(conn, city, date, model, fc, actual):
    conn.execute(
        "INSERT INTO model_accuracy (city,target_date,model,forecast_temp,actual_temp)"
        " VALUES (?,?,?,?,?)", (city, date, model, fc, actual))


def _migrate(conn):
    conn.execute("ALTER TABLE model_accuracy ADD COLUMN is_high INTEGER")
    dbmod._backfill_model_accuracy_is_high(conn)
    conn.execute(UNIQUE_INDEX)
    conn.commit()


class TestBackfill:
    def test_conflicting_day_splits_into_max_and_min(self):
        """The collision IS the signal: a city/date carrying two distinct actuals
        logged both directions, and the larger actual is that day's max."""
        c = _legacy_db()
        _add(c, "Tokyo", "2026-07-20", "ecmwf_ifs025", 95.0, 96.8)  # the max
        _add(c, "Tokyo", "2026-07-20", "ecmwf_ifs025", 74.0, 75.2)  # the min
        _add(c, "Tokyo", "2026-07-20", "icon_global", 94.0, 96.8)
        _add(c, "Tokyo", "2026-07-20", "icon_global", 73.0, 75.2)
        _migrate(c)
        rows = dict(c.execute(
            "SELECT is_high, actual_temp FROM model_accuracy WHERE model='ecmwf_ifs025'"))
        assert rows == {1: 96.8, 0: 75.2}

    def test_no_conflicting_groups_remain(self):
        c = _legacy_db()
        _add(c, "Tokyo", "2026-07-20", "ecmwf_ifs025", 95.0, 96.8)
        _add(c, "Tokyo", "2026-07-20", "ecmwf_ifs025", 74.0, 75.2)
        _migrate(c)
        bad = c.execute(
            "SELECT COUNT(*) FROM (SELECT 1 FROM model_accuracy WHERE is_high IS NOT NULL"
            " GROUP BY city,target_date,is_high,model HAVING COUNT(DISTINCT actual_temp)>1)"
        ).fetchone()[0]
        assert bad == 0

    def test_single_direction_recovered_from_trades(self):
        c = _legacy_db()
        _add(c, "Seoul", "2026-07-21", "ecmwf_ifs025", 90.0, 91.4)
        c.execute("INSERT INTO trades VALUES (1,'Seoul','2026-07-21',0)")
        _migrate(c)
        assert c.execute("SELECT is_high FROM model_accuracy").fetchone()[0] == 0

    def test_unrecoverable_row_left_null_not_guessed(self):
        """Guessing would file a max under a min's bias fit. NULL is correct."""
        c = _legacy_db()
        _add(c, "Lima", "2026-07-22", "gem_global", 70.0, 71.6)
        _migrate(c)
        assert c.execute("SELECT is_high FROM model_accuracy").fetchone()[0] is None

    def test_ambiguous_both_directions_traded_left_null(self):
        """One actual but both directions traded — genuinely unknowable."""
        c = _legacy_db()
        _add(c, "Miami", "2026-07-23", "gfs_global", 88.0, 89.6)
        c.execute("INSERT INTO trades VALUES (1,'Miami','2026-07-23',1)")
        c.execute("INSERT INTO trades VALUES (2,'Miami','2026-07-23',0)")
        _migrate(c)
        assert c.execute("SELECT is_high FROM model_accuracy").fetchone()[0] is None

    def test_exact_duplicates_collapse_keeping_newest(self):
        c = _legacy_db()
        for fc in (88.0, 88.5, 89.0):
            _add(c, "Miami", "2026-07-23", "gfs_global", fc, 89.6)
        c.execute("INSERT INTO trades VALUES (1,'Miami','2026-07-23',1)")
        _migrate(c)
        rows = c.execute("SELECT forecast_temp FROM model_accuracy").fetchall()
        assert rows == [(89.0,)]

    def test_migration_is_safe_on_empty_table(self):
        c = _legacy_db()
        _migrate(c)
        assert c.execute("SELECT COUNT(*) FROM model_accuracy").fetchone()[0] == 0


class TestUpsertMatchesPartialIndex:
    """log_model_accuracy's ON CONFLICT target must carry the same WHERE clause
    as the partial index. Without it SQLite raises 'ON CONFLICT clause does not
    match any PRIMARY KEY or UNIQUE constraint' on EVERY write."""

    def _upsert(self, conn, city, date, is_high, model, fc, actual):
        conn.execute('''
            INSERT INTO model_accuracy (city, target_date, is_high, model, forecast_temp, actual_temp)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (city, target_date, is_high, model) WHERE is_high IS NOT NULL
            DO UPDATE SET forecast_temp=excluded.forecast_temp, actual_temp=excluded.actual_temp
        ''', (city, date, is_high, model, fc, actual))

    def test_upsert_does_not_raise(self):
        c = _legacy_db()
        _migrate(c)
        self._upsert(c, "Tokyo", "2026-07-20", 1, "ecmwf_ifs025", 95.0, 96.8)

    def test_upsert_updates_in_place(self):
        c = _legacy_db()
        _migrate(c)
        self._upsert(c, "Tokyo", "2026-07-20", 1, "ecmwf_ifs025", 95.0, 96.8)
        self._upsert(c, "Tokyo", "2026-07-20", 1, "ecmwf_ifs025", 99.9, 96.8)
        assert c.execute("SELECT forecast_temp FROM model_accuracy").fetchall() == [(99.9,)]

    def test_max_and_min_coexist(self):
        """The whole point: both directions for one city/date, side by side."""
        c = _legacy_db()
        _migrate(c)
        self._upsert(c, "Tokyo", "2026-07-20", 1, "ecmwf_ifs025", 95.0, 96.8)
        self._upsert(c, "Tokyo", "2026-07-20", 0, "ecmwf_ifs025", 74.0, 75.2)
        assert c.execute("SELECT COUNT(*) FROM model_accuracy").fetchone()[0] == 2

    def test_null_row_coexists_with_both_directions(self):
        """A legacy NULL-direction row and both recovered directions can sit on
        the same city/date/model without colliding.

        The partial index excludes NULLs, and SQLite treats NULLs as distinct, so
        neither raises and none overwrites another. This is the state the
        migration actually leaves the production table in — recovered rows beside
        unrecoverable ones — and it was verified by hand but never tested."""
        c = _legacy_db()
        _migrate(c)
        c.execute("INSERT INTO model_accuracy (city,target_date,is_high,model,"
                  "forecast_temp,actual_temp) VALUES ('Tokyo','2026-07-20',NULL,"
                  "'ecmwf_ifs025',90.0,91.0)")
        self._upsert(c, "Tokyo", "2026-07-20", 1, "ecmwf_ifs025", 95.0, 96.8)
        self._upsert(c, "Tokyo", "2026-07-20", 0, "ecmwf_ifs025", 74.0, 75.2)
        rows = c.execute("SELECT is_high, forecast_temp, actual_temp FROM "
                         "model_accuracy ORDER BY id").fetchall()
        assert rows == [(None, 90.0, 91.0), (1, 95.0, 96.8), (0, 74.0, 75.2)]

    def test_upserting_a_direction_does_not_touch_the_null_row(self):
        c = _legacy_db()
        _migrate(c)
        c.execute("INSERT INTO model_accuracy (city,target_date,is_high,model,"
                  "forecast_temp,actual_temp) VALUES ('Tokyo','2026-07-20',NULL,"
                  "'ecmwf_ifs025',90.0,91.0)")
        self._upsert(c, "Tokyo", "2026-07-20", 1, "ecmwf_ifs025", 95.0, 96.8)
        self._upsert(c, "Tokyo", "2026-07-20", 1, "ecmwf_ifs025", 99.9, 96.8)
        assert c.execute("SELECT forecast_temp FROM model_accuracy WHERE "
                         "is_high IS NULL").fetchall() == [(90.0,)]
        assert c.execute("SELECT COUNT(*) FROM model_accuracy").fetchone()[0] == 2

    def test_two_null_rows_are_both_kept(self):
        """NULLs are distinct under the partial index, so unrecoverable legacy
        rows are never deduped. Documented here because it is surprising."""
        c = _legacy_db()
        _migrate(c)
        for fc in (90.0, 89.0):
            c.execute("INSERT INTO model_accuracy (city,target_date,is_high,model,"
                      "forecast_temp,actual_temp) VALUES ('Tokyo','2026-07-20',NULL,"
                      "'ecmwf_ifs025',?,91.0)", (fc,))
        assert c.execute("SELECT COUNT(*) FROM model_accuracy").fetchone()[0] == 2

    def test_duplicate_insert_without_upsert_is_rejected(self):
        c = _legacy_db()
        _migrate(c)
        self._upsert(c, "Tokyo", "2026-07-20", 1, "ecmwf_ifs025", 95.0, 96.8)
        try:
            c.execute("INSERT INTO model_accuracy (city,target_date,is_high,model,"
                      "forecast_temp,actual_temp) VALUES ('Tokyo','2026-07-20',1,"
                      "'ecmwf_ifs025',1,1)")
            raised = False
        except sqlite3.IntegrityError:
            raised = True
        assert raised, "UNIQUE index is not enforcing one row per city/date/direction/model"
