"""
Year-end leave rollover: create next year's ohrm_leave_entitlement rows per leave
policy 3.1-3.3 (see .claude/agents/year-end-rollover.md).

  VL          12 if the 1st deployment anniversary (gsheet_employees.operational_date + 1 year)
              is before the new year; otherwise nothing on Jan 1 (pro-rated VL is due on the
              anniversary, listed in the report).
  SL          12 if regularized before the new year; otherwise nothing on Jan 1. There's no
              regularization column, so it's estimated as deployment + 6 months (helpers/leave_policy.REG_MONTHS)
              (fits 2026 data) unless --reg-file gives real dates (CSV: employee_id,YYYY-MM-DD).
  Carry-Over  min(unused VL of the current year, 6); 0 means no row. Run after Dec 31, or the
              figure is only a preview.
  LWOP        99 for every Active / Training employee (LWOP has no limit; the row tracks usage).

Rows that already exist for the new year are skipped. Active employees only (Training: LWOP).

Dry run by default; --live inserts in one transaction and logs each row to
leave4day_entitlement_log and leave_balance_sync.log. Output names employees: don't commit it.

    venv/bin/python scripts/year_end_rollover.py 2027
    venv/bin/python scripts/year_end_rollover.py 2027 --csv /path/plan.csv
    venv/bin/python scripts/year_end_rollover.py 2027 --skip-co --live
    venv/bin/python scripts/year_end_rollover.py 2027 --only-co --live    # after Dec 31
"""
import argparse
import csv
import sys
from collections import Counter
from datetime import date, datetime

from dotenv import load_dotenv

sys.path.insert(0, '/var/www/html/leavesystem')
load_dotenv('/var/www/html/leavesystem/.env')

from helpers.orangehrm_db import get_orangehrm_connection  # noqa: E402
from helpers.leave_policy import (FULL_VL, FULL_SL, CO_CAP, LWOP_DAYS,  # noqa: E402
                                  vl_start, sl_start, prorated_days)

