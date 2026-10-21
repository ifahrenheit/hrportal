"""
Daily: grant pro-rated VL at the 1st deployment anniversary and SL at regularization, per
leave policy 3.2 / 3.3 (rules in helpers/leave_policy.py).

For every Active employee whose VL or SL starts in the current year:
  - credit date = start date if on or before the 15th, else the 1st of the next month
  - days = 1 per month from the credit month to December
  - created once the credit date is reached, only if the employee has no row of that type
    for the year yet (rows set by hand or by HR are never changed)
  - grants whose credit date is before AUTOMATION_START are listed, not created: they were due
    before this job existed, so HR confirms them first
SL start uses the deployment + 6 months estimate (no regularization date is recorded).

Every grant goes to leave4day_entitlement_log (Admin > Entitlements "Recent changes") and
leave_balance_sync.log, so they can be checked against HR's emails. Output names employees.

Dry run by default; --live writes. Run after it: scripts/apply_pending_leave_credits.py
(cron 1:30 AM) charges leave already filed against the new row.
    venv/bin/python scripts/prorated_leave_grants.py
    venv/bin/python scripts/prorated_leave_grants.py --live
"""
import argparse
import sys
from datetime import date, datetime, timedelta

from dotenv import load_dotenv

sys.path.insert(0, '/var/www/html/leavesystem')
load_dotenv('/var/www/html/leavesystem/.env')

from helpers.orangehrm_db import get_orangehrm_connection  # noqa: E402
from helpers.leave_policy import vl_start, sl_start, credit_date, prorated_days  # noqa: E402

AUTOMATION_START = date(2026, 10, 22)
ADMIN_EMP_NUMBER = 2
UPCOMING_DAYS = 31


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--live', action='store_true')
    ap.add_argument('--today', help='pretend today is YYYY-MM-DD (testing; dry run only)')
    a = ap.parse_args()
    today = datetime.strptime(a.today, '%Y-%m-%d').date() if a.today else date.today()
    if a.today and a.live:
        sys.exit('--today is for dry runs only')
    year = today.year

    conn = get_orangehrm_connection()
    c = conn.cursor()
    c.execute("SELECT id, name FROM ohrm_leave_type WHERE deleted = 0")
    types = {r['name']: r['id'] for r in c.fetchall()}
    tid = {'VL': types['Vacation Leave'], 'SL': types['Sick Leave']}
    c.execute("SELECT id FROM ohrm_user WHERE emp_number = %s AND deleted = 0 LIMIT 1", (ADMIN_EMP_NUMBER,))
    u = c.fetchone()
    created_by = u['id'] if u else None

    c.execute("""
        SELECT h.emp_number, h.employee_id, CONCAT(h.emp_firstname, ' ', h.emp_lastname) AS nm,
               g.operational_date AS deployed, h.joined_date
        FROM central_db.gsheet_employees g
        JOIN hs_hr_employee h
          ON CONVERT(h.employee_id USING utf8mb4) COLLATE utf8mb4_unicode_ci = g.employee_id COLLATE utf8mb4_unicode_ci
        WHERE g.status = 'Active'
    """)
    people = c.fetchall()
    c.execute("""SELECT emp_number, leave_type_id, no_of_days + 0 AS n FROM ohrm_leave_entitlement
                 WHERE deleted = 0 AND YEAR(from_date) = %s AND leave_type_id IN (%s, %s)""",
              (year, tid['VL'], tid['SL']))
    existing = {(r['emp_number'], r['leave_type_id']): float(r['n']) for r in c.fetchall()}

    create, overdue, upcoming, differs = [], [], [], []
    for p in people:
        dep = p['deployed'] or p['joined_date']
        if not dep:
            continue
        for kind, start in (('VL', vl_start(dep)), ('SL', sl_start(dep))):
            if start.year != year:
                continue
            cd, days = credit_date(start), prorated_days(start)
            if cd.year != year or days <= 0:
                continue
            item = {'p': p, 'kind': kind, 'start': start, 'credit': cd, 'days': days}
            have = existing.get((p['emp_number'], tid[kind]))
            if have is not None:
                if abs(have - days) > 0.001:
                    differs.append((item, have))
            elif cd > today:
                if cd <= today + timedelta(days=UPCOMING_DAYS):
                    upcoming.append(item)
            elif cd < AUTOMATION_START:
                overdue.append(item)
            else:
                create.append(item)

    def line(i):
        why = 'deployment anniversary' if i['kind'] == 'VL' else 'regularization (estimated: deployment + 6 months)'
        return f"{i['credit']}  {i['kind']} {i['days']:>2} days  {i['p']['nm']} ({i['p']['employee_id']}) - {why} {i['start']}"

    print(f"[{datetime.now():%Y-%m-%d %H:%M}] {'LIVE' if a.live else 'DRY RUN'}: pro-rated VL/SL grants, as of {today}")
    print(f"== To create now: {len(create)}")
    for i in sorted(create, key=lambda i: i['credit']):
        print('  ' + line(i))
    if overdue:
        print(f"== Due before this job started ({AUTOMATION_START}), not created; confirm with HR: {len(overdue)}")
        for i in sorted(overdue, key=lambda i: i['credit']):
            print('  ' + line(i))
    if differs:
        print(f"== Row exists but differs from the policy amount (left as is): {len(differs)}")
        for i, have in sorted(differs, key=lambda x: x[0]['credit']):
            print(f"  {line(i)} | has {have:g}")
    if upcoming:
        print(f"== Coming up in the next {UPCOMING_DAYS} days: {len(upcoming)}")
        for i in sorted(upcoming, key=lambda i: i['credit']):
            print('  ' + line(i))

    if not a.live or not create:
        conn.rollback()
        conn.close()
        if not a.live:
            print("DRY RUN: nothing written")
        return

    log = [f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}] LIVE pro-rated grants: {len(create)}"]
    try:
        for i in create:
            e, t = i['p']['emp_number'], tid[i['kind']]
            c.execute("""SELECT id FROM ohrm_leave_entitlement WHERE emp_number = %s AND leave_type_id = %s
                         AND deleted = 0 AND YEAR(from_date) = %s FOR UPDATE""", (e, t, year))
            if c.fetchone():
                raise RuntimeError(f"row appeared meanwhile for emp {e} type {t}")
            c.execute("""INSERT INTO ohrm_leave_entitlement
                             (emp_number, no_of_days, days_used, leave_type_id, from_date, to_date,
                              credited_date, note, entitlement_type, deleted, created_by_id)
                         VALUES (%s, %s, 0, %s, %s, %s, NOW(), %s, 1, 0, %s)""",
                      (e, i['days'], t, f'{year}-01-01', f'{year}-12-31',
                       f"Pro-rated {i['kind']} from {i['start']} (auto)", created_by))
            ent_id = c.lastrowid
            c.execute("""INSERT INTO leave4day_entitlement_log
                             (entitlement_id, emp_number, leave_type_id, year, action, old_days, new_days, changed_by)
                         VALUES (%s, %s, %s, %s, 'add', NULL, %s, %s)""",
                      (ent_id, e, t, year, i['days'], ADMIN_EMP_NUMBER))
            log.append(f"  #{ent_id} {line(i)}")
        conn.commit()
        log.append("  committed")
        print(f"LIVE: created {len(create)} grant(s)")
    except Exception:
        conn.rollback()
        log.append("  ROLLED BACK")
        raise
    finally:
        conn.close()
        with open('/var/www/html/leavesystem/leave_balance_sync.log', 'a') as f:
            f.write('\n'.join(log) + '\n')


if __name__ == '__main__':
    main()
