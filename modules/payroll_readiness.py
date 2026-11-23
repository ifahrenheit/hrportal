"""
Analytics > Payroll Readiness -- /admin/payroll-readiness

One page to open before payroll is run for a cutoff (8th-22nd, 23rd-7th): what
in that payroll period is still open and who it is waiting on.

  1. File requests still Pending: OT, FTS, CWS, RDW, Magic CWS
  2. Missing punches with no FTS filed (the FTS engine's fts_pathb_incidents,
     category 'fts', still pending review and not matched to a filed FTS)
  3. Leave not settled: Pending approval, SL waiting for HR verification, and
     leave filed before its year had a balance (credit_pending)

Plus a count of requests still Pending from EARLIER periods (late approvals
that will need a payroll adjustment). Read-only. CSV export of everything open.

It replaces the cutoff-day emails to payroll@ (cohere_dashboard/send_*_email.php)
whose cron lines never ran from May 2025 and were removed 2026-11-23.

Access: admins, and sub-admins with can_reports or can_ot_hours.

app.py runs as __main__, so this module must not `from app import ...`.
init_payroll_readiness() is called from app.py and hands over the helpers.
"""
import csv
import io
from datetime import date, datetime, timedelta

from flask import Blueprint, Response, flash, redirect, render_template, request, session, url_for

from helpers.payroll_period import (get_default_payroll_period, get_next_period,
                                    get_payroll_period_for_date, get_previous_period)

payroll_readiness_bp = Blueprint('payroll_readiness', __name__)

_h = {}

ACCESS_PERMS = ('can_reports', 'can_ot_hours')
LEAVE_CLOSED = ('cancelled', 'rejected', 'deleted')
C = 'COLLATE utf8mb4_unicode_ci'


def init_payroll_readiness(**helpers):
    """helpers: get_db (orangehrm2), get_central_db (central_db),
    get_approver_names (employee_ids -> {id: {'name', 'email', 'via_tl'}})."""
    _h.update(helpers)


def can_view():
    if session.get('is_admin'):
        return True
    perms = session.get('permissions', {}) or {}
    return any(perms.get(p) for p in ACCESS_PERMS)


# ──────────────────────────────────────────────────────────────────
# Period
# ──────────────────────────────────────────────────────────────────
def resolve_period(start_arg, today=None):
    """?start=YYYY-MM-DD picks the period containing that date; default is the
    most recently closed period (the one payroll is being run for)."""
    today = today or date.today()
    try:
        d = datetime.strptime(start_arg or '', '%Y-%m-%d').date()
        return get_payroll_period_for_date(d)
    except ValueError:
        return get_default_payroll_period(today)


def period_state(start, end, today=None):
    today = today or date.today()
    if today > end:
        days = (today - end).days
        return 'closed', f"Closed {end:%b %d} ({days} day{'s' if days != 1 else ''} ago)"
    if today < start:
        return 'upcoming', f"Starts {start:%b %d}"
    days = (end - today).days
    return 'open', ('Cutoff is today' if days == 0
                    else f"In progress, cutoff {end:%b %d} (in {days} day{'s' if days != 1 else ''})")


# ──────────────────────────────────────────────────────────────────
# Queries
# ──────────────────────────────────────────────────────────────────
def _fmt_time(v):
    if v is None or v == '':
        return ''
    if hasattr(v, 'strftime'):
        return v.strftime('%H:%M')
    if isinstance(v, timedelta):     # MySQL TIME comes back as timedelta, possibly >= 24h
        mins = int(v.total_seconds()) // 60
        return f'{(mins // 60) % 24:02d}:{mins % 60:02d}'
    s = str(v)
    if ':' in s:
        h, m = s.split(':')[:2]
        return f'{int(h) % 24:02d}:{m}'
    return s


def _range(a, b):
    a, b = _fmt_time(a), _fmt_time(b)
    return f'{a}-{b}' if a or b else ''


