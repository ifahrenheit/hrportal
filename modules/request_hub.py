"""
request_hub.py — one page to file OT, RDW, CWS, Magic CWS and FTS.

The page only renders the five forms; each form still POSTs to its existing
handler (file_ot, file_cws, magic_cws_form, file_fts), so validation,
duplicate checks, tables, approval routing and emails are unchanged. Those
handlers send the user back here (with their usual flash message) when the
form carries return_to=hub. The individual forms stay available as before.

Opening this page is read-only: the Magic CWS balance is computed the same
way as _init_magic_cws_balance but without creating the balance row.
"""
from datetime import date, datetime, timedelta
from functools import wraps
import json

from flask import Blueprint, render_template, request, session, redirect, url_for

request_hub_bp = Blueprint('request_hub', __name__)

REQUEST_TYPES = ('ot', 'rdw', 'cws', 'magic', 'fts')


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


def _fmt_clock(v):
    if v is None:
        return ''
    if isinstance(v, timedelta):
        v = datetime(2000, 1, 1) + timedelta(seconds=int(v.total_seconds()) % 86400)
    return v.strftime('%-I:%M %p')


def _peek_magic_balance(conn, emp_number):
    """Remaining Magic CWS hours, read-only (no balance row is created)."""
    from app import get_db, _get_magic_leave_type_id
    with conn.cursor() as c:
        c.execute("SELECT initial_hrs, used_hrs FROM magic_cws_balance WHERE emp_number = %s",
                  (emp_number,))
        row = c.fetchone()
    if row:
        return round(float(row['initial_hrs']) - float(row['used_hrs']), 2)
    leave_type_id = _get_magic_leave_type_id()
    if not leave_type_id:
        return 0.0
    db = get_db()
    try:
        with db.cursor() as c:
            c.execute("""SELECT (no_of_days - days_used) * 8 AS remaining_hrs
                         FROM orangehrm2.ohrm_leave_entitlement
                         WHERE emp_number = %s AND leave_type_id = %s
                           AND YEAR(from_date) = YEAR(CURDATE()) AND deleted = 0
                         ORDER BY id DESC LIMIT 1""", (emp_number, leave_type_id))
            r = c.fetchone()
    finally:
        db.close()
    return round(float(r['remaining_hrs']), 2) if r and r['remaining_hrs'] else 0.0


