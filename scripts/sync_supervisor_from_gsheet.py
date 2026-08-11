#!/usr/bin/env python3
"""
sync_supervisor_from_gsheet.py

Daily cron job: syncs central_db.Employees.supervisor_id from
central_db.gsheet_employees.approver (the source of truth for
who an employee reports to).

Join key: Employees.EmployeeID <-> gsheet_employees.employee_id
          (COLLATE utf8mb4_unicode_ci to avoid collation mismatch errors)

Behavior:
  - SELECT-before-mutate: builds a diff list first, only writes rows
    that actually changed.
  - Dry-run by default. Pass --live to actually write changes.
  - Every change (and every skip) is logged to both stdout (print,
    flush=True so it shows in journalctl if ever run as a service)
    and to a log file for historical record.
  - Employees present in gsheet_employees but with no matching ACTIVE
    EmployeeID in central_db.Employees are reported as skipped, never
    silently dropped. (Deactivated/superseded rows, e.g. old IDs from
    rehires, are excluded via IsActive = 1 on both sides of the join.)
  - Any gsheet.approver value that doesn't look like an email (broken
    sheet formulas like '#N/A', '#REF!', blanks, etc.) is skipped and
    reported separately — never written to supervisor_id.

Suggested cron (runs daily at 1:15 PM, after leave_to_gsheet.py at
12:45 PM so gsheet data for the day is already fresh):

    15 13 * * * /var/www/html/leavesystem/venv/bin/python \
        /var/www/html/leavesystem/scripts/sync_supervisor_from_gsheet.py --live \
        >> /var/www/html/leavesystem/logs/supervisor_sync.log 2>&1

Recommended first runs: WITHOUT --live (dry-run) to eyeball the diff
before it goes live unattended.
"""

import sys
import os
import re
import argparse
import logging
from datetime import datetime

# Minimal sanity check for "does this look like an email" — not full RFC
# validation, just enough to catch broken Google Sheets formula results
# like '#N/A', '#REF!', blank strings, etc. before they get written to
# supervisor_id.
EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')

import pymysql
import pymysql.cursors
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SCRIPT_DIR, '..', '.env')  # adjust if script lives elsewhere
load_dotenv(ENV_PATH)

LOG_DIR = os.path.join(SCRIPT_DIR, '..', 'logs')
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, 'supervisor_sync.log')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
    ]
)
logger = logging.getLogger('supervisor_sync')


def log(msg, level='info'):
    """Log to both file (via logging) and stdout (via print, flush=True) —
    print(flush=True) is required because logging.warning() from cron/blueprint
    contexts on this system doesn't reliably surface elsewhere."""
    getattr(logger, level)(msg)
    print(msg, flush=True)