def _q(conn, sql, params):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def pending_requests(cdb, start, end):
    """Pending OT/FTS/CWS/RDW/Magic CWS with a work date in the period."""
    rows = []
    for r in _q(cdb, """
            SELECT id, employee_id, ot_date AS d, ot_type, start_time, end_time, `timestamp` AS filed_at
            FROM ot_requests
            WHERE status = 'Pending' AND deleted_at IS NULL AND ot_date BETWEEN %s AND %s""", (start, end)):
        rows.append({'type': 'OT', 'employee_id': r['employee_id'], 'date': r['d'],
                     'details': ' '.join(x for x in (r['ot_type'] or '', _range(r['start_time'], r['end_time'])) if x),
                     'filed_at': r['filed_at']})
    for r in _q(cdb, """
            SELECT id, employeeID AS employee_id, employee_name, fts_date AS d, fts_time, fts_type, created_at
            FROM fts_requests
            WHERE status = 'Pending' AND deleted_at IS NULL AND fts_date BETWEEN %s AND %s""", (start, end)):
        rows.append({'type': 'FTS', 'employee_id': r['employee_id'], 'date': r['d'],
                     'details': f"{r['fts_type'] or ''} {_fmt_time(r['fts_time'])}".strip(),
                     'filed_at': r['created_at'], 'name': r['employee_name']})
    for r in _q(cdb, """
            SELECT id, employee_id, original_date, original_time, new_date, new_time, created_at
            FROM cws_requests
            WHERE status = 'Pending' AND deleted_at IS NULL
              AND (original_date BETWEEN %s AND %s OR new_date BETWEEN %s AND %s)""",
                (start, end, start, end)):
        rows.append({'type': 'CWS', 'employee_id': r['employee_id'], 'date': r['original_date'],
                     'details': f"{r['original_date']:%b %d} {r['original_time'] or ''} → "
                                f"{r['new_date']:%b %d} {r['new_time'] or ''}".replace('  ', ' ')
                                if r['original_date'] and r['new_date'] else '',
                     'filed_at': r['created_at']})
    for r in _q(cdb, """
            SELECT id, employee_id, rd_date AS d, start_time, end_time, work_category, created_at
            FROM rd_requests
            WHERE status = 'Pending' AND deleted_at IS NULL AND rd_date BETWEEN %s AND %s""", (start, end)):
        rows.append({'type': 'RDW', 'employee_id': r['employee_id'], 'date': r['d'],
                     'details': ' '.join(x for x in (_range(r['start_time'], r['end_time']),
                                                     r['work_category'] or '') if x),
                     'filed_at': r['created_at']})
    for r in _q(cdb, """
            SELECT id, employee_id, employee_name, original_date, original_shift, new_date, new_shift, filed_at
            FROM magic_cws_requests
            WHERE LOWER(status) = 'pending'
              AND (original_date BETWEEN %s AND %s OR new_date BETWEEN %s AND %s)""",
                (start, end, start, end)):
        rows.append({'type': 'Magic CWS', 'employee_id': r['employee_id'], 'date': r['original_date'],
                     'details': f"{r['original_shift'] or ''} → {r['new_shift'] or ''}",
                     'filed_at': r['filed_at'], 'name': r['employee_name']})
    return rows


def earlier_pending_count(cdb, start):
    """Requests still Pending whose work date is BEFORE this period."""
    row = _q(cdb, """
        SELECT (SELECT COUNT(*) FROM ot_requests  WHERE status='Pending' AND deleted_at IS NULL AND ot_date  < %s)
             + (SELECT COUNT(*) FROM fts_requests WHERE status='Pending' AND deleted_at IS NULL AND fts_date < %s)
             + (SELECT COUNT(*) FROM cws_requests WHERE status='Pending' AND deleted_at IS NULL AND original_date < %s)
             + (SELECT COUNT(*) FROM rd_requests  WHERE status='Pending' AND deleted_at IS NULL AND rd_date  < %s) AS n
    """, (start, start, start, start))
    return int(row[0]['n'] or 0) if row else 0


NEEDS = {'missing_in': ('IN',), 'missing_out': ('OUT',), 'missing_both': ('IN', 'OUT')}


def _filed_fts(cdb, start, end):
    """{(employee_id, 'IN'|'OUT'): {dates}} of FTS requests that are Pending or
    Approved, from the period start to the day after it ends (an overnight
    shift's OUT is filed on the next day)."""
    filed = {}
    for r in _q(cdb, """
            SELECT employeeID AS employee_id, fts_date, UPPER(fts_type) AS fts_type
            FROM fts_requests
            WHERE status IN ('Pending', 'Approved') AND deleted_at IS NULL
              AND fts_date BETWEEN %s AND %s""", (start, end + timedelta(days=1))):
        filed.setdefault((str(r['employee_id']), r['fts_type']), set()).add(r['fts_date'])
    return filed


