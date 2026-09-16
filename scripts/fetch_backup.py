#!/usr/bin/env python3
"""
Fetch Live SQLite Database from Fly.io to local backups/ directory.
Uses authenticated HTTPS stream with chunked range transfers and integrity checks.
"""
import os
import sys
import time
import gzip
import json
import shutil
import sqlite3
import hashlib
import http.cookiejar
import urllib.request
from datetime import datetime, timezone

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKUPS_DIR = os.path.join(APP_DIR, "backups")
os.makedirs(BACKUPS_DIR, exist_ok=True)

APP_URL = "https://stormedgev2.fly.dev"
EMAIL = "donaldemmaogbame@gmail.com"
PASSWORD = "stormedge"

import argparse

def main():
    parser = argparse.ArgumentParser(description="Fetch live SQLite database from Fly.io to local backups/ and optionally sync to data/bot.db")
    parser.add_argument("--no-snapshot", action="store_true", help="Skip triggering a fresh snapshot on Fly before download")
    parser.add_argument("--sync-data", action="store_true", default=True, help="Also copy the downloaded snapshot to data/bot.db (default: True)")
    parser.add_argument("--no-sync-data", dest="sync_data", action="store_false", help="Do not copy to data/bot.db")
    args = parser.parse_args()

    t_start = time.time()
    print("==================================================")
    print("1. AUTHENTICATING TO FLY DASHBOARD")
    print("==================================================")
    cookie_jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))

    login_url = f"{APP_URL}/api/login"
    login_data = json.dumps({"email": EMAIL, "password": PASSWORD}).encode("utf-8")
    req = urllib.request.Request(login_url, data=login_data, headers={"Content-Type": "application/json"})
    try:
        resp = opener.open(req, timeout=30)
        assert resp.status == 200, f"Login returned status {resp.status}"
        print(f"✓ Authenticated as {EMAIL}")
    except Exception as e:
        print(f"Authentication failed: {e}", file=sys.stderr)
        sys.exit(1)

    if not args.no_snapshot:
        print("\n==================================================")
        print("2. TRIGGERING FRESH LIVE SNAPSHOT ON FLY")
        print("==================================================")
        try:
            run_url = f"{APP_URL}/api/backup/run"
            run_req = urllib.request.Request(run_url, data=b"{}", headers={"Content-Type": "application/json"})
            run_resp = opener.open(run_req, timeout=60)
            run_data = json.loads(run_resp.read().decode("utf-8"))
            if run_data.get("ok"):
                print(f"✓ Fresh live snapshot created on Fly: {run_data.get('path')} ({run_data.get('bytes', 0):,} bytes)")
            else:
                print(f"⚠ Warning: Snapshot returned: {run_data.get('error')}", file=sys.stderr)
        except Exception as e:
            print(f"⚠ Could not trigger fresh snapshot: {e}. Falling back to latest existing snapshot.", file=sys.stderr)

    print("\n==================================================")
    print("3. QUERYING LATEST LIVE BACKUP METADATA")
    print("==================================================")
    download_url = f"{APP_URL}/api/backup/latest"
    try:
        head_req = urllib.request.Request(download_url, headers={"Range": "bytes=0-0"})
        resp_head = opener.open(head_req, timeout=30)
        cr = resp_head.headers.get("Content-Range", "")
        if "/" in cr:
            total_bytes = int(cr.split("/")[-1])
        else:
            total_bytes = int(resp_head.headers.get("Content-Length", 0))
        content_disp = resp_head.headers.get("Content-Disposition", "")
        filename = "latest.db.gz"
        if "filename=" in content_disp:
            filename = content_disp.split("filename=")[-1].strip("\"'")
        print(f"Remote Backup: {filename} ({total_bytes:,} bytes / {total_bytes/(1024*1024):.2f} MB)")
    except Exception as e:
        print(f"Failed to query backup endpoint: {e}", file=sys.stderr)
        sys.exit(1)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest_gz = os.path.join(BACKUPS_DIR, f"bot-{stamp}.db.gz")
    dest_db = os.path.join(BACKUPS_DIR, f"bot-{stamp}.db")

    print("\n==================================================")
    print("4. DOWNLOADING LIVE SNAPSHOT OVER HTTPS")
    print("==================================================")
    chunk_size = 32 * 1024 * 1024  # 32 MB chunks
    h = hashlib.sha256()
    t_dl = time.time()
    
    with open(dest_gz, "wb") as f_out:
        downloaded = 0
        chunk_idx = 0
        while downloaded < total_bytes:
            end = min(downloaded + chunk_size - 1, total_bytes - 1)
            req_chunk = urllib.request.Request(download_url, headers={"Range": f"bytes={downloaded}-{end}"})
            for attempt in range(5):
                try:
                    t_c = time.time()
                    resp = opener.open(req_chunk, timeout=60)
                    data = resp.read()
                    expected = end - downloaded + 1
                    assert len(data) == expected, f"Short read: {len(data)} != {expected}"
                    f_out.write(data)
                    h.update(data)
                    downloaded += len(data)
                    pct = (downloaded / total_bytes) * 100
                    speed = len(data) / (1024 * 1024) / max(0.001, time.time() - t_c)
                    print(f"  Chunk {chunk_idx+1:2d} ({downloaded/(1024*1024):5.1f} / {total_bytes/(1024*1024):.1f} MB - {pct:5.1f}%) @ {speed:5.1f} MB/s", flush=True)
                    chunk_idx += 1
                    break
                except Exception as e:
                    print(f"  Retry chunk {chunk_idx+1} (attempt {attempt+1}): {e}", flush=True)
                    time.sleep(2)
        f_out.flush()
        os.fsync(f_out.fileno())

    gz_size = os.path.getsize(dest_gz)
    sha256 = h.hexdigest()
    print(f"✓ Downloaded {gz_size:,} bytes in {time.time()-t_dl:.1f}s")
    print(f"  SHA-256: {sha256}")

    print("\n==================================================")
    print("5. DECOMPRESSING TO LOCAL SQLITE DATABASE")
    print("==================================================")
    t_dec = time.time()
    with gzip.open(dest_gz, "rb") as fin, open(dest_db, "wb") as fout:
        shutil.copyfileobj(fin, fout, length=4 * 1024 * 1024)
        fout.flush()
        os.fsync(fout.fileno())

    db_size = os.path.getsize(dest_db)
    print(f"✓ Decompressed to {dest_db} in {time.time()-t_dec:.1f}s ({db_size:,} bytes / {db_size/(1024*1024):.2f} MB)")

    print("\n==================================================")
    print("6. VERIFYING SQLITE INTEGRITY & AUDITING TABLES")
    print("==================================================")
    conn = sqlite3.connect(dest_db)
    res_integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    print(f"PRAGMA integrity_check: {res_integrity}")
    if res_integrity != "ok":
        print(f"FATAL: Database corruption: {res_integrity}", file=sys.stderr)
        sys.exit(1)

    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()]
    print(f"\nDiscovered {len(tables)} tables:")
    for t in tables:
        cnt = conn.execute(f"SELECT COUNT(*) FROM \"{t}\"").fetchone()[0]
        print(f"  - {t:<30}: {cnt:>12,} rows")

    print("\n--- Latest 3 Trades ---")
    tcols = [c[1] for c in conn.execute("PRAGMA table_info(trades)").fetchall()]
    trades = conn.execute("SELECT * FROM trades ORDER BY id DESC LIMIT 3").fetchall()
    if trades:
        for row in trades:
            d = dict(zip(tcols, row))
            print(f"  Trade #{d.get('id')}: {d.get('city')} | {d.get('side')} | Fill: {d.get('fill_price')} | Size: ${d.get('size_usdc')} | Status: {d.get('status')} | Time: {d.get('entry_time')}")
    else:
        print("  (No trades recorded in this database)")

    print("\n--- Latest 3 Bankroll Entries ---")
    bcols = [c[1] for c in conn.execute("PRAGMA table_info(bankroll)").fetchall()]
    bankrolls = conn.execute("SELECT * FROM bankroll ORDER BY id DESC LIMIT 3").fetchall()
    if bankrolls:
        for row in bankrolls:
            d = dict(zip(bcols, row))
            print(f"  Bankroll #{d.get('id')}: Event: {d.get('event')} | Amount: ${d.get('amount')} | Balance: ${d.get('balance')} | Mode: {d.get('mode')} | Time: {d.get('timestamp')}")
    else:
        print("  (No bankroll entries recorded in this database)")

    conn.close()

    if args.sync_data:
        print("\n==================================================")
        print("7. SYNCING TO LOCAL WORKING DATABASE (data/bot.db)")
        print("==================================================")
        local_db_path = os.path.join(APP_DIR, "data", "bot.db")
        os.makedirs(os.path.dirname(local_db_path), exist_ok=True)
        if os.path.exists(local_db_path):
            backup_local = f"{local_db_path}.bak"
            shutil.copy2(local_db_path, backup_local)
            print(f"✓ Backed up prior local DB to {backup_local}")
        for ext in ["-wal", "-shm"]:
            stale_f = f"{local_db_path}{ext}"
            if os.path.exists(stale_f):
                try:
                    os.unlink(stale_f)
                except OSError:
                    pass
        shutil.copy2(dest_db, local_db_path)
        print(f"✓ Copied live snapshot to {local_db_path}")

    print("\n==================================================")
    print(f"✓ BACKUP COMPLETED & VERIFIED IN {time.time()-t_start:.1f}s")
    print(f"  SQLite DB:  {dest_db}")
    print(f"  Compressed: {dest_gz}")
    if args.sync_data:
        print(f"  Working DB: {os.path.join(APP_DIR, 'data', 'bot.db')}")
    print("==================================================")

if __name__ == "__main__":
    main()

