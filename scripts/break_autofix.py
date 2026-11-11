"""
break_autofix.py — server-side run of the break auto-fix (no Apps Script needed).

Moves overnight-shift breaks that got dated to their calendar day back to the
shift they belong to (same rules as POST /webhook/break-autofix, via
modules.sheet_webhooks.fix_misdated_breaks). Without this, an overnight agent's
Break Log can show the previous night's breaks and never "reset".

DRY RUN by default: lists what it would move and changes nothing.

    venv/bin/python scripts/break_autofix.py                 # dry run, last 7 days
    venv/bin/python scripts/break_autofix.py --live          # apply
    venv/bin/python scripts/break_autofix.py --live --days 30

Cron (root, hourly at :20):
    20 * * * * cd /var/www/html/leavesystem && venv/bin/python scripts/break_autofix.py --live >> break_autofix.log 2>&1
"""
import argparse
import os
import sys
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))

from db_core import get_db_connection  # noqa: E402
from modules.sheet_webhooks import fix_misdated_breaks  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--live", action="store_true", help="apply the changes (default: dry run)")
    ap.add_argument("--days", type=int, default=7, help="look back this many days (default 7)")
    args = ap.parse_args()

    date_from = (datetime.now() - timedelta(days=args.days)).strftime("%Y-%m-%d")
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db_connection()
    try:
        rows, fixed = fix_misdated_breaks(conn, date_from, dry_run=not args.live)
        if args.live:
            conn.commit()
        else:
            conn.rollback()
    except Exception as e:
        conn.rollback()
        print(f"[{stamp}] ERROR: {e}")
        sys.exit(1)
    finally:
        conn.close()

    mode = "LIVE" if args.live else "DRY RUN"
    for r in rows:
        print(f"[{stamp}] {mode} move #{r['id']} {r['employee_id']} {r['employee_name']} "
              f"break {r['break_start']} shift_date {r['shift_date']} -> previous day "
              f"(that day: {r['shift_time'] or 'no schedule'}{' RD' if r['is_rest_day'] else ''})")
    print(f"[{stamp}] {mode}: {len(rows)} misdated break(s) since {date_from}"
          + (f", {fixed} corrected" if args.live else ""))


if __name__ == "__main__":
    main()
