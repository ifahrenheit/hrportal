#!/usr/bin/env python3
"""
setup_coaching_perms.py — add the two coaching permission columns to
leave4day_sub_admins (orangehrm2).

Reads DB creds from .env (same vars app.py uses):
    DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD

The coaching_sessions table already exists in central_db (shared with the PHP
page), so this script ONLY handles permissions. MySQL here does not support
ADD COLUMN IF NOT EXISTS, so we check information_schema first.

Usage:
    python3 setup_coaching_perms.py          # dry-run (default)
    python3 setup_coaching_perms.py --live   # apply
"""
import os
import sys
import pymysql

# Load .env if python-dotenv is available; otherwise rely on the environment.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

LIVE = "--live" in sys.argv

DB = dict(
    host=os.environ.get("DB_HOST", "localhost"),
    port=int(os.environ.get("DB_PORT", 3306)),
    user=os.environ.get("DB_USER", "root"),
    password=os.environ.get("DB_PASSWORD", ""),
    database=os.environ.get("DB_NAME", "orangehrm2"),
)

PERM_COLS = [
    ("can_coaching",         "TINYINT(1) NOT NULL DEFAULT 0"),
    ("can_coaching_reports", "TINYINT(1) NOT NULL DEFAULT 0"),
]


def col_exists(cur, db, table, col):
    cur.execute("""
        SELECT COUNT(*) FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND COLUMN_NAME=%s
    """, (db, table, col))
    return cur.fetchone()[0] > 0


def main():
    print(f"[{'LIVE' if LIVE else 'DRY-RUN'}] coaching permission setup")
    print(f"  target: {DB['user']}@{DB['host']}:{DB['port']}/{DB['database']}\n")
    conn = pymysql.connect(**DB)
    try:
        with conn.cursor() as cur:
            for col, ddl in PERM_COLS:
                if col_exists(cur, DB["database"], "leave4day_sub_admins", col):
                    print(f"  {col} already exists — skip")
                else:
                    print(f"  ALTER TABLE leave4day_sub_admins ADD COLUMN {col}")
                    if LIVE:
                        cur.execute(
                            f"ALTER TABLE leave4day_sub_admins ADD COLUMN {col} {ddl}")
            if LIVE:
                conn.commit()
    finally:
        conn.close()
    print("\nDone." + ("" if LIVE else "  Re-run with --live to apply."))


if __name__ == "__main__":
    main()
