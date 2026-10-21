"""
leave_credits.py
Leave filed ahead of its leave year (e.g. filed in Aug 2026 for Mar 2027).

Leave credits are charged when the leave is filed (app.adjust_leave_entitlement),
against the entitlement row for the leave date's year. If that year has no row
yet, the day can't be charged; it is saved with leave4day_requests.credit_pending = 1
and charged here once the row exists. That row may be created in the portal
(Admin > Entitlements, which writes ohrm_leave_entitlement) or directly in
OrangeHRM, so this also runs daily from
scripts/apply_pending_leave_credits.py.

Days that are rejected or cancelled while still pending are never charged, and
the reject/cancel paths skip their refund (nothing was deducted).
"""

ACTIVE_STATUSES = ('pending', 'approved', 'scheduled')


def find_entitlement(c, emp_number, leave_type_id, year):
    """The entitlement row leave for `year` is charged to: the active
    ohrm_leave_entitlement row. The portal's Admin > Entitlements page writes
    these same rows, so OrangeHRM and the portal never disagree.
    (leave4day_entitlements is retired: its leave type IDs didn't match
    ohrm_leave_type.) Returns (table, row) or (None, None)."""
    c.execute("""
        SELECT id, no_of_days, days_used FROM ohrm_leave_entitlement
        WHERE emp_number = %s AND leave_type_id = %s AND deleted = 0
          AND YEAR(from_date) = %s
        ORDER BY id LIMIT 1
    """, (emp_number, leave_type_id, year))
    row = c.fetchone()
    if row:
        return 'ohrm_leave_entitlement', row
    return None, None


def mark_credit_pending(c, leave_request_id, years):
    """Flag the days of one filing that fall in years with no entitlement row."""
    if not years:
        return
    marks = ','.join(['%s'] * len(years))
    c.execute(f"""
        UPDATE leave4day_requests SET credit_pending = 1
        WHERE leave_request_id = %s AND days_deducted > 0
          AND YEAR(leave_date) IN ({marks})
    """, [leave_request_id] + sorted(years))


def charged_items(rows):
    """(leave_date, days) for the rows whose credits were actually deducted,
    i.e. what a reject/cancel should refund."""
    return [(r['leave_date'], r['days_deducted']) for r in rows if not r.get('credit_pending')]


def apply_pending_credits(c, emp_number=None, live=True):
    """Charge flagged days whose year now has an entitlement row.

    Runs inside the caller's transaction; the caller commits. Rows are locked
    (FOR UPDATE) so the web app and the cron job can't both charge the same day.
    Returns one dict per (employee, leave type, year) group, with `applied`
    False when that year still has no row."""
    marks = ','.join(['%s'] * len(ACTIVE_STATUSES))
    sql = f"""
        SELECT r.id, r.emp_number, r.leave_type_id, r.leave_date, r.days_deducted,
               CONCAT(h.emp_firstname, ' ', h.emp_lastname) AS emp_name, lt.name AS leave_type
        FROM leave4day_requests r
        JOIN hs_hr_employee h ON h.emp_number = r.emp_number
        JOIN ohrm_leave_type lt ON lt.id = r.leave_type_id
        WHERE r.credit_pending = 1 AND r.status IN ({marks})
    """
    params = list(ACTIVE_STATUSES)
    if emp_number is not None:
        sql += " AND r.emp_number = %s"
        params.append(emp_number)
    sql += " ORDER BY r.emp_number, r.leave_type_id, r.leave_date"
    if live:
        sql += " FOR UPDATE"
    c.execute(sql, params)

    groups = {}
    for r in c.fetchall():
        key = (r['emp_number'], r['leave_type_id'], r['leave_date'].year)
        g = groups.setdefault(key, {'emp_number': r['emp_number'], 'emp_name': r['emp_name'],
                                    'leave_type_id': r['leave_type_id'], 'leave_type': r['leave_type'],
                                    'year': key[2], 'days': 0, 'ids': [],
                                    'first': r['leave_date'], 'last': r['leave_date']})
        g['days'] += float(r['days_deducted'] or 0)
        g['ids'].append(r['id'])
        g['last'] = r['leave_date']

    results = []
    for (emp, lt, year), g in groups.items():
        table, ent = find_entitlement(c, emp, lt, year)
        g['applied'] = bool(ent)
        g['table'] = table
        if ent:
            g['entitlement_id'] = ent['id']
            g['used_after'] = float(ent['days_used'] or 0) + g['days']
            g['allowed'] = float(ent['no_of_days'] or 0)
            g['over'] = g['used_after'] > g['allowed']
            if live:
                c.execute(f"UPDATE {table} SET days_used = days_used + %s WHERE id = %s",
                          (g['days'], ent['id']))
                marks_ids = ','.join(['%s'] * len(g['ids']))
                c.execute(f"UPDATE leave4day_requests SET credit_pending = 0 WHERE id IN ({marks_ids})",
                          g['ids'])
        results.append(g)
    return results
