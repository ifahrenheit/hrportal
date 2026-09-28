#!/usr/bin/env python3
"""
migrate_coaching_draft.py — add 'draft' to the coaching_sessions status ENUM.

Lets a TL create a coaching session ahead of time (before the agent is on
shift) without it counting as 'pending' — draft rows are excluded from the
create/remind notification emails and the daily coaching_reminders.py cron,
since both only ever query the existing pending-status values. Old values
are kept so the legacy PHP coaching page + existing rows remain valid
(same approach as migrate_coaching_v2.py).

Reads central_db creds from .env (MAIN_DB_*). Idempotent: checks before
altering. Dry-run default; --live to apply.

    cd /var/www/html/leavesystem
    python3 scripts/migrate_coaching_draft.py          # dry-run
    python3 scripts/migrate_coaching_draft.py --live
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

NEW_ENUM = "ENUM('completed','pending_followup','cancelled','pending','for_followup','draft')"


def main():
    print(f"[{'LIVE' if LIVE else 'DRY-RUN'}] coaching draft-status migration")
    print(f"  target: {DB['user']}@{DB['host']}/{DB['database']}\n")
    conn = pymysql.connect(**DB)
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT COLUMN_TYPE FROM information_schema.COLUMNS
                           WHERE TABLE_SCHEMA=%s AND TABLE_NAME='coaching_sessions'
                             AND COLUMN_NAME='status'""", (DB["database"],))
            cur_type = cur.fetchone()[0]
            if "draft" in cur_type:
                print("  status enum already has 'draft' — skip")
            else:
                print(f"  MODIFY status -> {NEW_ENUM}")
                if LIVE:
                    cur.execute(f"""ALTER TABLE coaching_sessions
                        MODIFY status {NEW_ENUM} NOT NULL DEFAULT 'completed'""")
                    conn.commit()
    finally:
        conn.close()
    print("\nDone." + ("" if LIVE else "  Re-run with --live to apply."))


if __name__ == "__main__":
    main()
