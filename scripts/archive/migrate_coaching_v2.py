#!/usr/bin/env python3
"""
migrate_coaching_v2.py — schema changes for the coaching workflow update.

  1. Widen status ENUM to add 'pending' and 'for_followup' (keep old values so
     the PHP page + existing rows remain valid).
  2. Add action_plan LONGTEXT (agent SMART plan).

Reads central_db creds from .env (MAIN_DB_*). Idempotent: checks before altering.
Dry-run default; --live to apply.

    cd /var/www/html/leavesystem
    python3 scripts/migrate_coaching_v2.py          # dry-run
    python3 scripts/migrate_coaching_v2.py --live
"""
import os, sys, pymysql
try:
    from dotenv import load_dotenv; load_dotenv()
except ImportError:
    pass

LIVE = "--live" in sys.argv
DB = dict(
    host=os.environ["MAIN_DB_HOST"], port=int(os.environ.get("MAIN_DB_PORT", 3306)),
    user=os.environ["MAIN_DB_USER"], password=os.environ["MAIN_DB_PASSWORD"],
    database=os.environ["MAIN_DB_NAME"],
)

NEW_ENUM = "ENUM('completed','pending_followup','cancelled','pending','for_followup')"


def main():
    print(f"[{'LIVE' if LIVE else 'DRY-RUN'}] coaching v2 migration")
    print(f"  target: {DB['user']}@{DB['host']}/{DB['database']}\n")
    conn = pymysql.connect(**DB)
    try:
        with conn.cursor() as cur:
            # 1. status enum
            cur.execute("""SELECT COLUMN_TYPE FROM information_schema.COLUMNS
                           WHERE TABLE_SCHEMA=%s AND TABLE_NAME='coaching_sessions'
                             AND COLUMN_NAME='status'""", (DB["database"],))
            cur_type = cur.fetchone()[0]
            if "for_followup" in cur_type:
                print("  status enum already widened — skip")
            else:
                print(f"  MODIFY status -> {NEW_ENUM}")
                if LIVE:
                    cur.execute(f"""ALTER TABLE coaching_sessions
                        MODIFY status {NEW_ENUM} NOT NULL DEFAULT 'completed'""")

            # 2. action_plan column
            cur.execute("""SELECT COUNT(*) FROM information_schema.COLUMNS
                           WHERE TABLE_SCHEMA=%s AND TABLE_NAME='coaching_sessions'
                             AND COLUMN_NAME='action_plan'""", (DB["database"],))
            if cur.fetchone()[0]:
                print("  action_plan column already exists — skip")
            else:
                print("  ADD COLUMN action_plan LONGTEXT NULL AFTER action_items")
                if LIVE:
                    cur.execute("""ALTER TABLE coaching_sessions
                        ADD COLUMN action_plan LONGTEXT NULL AFTER action_items""")
            if LIVE:
                conn.commit()
    finally:
        conn.close()
    print("\nDone." + ("" if LIVE else "  Re-run with --live to apply."))


if __name__ == "__main__":
    main()
