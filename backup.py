"""Off-box backup of the SQLite database.

Everything this project knows lives in one file on one Fly volume: the trade
ledger, the bankroll history, the resolutions that every calibration constant is
fitted against, and the replay log that is the only record of what the bot
believed at decision time. None of it can be reconstructed from Polymarket (the
order history is there, the *reasoning* is not) and none of it can be
reconstructed from Open-Meteo (forecasts are not archived at the free tier).
A volume loss is a total loss of the research asset, which is the actual product
here — the money is a rounding error next to it.

Three properties, in order of how badly their absence has bitten similar setups:

1. CONSISTENT. Taken through SQLite's online backup API, not `cp`. The bot
   writes on a 5-minute monitor cycle and a 10-minute scan cycle; copying the
   file byte-wise mid-transaction yields an archive that opens fine and is
   subtly torn. `conn.backup()` takes a read lock per page batch and produces a
   snapshot that is transactionally whole.

2. VERIFIED. Every snapshot is reopened, `PRAGMA integrity_check`-ed, and
   row-counted against the live DB before it is allowed to count as a backup.
   An unverified backup is not a backup, it is a belief about a backup — and the
   failure mode that matters (a wiped or truncated DB copied faithfully to cold
   storage, then rotated over the last good copy) passes every check that does
   not actually look inside.

3. OFF-BOX. A snapshot in /data/backups dies with /data. Local snapshots are
   kept because they make same-day recovery instant, but they are explicitly NOT
   counted as backups by `backup_status()`, and the dashboard treats a stale
   off-box push as a problem to report rather than a setting to ignore.

Destination: an HTTP PUT to BACKUP_PUT_URL_TEMPLATE. That is deliberately the
dumbest possible transport — a Cloudflare R2 / Backblaze B2 / S3 presigned URL,
or any bucket that accepts an authenticated PUT — because the alternative is
carrying an AWS SDK and a credential rotation story into a single-file bot. If
no destination is configured, `/api/backup` still serves the latest verified
snapshot to an authenticated pull (which is how a laptop cron can be the
off-box destination), and backup_status() reports the gap rather than staying
quiet about it.
"""
import gzip
import hashlib
import logging
import os
import re
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone

import config as _cfg

# Local snapshot directory. Lives on the same volume as the DB by design: it is
# the fast-recovery copy, not the disaster copy.
BACKUP_DIR = os.getenv("BACKUP_DIR", os.path.join(os.path.dirname(os.path.abspath(_cfg.DB_PATH)), "backups"))

# How many local snapshots to keep. Small: the DB is a few hundred MB at most
# and the volume has already hit 100% full once.
BACKUP_KEEP_LOCAL = int(os.getenv("BACKUP_KEEP_LOCAL", "3"))

# Off-box destination. `{name}` is substituted with the snapshot filename.
# Example (R2/S3 presigned): https://acct.r2.cloudflarestorage.com/bucket/{name}?X-Amz-...
BACKUP_PUT_URL_TEMPLATE = os.getenv("BACKUP_PUT_URL_TEMPLATE", "").strip()

# Optional bearer/auth header for destinations that want one instead of a
# presigned query string.
BACKUP_PUT_AUTH_HEADER = os.getenv("BACKUP_PUT_AUTH_HEADER", "").strip()

# An off-box push older than this is reported as a problem.
BACKUP_MAX_AGE_HOURS = float(os.getenv("BACKUP_MAX_AGE_HOURS", "36"))

# Tables whose emptiness means the snapshot is worthless even if sqlite says the
# file is structurally fine. `trades` and `resolutions` are the irreplaceable
# ones; `bankroll` is the ledger the money reconciles against.
_SENTINEL_TABLES = ("trades", "bankroll", "resolutions")