ADMIN_EMP_NUMBER = 2      # changed_by in leave4day_entitlement_log / ohrm_user for created_by_id


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('year', type=int)
    ap.add_argument('--live', action='store_true')
    ap.add_argument('--skip-co', action='store_true', help="don't create Carry-Over (e.g. before Dec 31)")
    ap.add_argument('--only-co', action='store_true', help='only create Carry-Over (run after Dec 31)')
    ap.add_argument('--csv', help='write the full plan to this CSV (keep it out of the repo)')
    ap.add_argument('--reg-file', help='CSV of employee_id,regularization_date to use instead of the estimate')
    a = ap.parse_args()
    year, prev = a.year, a.year - 1
    jan1 = date(year, 1, 1)

    reg_dates = {}
    if a.reg_file:
        with open(a.reg_file) as f:
            for row in csv.reader(f):
                if len(row) >= 2 and row[1].strip()[:4].isdigit():
                    reg_dates[row[0].strip()] = datetime.strptime(row[1].strip(), '%Y-%m-%d').date()

    conn = get_orangehrm_connection()
    c = conn.cursor()
    c.execute("SELECT id, name FROM ohrm_leave_type WHERE deleted = 0")
    types = {r['name']: r['id'] for r in c.fetchall()}
    VL, SL, CO = types['Vacation Leave'], types['Sick Leave'], types['Carry-Over Leave']
    LW = next(i for n, i in types.items() if n.startswith('Leave Without Pay'))
    c.execute("SELECT id FROM ohrm_user WHERE emp_number = %s AND deleted = 0 LIMIT 1", (ADMIN_EMP_NUMBER,))
    u = c.fetchone()
    created_by = u['id'] if u else None

    c.execute("""
        SELECT h.emp_number, h.employee_id, CONCAT(h.emp_firstname, ' ', h.emp_lastname) AS nm,
               g.status, g.account, g.group_name, g.operational_date AS deployed, h.joined_date
        FROM central_db.gsheet_employees g
        JOIN hs_hr_employee h
          ON CONVERT(h.employee_id USING utf8mb4) COLLATE utf8mb4_unicode_ci = g.employee_id COLLATE utf8mb4_unicode_ci
        WHERE g.status IN ('Active', 'Training')
        ORDER BY g.status, nm
    """)
    people = c.fetchall()
    c.execute("SELECT emp_number, leave_type_id FROM ohrm_leave_entitlement WHERE deleted = 0 AND YEAR(from_date) = %s", (year,))
    existing = {(r['emp_number'], r['leave_type_id']) for r in c.fetchall()}
    c.execute("""SELECT emp_number, no_of_days + 0 AS n, days_used + 0 AS u FROM ohrm_leave_entitlement
                 WHERE deleted = 0 AND leave_type_id = %s AND YEAR(from_date) = %s""", (VL, prev))
    vl_prev = {r['emp_number']: (float(r['n']), float(r['u'])) for r in c.fetchall()}

    plan, notes = [], []
    due = []          # pro-rated grants due during the new year
    for p in people:
        e = p['emp_number']
        rows = []
        if p['status'] == 'Active':
            dep = p['deployed'] or p['joined_date']
            if not dep:
                notes.append(f"no deployment or joined date: {p['nm']} ({p['employee_id']}), LWOP only")
            else:
                if not p['deployed']:
                    notes.append(f"no deployment date, used joined date {dep}: {p['nm']} ({p['employee_id']})")
                anniv = vl_start(dep)
                if anniv < jan1:
                    rows.append((VL, 'Vacation Leave', FULL_VL, f"anniversary {anniv}"))
                elif anniv.year == year:
                    due.append((anniv, p['nm'], p['employee_id'], 'VL', prorated_days(anniv), 'deployment anniversary'))
                reg = sl_start(dep, reg_dates.get(p['employee_id']))
                src = 'given' if p['employee_id'] in reg_dates else 'estimated'
                if reg < jan1:
                    rows.append((SL, 'Sick Leave', FULL_SL, f"regularized {reg} ({src})"))
                elif reg.year == year:
                    due.append((reg, p['nm'], p['employee_id'], 'SL', prorated_days(reg), f'regularization ({src})'))
            if not a.skip_co and e in vl_prev:
                n, used = vl_prev[e]
                unused = max(0.0, n - used)
                co = min(unused, CO_CAP)
                if co > 0:
                    rows.append((CO, 'Carry-Over Leave', round(co, 4), f"unused {prev} VL {unused:g} of {n:g}"))
        rows.append((LW, 'Leave Without Pay (LWOP)', LWOP_DAYS, 'everyone'))
        for t, tname, days, why in rows:
            plan.append({'emp_number': e, 'employee_id': p['employee_id'], 'name': p['nm'], 'status': p['status'],
                         'group': p['group_name'], 'type_id': t, 'type': tname, 'days': days, 'why': why,
                         'skip': (e, t) in existing})

    if a.only_co:
        plan = [r for r in plan if r['type_id'] == CO]
    todo = [r for r in plan if not r['skip']]
    print(f"== {year} rollover {'(LIVE)' if a.live else '(dry run)'}: {len(people)} Active/Training employees")
    summary = Counter((r['type'], r['days']) for r in todo)
    for (t, d), k in sorted(summary.items()):
        print(f"  {t:26} {d:>5g} days  x {k}")
    print(f"  rows to create: {len(todo)}   already exist (skipped): {len(plan) - len(todo)}")
    if not a.skip_co:
        print(f"  Carry-Over uses {prev} VL as of today; re-run after Dec 31 for final figures, or use --skip-co.")
    if due:
        print(f"== Pro-rated grants due during {year} (not created now): {len(due)}")
        for d_, nm, eid, t, n, why in sorted(due):
            print(f"  {d_}  {t} {n:>2} days  {nm} ({eid}) - {why}")
    if notes:
        print("== Check with HR")
        for n in notes:
            print('  ' + n)
    if a.csv:
        with open(a.csv, 'w', newline='') as f:
            w = csv.DictWriter(f, fieldnames=list(plan[0].keys()) if plan else ['name'])
            w.writeheader()
            w.writerows(plan)
        print(f"plan written to {a.csv}")

    if not a.live:
        conn.rollback()
        conn.close()
        print("DRY RUN: nothing written")
        return

    log = [f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}] LIVE year-end rollover {year}: {len(todo)} rows"]
    try:
        for r in todo:
            c.execute("""SELECT id FROM ohrm_leave_entitlement WHERE emp_number = %s AND leave_type_id = %s
                         AND deleted = 0 AND YEAR(from_date) = %s FOR UPDATE""", (r['emp_number'], r['type_id'], year))
            if c.fetchone():
                raise RuntimeError(f"row appeared meanwhile for emp {r['emp_number']} type {r['type_id']}")
            c.execute("""INSERT INTO ohrm_leave_entitlement
                             (emp_number, no_of_days, days_used, leave_type_id, from_date, to_date,
                              credited_date, note, entitlement_type, deleted, created_by_id)
                         VALUES (%s, %s, 0, %s, %s, %s, NOW(), %s, 1, 0, %s)""",
                      (r['emp_number'], r['days'], r['type_id'], f'{year}-01-01', f'{year}-12-31',
                       f'Year-end rollover {year}', created_by))
            ent_id = c.lastrowid
            c.execute("""INSERT INTO leave4day_entitlement_log
                             (entitlement_id, emp_number, leave_type_id, year, action, old_days, new_days, changed_by)
                         VALUES (%s, %s, %s, %s, 'add', NULL, %s, %s)""",
                      (ent_id, r['emp_number'], r['type_id'], year, r['days'], ADMIN_EMP_NUMBER))
            log.append(f"  #{ent_id} {r['name']} {r['type']} {year}: {r['days']:g} ({r['why']})")
        conn.commit()
        log.append("  committed")
        print(f"LIVE: committed {len(todo)} rows. Next: venv/bin/python scripts/apply_pending_leave_credits.py")
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