@request_hub_bp.route('/file-requests/new')
@login_required
def new():
    from app import (get_central_db, get_central_employee_id, get_supervisor_email,
                     get_approver_names, _get_work_mode, SHIFTS_4DAY, SHIFTS_5DAY,
                     TICKET_REQUIRED_TYPES, app)

    user = session['user']
    emp_number = user['emp_number']
    employee_id = get_central_employee_id(user['email']) or (user.get('employee_id') or '')
    today = date.today()
    selected = request.args.get('type', '')
    if selected not in REQUEST_TYPES:
        selected = ''

    work_mode = _get_work_mode(emp_number)
    magic_shifts = SHIFTS_4DAY if work_mode == '4day' else SHIFTS_5DAY

    schedule, punches, recent = {}, {}, {k: [] for k in REQUEST_TYPES}
    magic_balance = 0.0
    approver = ''
    conn = get_central_db()
    try:
        with conn.cursor() as c:
            if employee_id:
                # schedule: 30 days back (OT/FTS/CWS from) to 45 days ahead (CWS to)
                c.execute("""SELECT schedule_date, shift_time, is_rest_day FROM employee_schedules
                             WHERE employee_id = %s AND schedule_date BETWEEN %s AND %s""",
                          (employee_id, today - timedelta(days=30), today + timedelta(days=45)))
                for r in c.fetchall():
                    schedule[r['schedule_date'].isoformat()] = (
                        'Rest day' if r['is_rest_day'] else (r['shift_time'] or ''))

                c.execute("""SELECT ot_date d, ot_type t, start_time s, end_time e, status FROM ot_requests
                             WHERE employee_id = %s AND deleted_at IS NULL ORDER BY id DESC LIMIT 4""", (employee_id,))
                recent['ot'] = [{'date': r['d'].strftime('%b %-d'), 'status': r['status'],
                                 'detail': f"{(r['t'] or '').replace('_', ' ').title()} · {_fmt_clock(r['s'])} – {_fmt_clock(r['e'])}"}
                                for r in c.fetchall()]
                c.execute("""SELECT rd_date d, start_time s, end_time e, work_category w, status FROM rd_requests
                             WHERE employee_id = %s AND deleted_at IS NULL ORDER BY id DESC LIMIT 4""", (employee_id,))
                recent['rdw'] = [{'date': r['d'].strftime('%b %-d'), 'status': r['status'],
                                  'detail': f"{_fmt_clock(r['s'])} – {_fmt_clock(r['e'])} · {(r['w'] or '').replace('_', ' ').title()}"}
                                 for r in c.fetchall()]
                c.execute("""SELECT original_date d, new_date nd, new_time nt, status FROM cws_requests
                             WHERE employee_id = %s AND deleted_at IS NULL ORDER BY id DESC LIMIT 4""", (employee_id,))
                recent['cws'] = [{'date': r['d'].strftime('%b %-d'), 'status': r['status'],
                                  'detail': f"Moved to {r['nd'].strftime('%b %-d') if r['nd'] else '?'} · {r['nt'] or ''}"}
                                 for r in c.fetchall()]
                c.execute("""SELECT fts_date d, fts_time t, fts_type ty, status FROM fts_requests
                             WHERE employeeID = %s AND deleted_at IS NULL ORDER BY id DESC LIMIT 4""", (employee_id,))
                recent['fts'] = [{'date': r['d'].strftime('%b %-d'), 'status': r['status'],
                                  'detail': f"{r['ty'] or ''} · {_fmt_clock(r['t'])}"}
                                 for r in c.fetchall()]
            c.execute("""SELECT original_date d, original_shift os, new_date nd, new_shift ns, deduction_hours h, status
                         FROM magic_cws_requests WHERE emp_number = %s AND status <> 'Deleted'
                         ORDER BY filed_at DESC LIMIT 4""", (emp_number,))
            recent['magic'] = [{'date': r['d'].strftime('%b %-d') if r['d'] else '', 'status': r['status'],
                                'detail': f"{r['os']} → {r['nd'].strftime('%b %-d') if r['nd'] else ''} {r['ns']} · {r['h']} hrs"}
                               for r in c.fetchall()]
            # time records of the last 14 days, for the missed-swipe hint
            c.execute("""SELECT d.date, d.type FROM dailytimerecord d JOIN userdata u ON u.personid = d.personid
                         WHERE LOWER(u.email) = %s AND d.date >= %s ORDER BY d.date""",
                      (user['email'].lower(), today - timedelta(days=14)))
            for r in c.fetchall():
                punches.setdefault(r['date'].date().isoformat(), []).append(
                    f"{(r['type'] or '').upper()} {_fmt_clock(r['date'])}")
        magic_balance = _peek_magic_balance(conn, emp_number)
    except Exception as e:
        app.logger.warning(f'request hub data: {e}')
    finally:
        conn.close()

    try:
        sup = get_supervisor_email(emp_number)
        if sup and employee_id:
            approver = (get_approver_names([employee_id]).get(employee_id) or {}).get('name') or sup
        elif sup:
            approver = sup
    except Exception as e:
        app.logger.warning(f'request hub approver: {e}')

    week_start = today - timedelta(days=today.weekday())
    week = []
    for i in range(7):
        d = week_start + timedelta(days=i)
        s = schedule.get(d.isoformat(), '')
        week.append({'label': d.strftime('%a, %b %-d'), 'shift': s.replace('-', ' – ') if s else 'No schedule',
                     'today': d == today, 'rest': s == 'Rest day'})

    status_style = {'approved': 'ok', 'served': 'ok', 'pending': 'warn', 'rejected': 'bad',
                    'cancelled': 'muted', 'scheduled': 'info'}
    for items in recent.values():
        for r in items:
            r['style'] = status_style.get((r['status'] or '').lower(), 'muted')

    time_options = [f"{h:02d}:{m}:00" for h in range(24) for m in ('00', '30')]
    return render_template(
        'file_requests/new.html',
        selected=selected, approver=approver, week=week, recent=recent,
        time_options=time_options, magic_shifts=magic_shifts, work_mode=work_mode,
        magic_balance=magic_balance, magic_max=10 if work_mode == '4day' else 8,
        ot_ticket_types=sorted(TICKET_REQUIRED_TYPES),
        schedule_json=json.dumps(schedule), punches_json=json.dumps(punches),
        today=today.isoformat(), has_employee_id=bool(employee_id))
