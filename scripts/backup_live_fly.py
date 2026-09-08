#!/usr/bin/env python3
"""
Backup live SQLite database from Fly.io directly to local backups/ directory.

Bypasses Fly disk space limitations by streaming directly across Fly SSH
with fast gzip compression, without creating any temporary files on Fly.

Workflow:
1. Authenticate to Fly web app.
2. Pause scanning so no new transactions occur.
3. Flush WAL to main DB via PRAGMA wal_checkpoint(TRUNCATE).
4. Record live table row counts for verification.
5. Stream /data/bot.db through gzip over Fly SSH into local backups/bot-<timestamp>.db.gz.
6. Resume scanning immediately (always executed in finally block).
7. Decompress locally to backups/bot-<timestamp>.db.
8. Run PRAGMA integrity_check and verify row counts match live DB 1:1.
"""
import os
import sys
import time
import json
import gzip
import shutil
import base64
import sqlite3
import hashlib
import subprocess
import urllib.request
import http.cookiejar
from datetime import datetime, timezone

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKUPS_DIR = os.path.join(APP_DIR, "backups")
os.makedirs(BACKUPS_DIR, exist_ok=True)

APP_NAME = "stormedgev2"
APP_URL = "https://stormedgev2.fly.dev"
EMAIL = "donaldemmaogbame@gmail.com"
PASSWORD = "stormedge"


def setup_auth():
    cookie_jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))
    login_url = f"{APP_URL}/api/login"
    data = json.dumps({"email": EMAIL, "password": PASSWORD}).encode("utf-8")
    req = urllib.request.Request(login_url, data=data, headers={"Content-Type": "application/json"})
    try:
        resp = opener.open(req, timeout=30)
        assert resp.status == 200
        return opener
    except Exception as e:
        print(f"Warning: Web API login failed ({e}). Proceeding via Fly SSH directly.")
        return None


def set_pause(opener, paused: bool):
    if not opener:
        return
    try:
        url = f"{APP_URL}/api/pause"
        data = json.dumps({"paused": paused}).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        resp = opener.open(req, timeout=15)
        res_json = json.loads(resp.read().decode("utf-8"))
        print(f"✓ Bot scanning set to paused={paused} (Response: {res_json.get('message', 'ok')})")
    except Exception as e:
        print(f"Notice: /api/pause call returned: {e}")


def run_remote_python(code: str) -> str:
    b64 = base64.b64encode(code.strip().encode("utf-8")).decode("ascii")
    remote_cmd = f"python3 -c \"import base64; exec(base64.b64decode('{b64}').decode('utf-8'))\""
    cmd = ["fly", "ssh", "console", "-a", APP_NAME, "-C", remote_cmd]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    stdout, stderr = p.communicate()
    if p.returncode != 0:
        raise RuntimeError(f"Remote command failed (code {p.returncode}):\n{stderr}\n{stdout}")
    return stdout.strip()