def get_connection():
    """Connect to central_db using MAIN_DB_* credentials from .env."""
    return pymysql.connect(
        host=os.environ['MAIN_DB_HOST'],
        user=os.environ['MAIN_DB_USER'],
        password=os.environ['MAIN_DB_PASSWORD'],
        database=os.environ.get('MAIN_DB_NAME', 'central_db'),
        cursorclass=pymysql.cursors.DictCursor,
        charset='utf8mb4',
    )


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def fetch_diff(conn):
    """
    SELECT-before-mutate: pull every Employees row alongside its gsheet
    approver value, and split into:
      - changes:  supervisor_id differs from gsheet approver (update needed)
      - unmatched: gsheet has no matching EmployeeID in Employees (skip + report)

    Rows where gsheet.approver IS NULL are left alone (we never overwrite
    a known supervisor_id with NULL — only sync forward when gsheet has
    an actual value).
    """
    sql = """
        SELECT
            e.EmployeeID,
            e.FirstName,
            e.LastName,
            e.supervisor_id AS current_supervisor_id,
            g.approver      AS gsheet_approver,
            g.tl            AS gsheet_tl
        FROM central_db.Employees e
        LEFT JOIN central_db.gsheet_employees g
            ON e.EmployeeID COLLATE utf8mb4_unicode_ci = g.employee_id COLLATE utf8mb4_unicode_ci
        WHERE g.employee_id IS NOT NULL
          AND e.IsActive = 1
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall()

    changes = []
    invalid = []
    unchanged = 0

    for row in rows:
        approver = (row['gsheet_approver'] or '').strip() or None
        current = (row['current_supervisor_id'] or '').strip() or None

        if approver is None:
            # gsheet doesn't have an approver for this employee yet — skip,
            # don't blank out an existing supervisor_id.
            continue

        if not EMAIL_RE.match(approver):
            # Broken sheet formula result (#N/A, #REF!, etc.) or otherwise
            # garbage value — never write this, just report it so it can
            # be fixed at the source in Google Sheets.
            invalid.append({
                'EmployeeID': row['EmployeeID'],
                'name': f"{row['FirstName']} {row['LastName']}",
                'bad_value': approver,
            })
            continue

        if approver != current:
            changes.append({
                'EmployeeID': row['EmployeeID'],
                'name': f"{row['FirstName']} {row['LastName']}",
                'old_value': current,
                'new_value': approver,
                'gsheet_tl': row['gsheet_tl'],
            })
        else:
            unchanged += 1

    return changes, unchanged, invalid


def fetch_unmatched(conn):
    """
    gsheet_employees rows with an approver set, but no matching EmployeeID
    in Employees at all (e.g. brand-new hires not yet provisioned, or
    dual-ID rehire artifacts like the Joanna Potot case). Report only,
    never write.
    """
    sql = """
        SELECT g.employee_id, g.schedule_name, g.approver
        FROM central_db.gsheet_employees g
        LEFT JOIN central_db.Employees e
            ON g.employee_id COLLATE utf8mb4_unicode_ci = e.EmployeeID COLLATE utf8mb4_unicode_ci
            AND e.IsActive = 1
        WHERE g.approver IS NOT NULL
          AND g.approver != ''
          AND e.EmployeeID IS NULL
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchall()


def apply_changes(conn, changes):
    """Apply each diffed row as an individual UPDATE. Commits once at the end."""
    with conn.cursor() as cur:
        for c in changes:
            cur.execute(
                "UPDATE central_db.Employees SET supervisor_id = %s WHERE EmployeeID = %s",
                (c['new_value'], c['EmployeeID'])
            )
    conn.commit()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description='Sync Employees.supervisor_id from gsheet_employees.approver')
    parser.add_argument('--live', action='store_true', help='Actually write changes. Without this flag, runs as dry-run only.')
    args = parser.parse_args()

    mode = 'LIVE' if args.live else 'DRY-RUN'
    log(f"===== supervisor_sync started ({mode}) at {datetime.now().isoformat()} =====")

    conn = None
    try:
        conn = get_connection()

        changes, unchanged_count, invalid = fetch_diff(conn)
        unmatched = fetch_unmatched(conn)

        log(f"Checked employees: {len(changes) + unchanged_count + len(invalid)} matched EmployeeID rows")
        log(f"Unchanged: {unchanged_count}")
        log(f"Changes needed: {len(changes)}")

        for c in changes:
            log(f"  [{c['EmployeeID']}] {c['name']}: '{c['old_value']}' -> '{c['new_value']}' (tl={c['gsheet_tl']})")

        if invalid:
            log(f"Invalid approver values (not an email, skipped — fix at source in Google Sheets): {len(invalid)}")
            for i in invalid:
                log(f"  INVALID [{i['EmployeeID']}] {i['name']}: approver='{i['bad_value']}'")

        if unmatched:
            log(f"Unmatched gsheet rows (no active EmployeeID in Employees, skipped): {len(unmatched)}")
            for u in unmatched:
                log(f"  SKIP [{u['employee_id']}] {u['schedule_name']} -> approver={u['approver']}")

        if args.live:
            if changes:
                apply_changes(conn, changes)
                log(f"Applied {len(changes)} update(s) to Employees.supervisor_id.")
            else:
                log("No changes to apply.")
        else:
            log("Dry-run mode — no changes written. Re-run with --live to apply.")

    except Exception as e:
        log(f"ERROR: {e}", level='error')
        sys.exit(1)
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass

    log(f"===== supervisor_sync finished ({mode}) =====\n")


if __name__ == '__main__':
    main()