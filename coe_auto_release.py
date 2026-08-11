"""
coe_auto_release.py

Safety net for the CoE self-confirm flow: any request still sitting in
'ready_for_release' past COE_AUTO_RELEASE_DAYS gets auto-marked as 'released'
so nothing lingers indefinitely if the employee forgets to confirm.

Intended to run once daily via cron, e.g.:
    30 13 * * * /usr/bin/python3 /var/www/html/leavesystem/coe_auto_release.py >> /var/log/coe_auto_release.log 2>&1

(Placed alongside your existing absence-alert cron pipeline at 12:30/12:45/1:00 PM
 -- pick a time that doesn't collide, e.g. 1:30 PM.)
"""

import os
import sys
from datetime import datetime

sys.path.insert(0, "/var/www/html/leavesystem")

from db_core import get_db_connection  # noqa: E402

COE_AUTO_RELEASE_DAYS = int(os.environ.get("COE_AUTO_RELEASE_DAYS", 3))


def main():
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, employee_name, employee_email, ready_for_release_at
                FROM coe_requests
                WHERE is_deleted = 0
                  AND status = 'ready_for_release'
                  AND ready_for_release_at IS NOT NULL
                  AND ready_for_release_at <= (NOW() - INTERVAL %s DAY)
                """,
                (COE_AUTO_RELEASE_DAYS,),
            )
            stale = cur.fetchall()

            if not stale:
                print(f"[{datetime.now()}] No stale ready_for_release requests found.")
                return

            ids = [r["id"] for r in stale]
            print(f"[{datetime.now()}] Auto-releasing {len(ids)} request(s): {ids}")

            cur.execute(
                f"""
                UPDATE coe_requests
                SET status = 'released',
                    released_at = NOW(),
                    released_via = 'auto'
                WHERE id IN ({','.join(['%s'] * len(ids))})
                """,
                tuple(ids),
            )
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    main()