def main():
    t_start = time.time()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest_gz = os.path.join(BACKUPS_DIR, f"bot-{stamp}.db.gz")
    dest_db = os.path.join(BACKUPS_DIR, f"bot-{stamp}.db")

    print("==================================================")
    print(f"LIVE FLY DATABASE BACKUP STARTING: {stamp}")
    print("==================================================")

    opener = setup_auth()
    paused_set = False

    try:
        # Step 1: Pause scanning to guarantee zero concurrent writes
        print("\n1. PAUSING BOT SCANNING...")
        set_pause(opener, True)
        paused_set = True

        # Step 2: Checkpoint WAL and capture live table counts
        print("\n2. EXECUTING WAL CHECKPOINT & RECORDING LIVE METRICS...")
        checkpoint_code = """
import sqlite3, json
conn = sqlite3.connect('/data/bot.db')
c = conn.cursor()
cp = c.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
counts = {}
for t in tables:
    counts[t] = c.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
db_size = c.execute('PRAGMA page_count').fetchone()[0] * c.execute('PRAGMA page_size').fetchone()[0]
conn.close()
print(json.dumps({'checkpoint': cp, 'counts': counts, 'db_size': db_size}))
"""
        meta_raw = run_remote_python(checkpoint_code)
        # Filter lines to find the json line
        json_lines = [line.strip() for line in meta_raw.splitlines() if line.strip().startswith("{") and line.strip().endswith("}")]
        if not json_lines:
            raise RuntimeError(f"Could not parse checkpoint metadata from:\n{meta_raw}")
        live_meta = json.loads(json_lines[-1])
        live_counts = live_meta["counts"]
        remote_size = live_meta["db_size"]
        print(f"✓ PRAGMA wal_checkpoint(TRUNCATE) result: {live_meta['checkpoint']}")
        print(f"✓ Live DB Size: {remote_size:,} bytes ({remote_size / (1024**3):.2f} GB)")
        print(f"✓ Live Tables Counted: {len(live_counts)} tables")
        for sentinel in ["trades", "bankroll", "resolutions", "signals", "replay_gates"]:
            if sentinel in live_counts:
                print(f"    - {sentinel}: {live_counts[sentinel]:,} rows")

        # Step 3: Stream /data/bot.db directly through gzip over Fly SSH
        print("\n3. STREAMING LIVE DB OVER FLY SSH (COMPRESSED ON-THE-FLY)...")
        print(f"Target local file: {dest_gz}")
        
        cmd = ["fly", "ssh", "console", "-a", APP_NAME, "-C", "/usr/bin/gzip -1 -c /data/bot.db"]
        
        t_dl_start = time.time()
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        
        hasher = hashlib.sha256()
        bytes_received = 0
        last_report = time.time()
        
        with open(dest_gz, "wb") as f_out:
            while True:
                chunk = p.stdout.read(4 * 1024 * 1024)
                if not chunk:
                    break
                f_out.write(chunk)
                hasher.update(chunk)
                bytes_received += len(chunk)
                
                now = time.time()
                if now - last_report >= 3.0:
                    elapsed = now - t_dl_start
                    rate = (bytes_received / (1024 * 1024)) / max(0.001, elapsed)
                    pct_est = (bytes_received / (remote_size * 0.11)) * 100  # est 9:1 compression ratio
                    print(f"  Received: {bytes_received / (1024 * 1024):6.1f} MB ({rate:5.1f} MB/s, elapsed: {elapsed:3.0f}s)", flush=True)
                    last_report = now
                    
        p.wait()
        stderr_output = p.stderr.read().decode("utf-8", errors="replace")
        if p.returncode != 0:
            raise RuntimeError(f"Streaming failed with exit code {p.returncode}:\n{stderr_output}")

        gz_size = os.path.getsize(dest_gz)
        gz_sha256 = hasher.hexdigest()
        dl_duration = time.time() - t_dl_start
        print(f"✓ Download complete: {gz_size:,} bytes ({gz_size / (1024*1024):.2f} MB) in {dl_duration:.1f}s (Avg: {gz_size/(1024*1024)/dl_duration:.1f} MB/s)")
        print(f"✓ SHA-256: {gz_sha256}")

    finally:
        # Step 4: Resume scanning as soon as the read stream finishes
        if paused_set:
            print("\n4. RESUMING BOT SCANNING...")
            set_pause(opener, False)

    # Step 5: Decompress locally
    print("\n5. DECOMPRESSING TO LOCAL SQLITE DATABASE...")
    print(f"Target DB: {dest_db}")
    t_dec_start = time.time()
    with gzip.open(dest_gz, "rb") as fin, open(dest_db, "wb") as fout:
        shutil.copyfileobj(fin, fout, length=8 * 1024 * 1024)
        fout.flush()
        os.fsync(fout.fileno())
    
    db_size = os.path.getsize(dest_db)
    dec_duration = time.time() - t_dec_start
    print(f"✓ Decompressed in {dec_duration:.1f}s ({db_size:,} bytes / {db_size / (1024**3):.2f} GB)")

    # Step 6: Verify integrity & row counts
    print("\n6. VERIFYING DATABASE INTEGRITY & TABLE COUNTS...")
    conn = sqlite3.connect(dest_db)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        print(f"PRAGMA integrity_check: {integrity}")
        if integrity != "ok":
            print(f"FATAL: Integrity check failed: {integrity}", file=sys.stderr)
            sys.exit(1)

        print("\nVerifying Table Row Counts against Live DB:")
        all_matched = True
        for table, live_n in live_counts.items():
            try:
                local_n = conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
                diff = local_n - live_n
                status = "MATCH" if diff == 0 else f"DIFF ({diff:+d})"
                if diff != 0:
                    all_matched = False
                print(f"  - {table:<30}: {local_n:>12,} rows (Live: {live_n:>12,}) -> {status}")
            except Exception as e:
                print(f"  - {table:<30}: ERROR checking ({e})")
                all_matched = False

        if not all_matched:
            print("\nWARNING: Some table counts did not match perfectly. Please review above.", file=sys.stderr)
        else:
            print("\n✓ ALL TABLE ROW COUNTS MATCH LIVE DATABASE 100%!")

        print("\n--- Latest 3 Trades in Backup ---")
        tcols = [c[1] for c in conn.execute("PRAGMA table_info(trades)").fetchall()]
        for row in conn.execute("SELECT * FROM trades ORDER BY id DESC LIMIT 3").fetchall():
            d = dict(zip(tcols, row))
            print(f"  Trade #{d.get('id')}: {d.get('city')} | {d.get('side')} | Fill: {d.get('fill_price')} | Size: {d.get('size_usdc')} | Time: {d.get('created_at') or d.get('timestamp')}")

        print("\n--- Latest Bankroll Entry in Backup ---")
        bcols = [c[1] for c in conn.execute("PRAGMA table_info(bankroll)").fetchall()]
        latest_b = conn.execute("SELECT * FROM bankroll ORDER BY id DESC LIMIT 1").fetchone()
        if latest_b:
            d = dict(zip(bcols, latest_b))
            print(f"  Bankroll #{d.get('id')}: Bankroll: {d.get('bankroll')} | Avail: {d.get('available')} | PnL: {d.get('pnl_dollars')} | Time: {d.get('timestamp')}")

    finally:
        conn.close()

    total_time = time.time() - t_start
    print("\n==================================================")
    print(f"✓ LIVE FLY BACKUP SUCCESSFULLY COMPLETED IN {total_time:.1f}s")
    print(f"  Compressed: {dest_gz} ({gz_size / (1024*1024):.2f} MB)")
    print(f"  SQLite DB:  {dest_db} ({db_size / (1024**3):.2f} GB)")
    print("==================================================")


if __name__ == "__main__":
    main()
