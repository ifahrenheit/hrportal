"""
One-time migration (2026-10-21): make ohrm_leave_entitlement the only balance table.

1. leave4day_entitlements ("Portal DB") rows move into ohrm_leave_entitlement.
   Their leave_type_id points at leave4day_leave_types, whose IDs differ from
   ohrm_leave_type (only LWOP = 3 matches), so types are mapped by name.
   - OrangeHRM already has the row: add the portal row's days_used to it
     (those days were charged to the portal row only). no_of_days is left
     alone and differences are reported.
   - No OrangeHRM row: insert one with the portal row's days and used.
   The portal table itself is left untouched; nothing reads it any more.

2. Leave filed for a future year before commit 400d30a (2026-10-14) was charged
   to the *current* year's row at filing. Those days are now credit_pending
   (helpers/leave_credits.py) and will be charged to their own year, so the
   current-year charge is given back here to avoid charging them twice.

Dry run by default; pass --live to write.
    venv/bin/python scripts/manual_fixes/retire_portal_entitlements.py
"""
import sys

from dotenv import load_dotenv

sys.path.insert(0, '/var/www/html/leavesystem')
load_dotenv('/var/www/html/leavesystem/.env')

from helpers.orangehrm_db import get_orangehrm_connection  # noqa: E402

PER_YEAR_CHARGING_SINCE = '2026-10-14 02:41:29'   # commit 400d30a


def main():
    live = '--live' in sys.argv
    conn = get_orangehrm_connection()
    c = conn.cursor()
    try:
        print('== 1. Portal DB rows -> OrangeHRM')
        c.execute("""
            SELECT e.*, p.name AS portal_type, ot.id AS ohrm_type_id,
                   CONCAT(h.emp_firstname, ' ', h.emp_lastname) AS emp_name
            FROM leave4day_entitlements e
            JOIN hs_hr_employee h ON h.emp_number = e.emp_number
            JOIN leave4day_leave_types p ON p.id = e.leave_type_id
            LEFT JOIN ohrm_leave_type ot ON LOWER(ot.name) = LOWER(p.name) AND ot.deleted = 0
            ORDER BY e.id
        """)
        for e in c.fetchall():
            label = f"#{e['id']} {e['emp_name']} {e['portal_type']} {e['from_date'].year}"
            if not e['ohrm_type_id']:
                print(f"  SKIP {label}: no OrangeHRM type named {e['portal_type']!r}")
                continue
            c.execute("""SELECT id, no_of_days, days_used FROM ohrm_leave_entitlement
                         WHERE emp_number = %s AND leave_type_id = %s AND deleted = 0
                           AND YEAR(from_date) = %s FOR UPDATE""",
                      (e['emp_number'], e['ohrm_type_id'], e['from_date'].year))
            o = c.fetchone()
            used = float(e['days_used'] or 0)
            if o:
                note = ''
                if float(o['no_of_days']) != float(e['no_of_days']):
                    note = f" (days differ: OrangeHRM {float(o['no_of_days']):g}, portal {float(e['no_of_days']):g}; kept OrangeHRM)"
                if used:
                    print(f"  ADD USED {label}: OrangeHRM #{o['id']} used {float(o['days_used']):g} -> {float(o['days_used']) + used:g}{note}")
                    if live:
                        c.execute("UPDATE ohrm_leave_entitlement SET days_used = days_used + %s WHERE id = %s",
                                  (used, o['id']))
                else:
                    print(f"  OK {label}: already in OrangeHRM #{o['id']}, nothing used{note}")
            else:
                print(f"  INSERT {label}: {float(e['no_of_days']):g} days, {used:g} used, OrangeHRM type {e['ohrm_type_id']}")
                if live:
                    c.execute("""
                        INSERT INTO ohrm_leave_entitlement
                            (emp_number, no_of_days, days_used, leave_type_id, from_date, to_date,
                             credited_date, note, entitlement_type, deleted, created_by_id)
                        VALUES (%s, %s, %s, %s, %s, %s, NOW(), 'Moved from portal entitlements', 1, 0, NULL)
                    """, (e['emp_number'], e['no_of_days'], used, e['ohrm_type_id'],
                          e['from_date'], e['to_date']))

        print('== 2. Future-year leave charged to the filing year before per-year charging')
        c.execute("""
            SELECT r.emp_number, r.leave_type_id, YEAR(r.filed_at) AS charged_year,
                   YEAR(r.leave_date) AS leave_year, SUM(r.days_deducted) AS days, COUNT(*) AS n,
                   CONCAT(h.emp_firstname, ' ', h.emp_lastname) AS emp_name, lt.name AS leave_type
            FROM leave4day_requests r
            JOIN hs_hr_employee h ON h.emp_number = r.emp_number
            JOIN ohrm_leave_type lt ON lt.id = r.leave_type_id
            WHERE r.credit_pending = 1 AND r.status IN ('pending', 'approved', 'scheduled')
              AND r.filed_at < %s AND YEAR(r.leave_date) > YEAR(r.filed_at)
            GROUP BY 1, 2, 3, 4, 7, 8
        """, (PER_YEAR_CHARGING_SINCE,))
        for g in c.fetchall():
            c.execute("""SELECT id, days_used FROM ohrm_leave_entitlement
                         WHERE emp_number = %s AND leave_type_id = %s AND deleted = 0
                           AND YEAR(from_date) = %s FOR UPDATE""",
                      (g['emp_number'], g['leave_type_id'], g['charged_year']))
            o = c.fetchone()
            days = float(g['days'])
            label = f"{g['emp_name']} {g['leave_type']}: {g['n']} day(s) of {g['leave_year']} leave, {days:g} days"
            if not o:
                print(f"  SKIP {label}: no {g['charged_year']} row to give back to")
                continue
            print(f"  GIVE BACK {label} -> {g['charged_year']} OrangeHRM #{o['id']} "
                  f"used {float(o['days_used']):g} -> {max(0, float(o['days_used']) - days):g}")
            if live:
                c.execute("UPDATE ohrm_leave_entitlement SET days_used = GREATEST(0, days_used - %s) WHERE id = %s",
                          (days, o['id']))

        if live:
            conn.commit()
            print('LIVE: committed')
        else:
            conn.rollback()
            print('DRY RUN: nothing written')
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == '__main__':
    main()