def _covered(filed, employee_id, shift_date, need):
    """Same rule as the FTS engine: IN is filed on the shift date; OUT on the
    shift date or the next day (shifts that cross midnight)."""
    dates = filed.get((str(employee_id), need), set())
    if need == 'IN':
        return shift_date in dates
    return shift_date in dates or (shift_date + timedelta(days=1)) in dates


def missing_punches(cdb, start, end):
    """Shifts the FTS engine flagged with a missing IN or OUT and no FTS filed.

    The engine fills fts_filed on its next daily run, so an FTS filed since then
    would still look unfiled here; those are matched against fts_requests and
    left out (a pending one is already listed under File requests)."""
    filed = _filed_fts(cdb, start, end)
    rows = []
    for r in _q(cdb, """
            SELECT employee_id, name, tl, schedule_date, shift_time, flag_type, candidate_note, created_at
            FROM fts_pathb_incidents
            WHERE category = 'fts' AND (fts_filed IS NULL OR fts_filed = '')
              AND review_status = 'pending' AND schedule_date BETWEEN %s AND %s""", (start, end)):
        needs = NEEDS.get(r['flag_type'], ())
        still = [n for n in needs if not _covered(filed, r['employee_id'], r['schedule_date'], n)]
        if needs and not still:
            continue
        missing = ' and '.join(still) if still else (r['flag_type'] or '')
        rows.append({'type': 'Missing punch', 'employee_id': r['employee_id'], 'date': r['schedule_date'],
                     'details': f"No {missing} punch, shift {r['shift_time'] or '?'}",
                     'filed_at': r['created_at'], 'name': r['name'], 'tl': r['tl'],
                     'waiting_on': 'Employee: file an FTS'})
    return rows


def leave_items(db, start, end):
    """Leave in the period that is not settled for payroll yet."""
    closed = ','.join(['%s'] * len(LEAVE_CLOSED))
    rows = []
    for r in _q(db, f"""
            SELECT lr.employee_id, lr.leave_date, LOWER(lr.status) AS status, lr.sl_hr_stage,
                   lr.credit_pending, lr.days_deducted, lr.filed_at, lt.name AS leave_type,
                   CONCAT(e.emp_firstname, ' ', e.emp_lastname) AS name
            FROM leave4day_requests lr
            JOIN ohrm_leave_type lt ON lt.id = lr.leave_type_id
            LEFT JOIN hs_hr_employee e ON e.emp_number = lr.emp_number
            WHERE lr.deleted_at IS NULL
              AND lr.leave_date BETWEEN %s AND %s
              AND LOWER(lr.status) NOT IN ({closed})
              AND (LOWER(lr.status) = 'pending' OR lr.sl_hr_stage = 'pending_hr' OR lr.credit_pending = 1)
            """, (start, end) + LEAVE_CLOSED):
        if r['status'] == 'pending':
            kind, waiting = 'Leave pending approval', None          # approver filled in later
        elif r['sl_hr_stage'] == 'pending_hr':
            kind, waiting = 'SL waiting for HR', 'HR: SL verification'
        else:
            kind, waiting = 'Leave not charged yet', 'Leave balance for that year (credit pending)'
        days = r['days_deducted']
        rows.append({'type': kind, 'employee_id': r['employee_id'], 'date': r['leave_date'],
                     'details': f"{r['leave_type']}" + (f", {float(days):g} day" if days else ''),
                     'filed_at': r['filed_at'], 'name': r['name'], 'waiting_on': waiting})
    return rows


def _attach_people(cdb, rows, today):
    """Fill name/TL from the employee sheet, the approver for anything that
    still needs approving, and how many days each item has been open."""
    ids = sorted({str(r['employee_id']) for r in rows if r.get('employee_id')})
    people = {}
    if ids:
        ph = ','.join(['%s'] * len(ids))
        for p in _q(cdb, f"""
                SELECT employee_id, schedule_name, tl FROM gsheet_employees
                WHERE employee_id {C} IN ({ph})""", ids):
            people[p['employee_id']] = p
    need_approver = [r['employee_id'] for r in rows if not r.get('waiting_on')]
    approvers = _h['get_approver_names'](need_approver) if need_approver and 'get_approver_names' in _h else {}
    for r in rows:
        p = people.get(str(r.get('employee_id')), {})
        r['name'] = p.get('schedule_name') or r.get('name') or str(r.get('employee_id') or '?')
        r['tl'] = p.get('tl') or r.get('tl') or ''
        if not r.get('waiting_on'):
            a = approvers.get(str(r['employee_id'])) or {}
            name = a.get('name') or a.get('email') or 'No approver found'
            r['waiting_on'] = name + (' (TL\'s SOM)' if a.get('via_tl') else '')
        filed = r.get('filed_at')
        filed_d = filed.date() if isinstance(filed, datetime) else filed
        r['age_days'] = (today - filed_d).days if isinstance(filed_d, date) else None
    rows.sort(key=lambda r: (r['date'] or date.min, r['name']))
    return rows


