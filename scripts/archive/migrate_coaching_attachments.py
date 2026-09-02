#!/usr/bin/env python3
"""
migrate_coaching_attachments.py — attachments table for coaching sessions.

4 images per side (TL + agent), images only, soft-delete, uploader-owned.
Reads central_db creds from .env (MAIN_DB_*). Idempotent. Dry-run default.

    cd /var/www/html/leavesystem
    python3 scripts/migrate_coaching_attachments.py          # dry-run
    python3 scripts/migrate_coaching_attachments.py --live
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

DDL = """
CREATE TABLE IF NOT EXISTS coaching_attachments (
    id               INT AUTO_INCREMENT PRIMARY KEY,
    session_id       INT         NOT NULL,
    uploaded_by_side ENUM('tl','agent') NOT NULL,
    uploaded_by_emp  VARCHAR(20) NOT NULL,
    file_name        VARCHAR(255) NOT NULL,
    original_name    VARCHAR(255)     NULL,
    uploaded_at      TIMESTAMP   DEFAULT CURRENT_TIMESTAMP,
    deleted_at       TIMESTAMP        NULL DEFAULT NULL,
    KEY idx_session (session_id),
    KEY idx_side    (session_id, uploaded_by_side),
    KEY idx_deleted (deleted_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""


def main():
    print(f"[{'LIVE' if LIVE else 'DRY-RUN'}] coaching_attachments migration")
    print(f"  target: {DB['user']}@{DB['host']}/{DB['database']}\n")
    conn = pymysql.connect(**DB)
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT COUNT(*) FROM information_schema.TABLES
                           WHERE TABLE_SCHEMA=%s AND TABLE_NAME='coaching_attachments'""",
                        (DB["database"],))
            if cur.fetchone()[0]:
                print("  coaching_attachments already exists — skip")
            else:
                print("  CREATE TABLE coaching_attachments")
                if LIVE:
                    cur.execute(DDL); conn.commit()
    finally:
        conn.close()
    print("\nDone." + ("" if LIVE else "  Re-run with --live to apply."))


if __name__ == "__main__":
    main()