# Snapshots THIS MODULE manages, by exact name shape. Deliberately strict rather
# than a `bot-*.db.gz` glob: /data/backups already contains a hand-made
# `bot-db-2026-07-24.db.gz`, and a loose glob both (a) counted that file as the
# freshness signal — it sorts after every generated name, so the status endpoint
# would report a 12-day-old manual copy as the latest backup forever — and (b)
# made it eligible for rotation, i.e. the automation would eventually delete a
# backup a human took on purpose. Files that do not match are left alone.
_SNAPSHOT_RE = re.compile(r"^bot-\d{8}T\d{6}Z\.db\.gz$")


class BackupError(RuntimeError):
    """A backup that did not verify. Raised rather than logged-and-swallowed so
    the scheduled job records a failure instead of a silent no-op."""


def _db_path():
    # Read through db.py so the absolute-path resolution is done in exactly one
    # place — a backup of a different file than the bot writes to is the one
    # bug this module must not have.
    from db import DB_PATH
    return DB_PATH


def _table_counts(conn, tables=_SENTINEL_TABLES):
    """Row count per table, with missing tables reported as None rather than 0.

    The distinction matters: a fresh DB legitimately has zero trades, while a
    *missing* trades table means the snapshot is of something that is not this
    bot's database."""
    out = {}
    for t in tables:
        try:
            out[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except sqlite3.Error:
            out[t] = None
    return out


def create_snapshot(dest_path=None):
    """Consistent, verified snapshot of the live DB. Returns the .db path.

    Uses sqlite3's online backup API against a live connection, so it is safe to
    call while the bot is trading. Verification happens here, not at the call
    site: a snapshot that fails integrity_check or loses rows relative to the
    live DB never becomes a file anyone can mistake for a backup."""
    src_path = _db_path()
    if not os.path.exists(src_path):
        raise BackupError(f"source DB does not exist: {src_path}")

    if dest_path is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        os.makedirs(BACKUP_DIR, exist_ok=True)
        dest_path = os.path.join(BACKUP_DIR, f"bot-{stamp}.db")

    # Snapshot into a temp file first, so a crash mid-copy cannot leave a
    # truncated file sitting in the backup directory looking like a backup.
    tmp_fd, tmp_path = tempfile.mkstemp(prefix="bot-snap-", suffix=".db",
                                        dir=os.path.dirname(dest_path))
    os.close(tmp_fd)
    try:
        src = sqlite3.connect(src_path)
        try:
            live_counts = _table_counts(src)
            dst = sqlite3.connect(tmp_path)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()

        _verify_snapshot_file(tmp_path, live_counts)
        os.replace(tmp_path, dest_path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

    logging.info(f"DB snapshot created: {dest_path} ({os.path.getsize(dest_path):,} bytes)")
    return dest_path


def _verify_snapshot_file(path, live_counts):
    """Raise unless `path` is a structurally sound snapshot holding at least as
    many rows as the live DB did when the copy started.

    'At least' rather than 'exactly': the bot may commit a trade between the
    count and the end of the page copy, so the snapshot can legitimately run
    ahead. It can never legitimately run behind."""
    # A badly enough damaged file makes sqlite RAISE on integrity_check rather
    # than return a non-"ok" row, so both outcomes have to funnel to the same
    # place. Callers get exactly one exception type to handle, which is what
    # lets run_backup treat "corrupt" and "short" identically: neither may be
    # allowed to become the newest file in the backup directory.
    conn = sqlite3.connect(path)
    try:
        try:
            res = conn.execute("PRAGMA integrity_check").fetchone()
        except sqlite3.DatabaseError as e:
            raise BackupError(f"snapshot is unreadable ({e}): {path}") from e
        if not res or res[0] != "ok":
            raise BackupError(f"integrity_check failed on {path}: {res}")
        snap_counts = _table_counts(conn)
    finally:
        conn.close()

    for table, live_n in live_counts.items():
        snap_n = snap_counts.get(table)
        if live_n is None:
            continue  # table absent in the live DB too — not this snapshot's fault
        if snap_n is None:
            raise BackupError(f"snapshot is missing table {table!r} — not a copy of this DB")
        if snap_n < live_n:
            raise BackupError(
                f"snapshot lost rows in {table!r}: live={live_n} snapshot={snap_n}"
            )
    return snap_counts


def compress_snapshot(db_path, remove_source=True):
    """gzip the snapshot. Returns the .db.gz path.

    SQLite pages compress ~4-6x here (mostly repeated JSON in signals/replay),
    which matters because the transport is a single HTTP PUT over a Fly
    machine's uplink."""
    gz_path = db_path + ".gz"
    with open(db_path, "rb") as fin, gzip.open(gz_path, "wb", compresslevel=6) as fout:
        shutil.copyfileobj(fin, fout, length=1024 * 1024)
    if remove_source:
        os.unlink(db_path)
    return gz_path


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def push_snapshot(gz_path):
    """PUT the compressed snapshot to the off-box destination.

    Returns True on success, False when no destination is configured (which is a
    reportable state, not an error — see backup_status). Raises on a configured
    destination that rejects the upload, because a failing push must not be
    indistinguishable from an absent one."""
    if not BACKUP_PUT_URL_TEMPLATE:
        return False

    from utils import get_session

    name = os.path.basename(gz_path)
    url = BACKUP_PUT_URL_TEMPLATE.replace("{name}", name)
    headers = {"Content-Type": "application/gzip"}
    if BACKUP_PUT_AUTH_HEADER and ":" in BACKUP_PUT_AUTH_HEADER:
        k, v = BACKUP_PUT_AUTH_HEADER.split(":", 1)
        headers[k.strip()] = v.strip()

    size = os.path.getsize(gz_path)
    with open(gz_path, "rb") as fh:
        resp = get_session().put(url, data=fh, headers=headers, timeout=300)
    if resp.status_code not in (200, 201, 204):
        raise BackupError(
            f"off-box PUT rejected ({resp.status_code}): {resp.text[:200]}"
        )
    logging.info(f"Off-box backup pushed: {name} ({size:,} bytes) -> {url.split('?')[0]}")
    return True


def prune_local(keep=None):
    """Keep the newest `keep` local snapshots, delete the rest.

    Deliberately runs AFTER a successful new snapshot, never before: pruning
    first would, on a failing snapshot path, delete the last good copy in order
    to make room for one that never arrives."""
    keep = BACKUP_KEEP_LOCAL if keep is None else keep
    if not os.path.isdir(BACKUP_DIR):
        return []
    snaps = _managed_snapshots()
    removed = []
    for stale in snaps[keep:]:
        try:
            os.unlink(os.path.join(BACKUP_DIR, stale))
            removed.append(stale)
        except OSError as e:
            logging.warning(f"Could not prune old backup {stale}: {e}")
    return removed


def _managed_snapshots():
    """Generated snapshot filenames, newest first. The name is an ISO-8601 UTC
    stamp, so lexicographic order IS chronological order — no stat() per file
    and no mtime, which a copy or a restore would have reset."""
    if not os.path.isdir(BACKUP_DIR):
        return []
    return sorted((f for f in os.listdir(BACKUP_DIR) if _SNAPSHOT_RE.match(f)),
                  reverse=True)


def latest_local_snapshot():
    """Path to the newest snapshot this module generated, or None."""
    snaps = _managed_snapshots()
    return os.path.join(BACKUP_DIR, snaps[0]) if snaps else None


def _record(kind, message, severity):
    """Write the outcome to the notifications table so it shows in the dashboard.

    Best-effort: the backup must not fail because the DB it is backing up cannot
    be written to — that is precisely the situation where the backup matters."""
    try:
        from db import add_notification
        add_notification(kind, message, severity)
    except Exception as e:
        logging.error(f"Could not record backup notification: {e}")


def run_backup():
    """Full cycle: snapshot -> verify -> compress -> push off-box -> prune.

    This is the scheduled entrypoint. Returns a dict describing what happened;
    raises nothing, because a scheduler job that raises is a job that silently
    stops being scheduled in some runners. The failure is recorded instead."""
    started = datetime.now(timezone.utc)
    out = {"ok": False, "pushed": False, "path": None, "sha256": None,
           "bytes": None, "error": None,
           "started_at": started.isoformat()}
    try:
        db_snap = create_snapshot()
        gz = compress_snapshot(db_snap)
        out["path"] = gz
        out["bytes"] = os.path.getsize(gz)
        out["sha256"] = sha256_of(gz)
        out["pushed"] = push_snapshot(gz)
        out["ok"] = True
        prune_local()

        if out["pushed"]:
            _record("backup", f"Off-box backup pushed ({out['bytes']:,} bytes, "
                              f"sha256 {out['sha256'][:12]})", "info")
        else:
            _record("backup",
                    "Local snapshot taken but NO off-box destination is configured "
                    "(BACKUP_PUT_URL_TEMPLATE unset) — a volume loss is still a total loss. "
                    "Pull /api/backup, or set the destination.",
                    "warning")
    except Exception as e:
        out["error"] = str(e)
        logging.error(f"Backup failed: {e}", exc_info=True)
        _record("backup", f"BACKUP FAILED: {e}", "error")
    out["finished_at"] = datetime.now(timezone.utc).isoformat()
    return out


def backup_status():
    """What the dashboard and preflight need to know, without doing any work.

    `off_box` is the only field that answers the question the module exists for.
    A local snapshot deliberately does not set it."""
    latest = latest_local_snapshot()
    age_hours = None
    if latest:
        # Age from the NAME, not from mtime: mtime is reset by a copy, an rsync
        # or a volume restore, any of which would make a stale backup look
        # minutes old at exactly the moment that matters.
        stamp = datetime.strptime(os.path.basename(latest)[4:20], "%Y%m%dT%H%M%SZ")
        stamp = stamp.replace(tzinfo=timezone.utc)
        age_hours = (datetime.now(timezone.utc) - stamp).total_seconds() / 3600.0

    configured = bool(BACKUP_PUT_URL_TEMPLATE)
    stale = age_hours is None or age_hours > BACKUP_MAX_AGE_HOURS
    problems = []
    if not configured:
        problems.append(
            "No off-box backup destination configured (BACKUP_PUT_URL_TEMPLATE). "
            "Every trade, resolution and replay row lives on one Fly volume."
        )
    if stale:
        problems.append(
            f"Latest local snapshot is "
            f"{'absent' if age_hours is None else f'{age_hours:.1f}h old'} "
            f"(limit {BACKUP_MAX_AGE_HOURS:.0f}h)."
        )
    return {
        "off_box_configured": configured,
        "latest_local": latest,
        "latest_age_hours": age_hours,
        "keep_local": BACKUP_KEEP_LOCAL,
        "max_age_hours": BACKUP_MAX_AGE_HOURS,
        "problems": problems,
    }


def restore_check(gz_path):
    """Prove a compressed snapshot is restorable, without touching the live DB.

    A backup nobody has ever restored is a hypothesis. This decompresses to a
    temp file, opens it, integrity-checks it, and reports the sentinel table
    counts, so 'do we have a backup' has an answer that involves reading the
    bytes back."""
    with tempfile.NamedTemporaryFile(prefix="bot-restore-", suffix=".db", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        with gzip.open(gz_path, "rb") as fin, open(tmp_path, "wb") as fout:
            shutil.copyfileobj(fin, fout, length=1024 * 1024)
        conn = sqlite3.connect(tmp_path)
        try:
            res = conn.execute("PRAGMA integrity_check").fetchone()
            ok = bool(res and res[0] == "ok")
            counts = _table_counts(conn)
        finally:
            conn.close()
        return {"ok": ok, "integrity": res[0] if res else None, "counts": counts,
                "bytes": os.path.getsize(gz_path)}
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


if __name__ == "__main__":
    import json as _json
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "status":
        print(_json.dumps(backup_status(), indent=2))
    elif len(sys.argv) > 2 and sys.argv[1] == "restore-check":
        print(_json.dumps(restore_check(sys.argv[2]), indent=2))
    else:
        print(_json.dumps(run_backup(), indent=2))
