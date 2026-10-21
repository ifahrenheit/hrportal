"""
Charge leave that was filed before its leave year had a balance.

Example: leave filed in Aug 2026 for Mar 2027, before 2027 entitlements were
loaded. Those days are saved with leave4day_requests.credit_pending = 1. Once
the 2027 entitlement row exists (portal or OrangeHRM), this deducts them from it.
See helpers/leave_credits.py.

Dry run by default; pass --live to write.
    venv/bin/python scripts/apply_pending_leave_credits.py
    venv/bin/python scripts/apply_pending_leave_credits.py --live
"""
import sys
from datetime import datetime

from dotenv import load_dotenv

sys.path.insert(0, '/var/www/html/leavesystem')
load_dotenv('/var/www/html/leavesystem/.env')

from helpers.orangehrm_db import get_orangehrm_connection  # noqa: E402
from helpers.leave_credits import apply_pending_credits     # noqa: E402


def main():
    live = '--live' in sys.argv
    stamp = datetime.now().strftime('%Y-%m-%d %H:%M')
    conn = get_orangehrm_connection()
    try:
        with conn.cursor() as c:
            results = apply_pending_credits(c, live=live)
        if live:
            conn.commit()
        else:
            conn.rollback()
    finally:
        conn.close()

    mode = 'LIVE' if live else 'DRY RUN'
    applied = [g for g in results if g['applied']]
    waiting = [g for g in results if not g['applied']]
    print(f"[{stamp}] {mode}: {len(applied)} group(s) charged, {len(waiting)} still waiting for a balance")
    for g in applied:
        verb = 'charged' if live else 'would charge'
        flag = '  OVER ALLOWANCE' if g['over'] else ''
        print(f"  {verb} {g['days']:g} day(s) {g['leave_type']} {g['year']} -> {g['emp_name']} "
              f"(emp {g['emp_number']}, {g['first']:%b %d}-{g['last']:%b %d}) on {g['table']} #{g['entitlement_id']}: "
              f"used {g['used_after']:g} of {g['allowed']:g}{flag}")
    for g in waiting:
        print(f"  waiting: {g['days']:g} day(s) {g['leave_type']} {g['year']} for {g['emp_name']} "
              f"(emp {g['emp_number']}), no {g['year']} balance yet")


if __name__ == '__main__':
    main()
