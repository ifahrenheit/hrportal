#!/usr/bin/env python3
"""
Add uploaded_by_side to the existing coaching_attachments table (PHP-era table).
Additive + PHP-safe. Existing rows default to 'tl' (old page was TL-only).
Reads MAIN_DB_* from .env. Idempotent. Dry-run default.
"""
import os, sys, pymysql
try:
    from dotenv import load_dotenv; load_dotenv()
except ImportError:
    pass
LIVE = "--live" in sys.argv
DB = dict(host=os.environ["MAIN_DB_HOST"], port=int(os.environ.get("MAIN_DB_PORT",3306)),
          user=os.environ["MAIN_DB_USER"], password=os.environ["MAIN_DB_PASSWORD"],
          database=os.environ["MAIN_DB_NAME"])

def col_exists(cur, db, t, c):
    cur.execute("""SELECT COUNT(*) FROM information_schema.COLUMNS
                   WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND COLUMN_NAME=%s""",(db,t,c))
    return cur.fetchone()[0] > 0

def main():
    print(f"[{'LIVE' if LIVE else 'DRY-RUN'}] add uploaded_by_side\n")
    conn = pymysql.connect(**DB)
    try:
        with conn.cursor() as cur:
            if col_exists(cur, DB["database"], "coaching_attachments", "uploaded_by_side"):
                print("  uploaded_by_side already exists — skip")
            else:
                print("  ALTER TABLE coaching_attachments ADD COLUMN uploaded_by_side")
                if LIVE:
                    cur.execute("""ALTER TABLE coaching_attachments
                        ADD COLUMN uploaded_by_side ENUM('tl','agent') NOT NULL DEFAULT 'tl'
                        AFTER uploaded_by""")
                    conn.commit()
    finally:
        conn.close()
    print("\nDone." + ("" if LIVE else "  Re-run with --live to apply."))

if __name__ == "__main__":
    main()