def collect(start, end, today=None):
    today = today or date.today()
    cdb = _h['get_central_db']()
    try:
        requests_ = _attach_people(cdb, pending_requests(cdb, start, end), today)
        punches = _attach_people(cdb, missing_punches(cdb, start, end), today)
        earlier = earlier_pending_count(cdb, start)
        db = _h['get_db']()
        try:
            leave_raw = leave_items(db, start, end)
        finally:
            db.close()
        leave = _attach_people(cdb, leave_raw, today)
    finally:
        cdb.close()
    sections = [
        {'key': 'requests', 'title': 'File requests still pending', 'icon': 'file-clock', 'rows': requests_,
         'empty': 'No OT, FTS, CWS, RDW or Magic CWS requests are pending for this period.',
         'hint': 'Approvers act on these in File Requests › Approvals.'},
        {'key': 'punches', 'title': 'Missing punches with no FTS filed', 'icon': 'fingerprint', 'rows': punches,
         'empty': 'Every flagged missing punch has an FTS filed or was reviewed.',
         'hint': 'Flagged by the FTS engine. The employee needs to file an FTS, or HR dismisses it in FTS Review.'},
        {'key': 'leave', 'title': 'Leave not settled', 'icon': 'calendar-clock', 'rows': leave,
         'empty': 'All leave in this period is approved and charged.',
         'hint': 'Pending approval, sick leave waiting for HR verification, or leave not yet charged to a balance.'},
    ]
    by_type = {}
    for s in sections:
        for r in s['rows']:
            by_type[r['type']] = by_type.get(r['type'], 0) + 1
    total = sum(len(s['rows']) for s in sections)
    return {'sections': sections, 'total': total, 'by_type': by_type,
            'earlier_pending': earlier,
            'people': len({r['employee_id'] for s in sections for r in s['rows']})}


# ──────────────────────────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────────────────────────
def _guard():
    if 'user' not in session:
        return redirect(url_for('login'))
    if not can_view():
        flash('You do not have access to Payroll Readiness.', 'danger')
        return redirect(url_for('dashboard'))
    return None


@payroll_readiness_bp.route('/admin/payroll-readiness')
def page():
    blocked = _guard()
    if blocked:
        return blocked
    today = date.today()
    start, end = resolve_period(request.args.get('start'), today)
    state, state_text = period_state(start, end, today)
    data = collect(start, end, today)
    prev_s, _ = get_previous_period(start, end)
    next_s, _ = get_next_period(start, end)
    current_s, _ = get_payroll_period_for_date(today)
    return render_template('payroll_readiness/index.html', start=start, end=end,
                           state=state, state_text=state_text, d=data, today=today,
                           prev_start=prev_s, next_start=next_s, current_start=current_s,
                           earlier_to=start - timedelta(days=1),
                           default_start=get_default_payroll_period(today)[0])


@payroll_readiness_bp.route('/admin/payroll-readiness.csv')
def export_csv():
    blocked = _guard()
    if blocked:
        return blocked
    start, end = resolve_period(request.args.get('start'))
    data = collect(start, end)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(['Section', 'Type', 'Employee ID', 'Name', 'TL', 'Date', 'Details',
                'Waiting on', 'Filed / flagged', 'Days open'])
    for s in data['sections']:
        for r in s['rows']:
            filed = r.get('filed_at')
            w.writerow([s['title'], r['type'], r['employee_id'], r['name'], r['tl'],
                        r['date'].isoformat() if r.get('date') else '', r['details'], r['waiting_on'],
                        filed.strftime('%Y-%m-%d %H:%M') if hasattr(filed, 'strftime') else (filed or ''),
                        '' if r['age_days'] is None else r['age_days']])
    name = f"payroll-readiness_{start:%Y%m%d}-{end:%Y%m%d}.csv"
    return Response(out.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': f'attachment; filename="{name}"'})
