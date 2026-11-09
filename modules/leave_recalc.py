"""
Recalculate filed leave -- /admin/emp-settings/recalc

Leave hours are fixed when the leave is filed (app.calculate_deduction): 10 h
per working day on 4-day mode, 8 h on 5-day, 0 h on a rest day. Changing an
employee's work mode (Admin > Work Mode) or their Google Sheet schedule later
does NOT touch leave they already filed. This page re-runs each filed day
through the same calculate_deduction, with the employee's current work mode
and schedule, shows old vs new, and applies the difference on Apply.

What it changes, per day whose hours differ:
  - leave4day_requests.hours_deducted / days_deducted / is_off_day
  - ohrm_leave.length_hours / length_days (same leave_request_id + date)
  - ohrm_leave_entitlement.days_used, charged or refunded by the difference
    (via app.adjust_leave_entitlement). Days still credit_pending were never
    charged, so only the row changes; the daily cron charges the new amount.
  - one central_db.employee_audit_log row per day (change_source='leave_recalc')

It never adds or removes days. A 5-day employee's rest day gets no row when
filed, while a 4-day rest day is stored as a 0-hour row; here an existing row
that now falls on a rest day becomes 0 h, and a 0-hour off-day row that is
now a working day gets full hours.

Skipped (listed on the preview, never changed): holidays, AWOL, rows whose
hours aren't a standard full / half / off value (Custom Hours filings).

app.py runs as __main__, so this module must not `from app import ...`.
init_leave_recalc() is called from app.py and hands over the helpers.
"""
from datetime import date, datetime

from flask import (Blueprint, render_template, request, session, redirect,
                   url_for, flash)

from csrf import validate_csrf
from helpers.leave_credits import ACTIVE_STATUSES

leave_recalc_bp = Blueprint('leave_recalc', __name__)

_h = {}

# Hours a non-custom filing can have: off, 5-day half, 4-day half, 5-day full, 4-day full.
STANDARD_HOURS = {0.0, 4.0, 5.0, 8.0, 10.0}
SHIFT_TO_DURATION = {'1st Half': 'first_half', '2nd Half': 'second_half'}


def init_leave_recalc(**helpers):
    """helpers: get_db, get_central_db, calculate_deduction, is_holiday,
    get_work_mode, adjust_leave_entitlement, resolve_emp_number, awol_type."""
    _h.update(helpers)


def _allowed():
    return bool(session.get('is_admin')) or bool(
        session.get('permissions', {}).get('can_work_mode'))


def expected_for_row(row, calc, holiday):
    """What one filed day should deduct now.

    row: leave4day_requests dict (leave_date, hours_deducted, shift_half, ...).
    calc(date, duration) -> calculate_deduction(emp, date, date, duration).
    holiday(date) -> bool.
    Returns (hours, days, is_off, skip_reason); skip_reason set means leave it."""
    d = row['leave_date']
    old_hours = float(row['hours_deducted'])
    if holiday(d):
        return None, None, None, 'Holiday'
    if old_hours not in STANDARD_HOURS:
        return None, None, None, f'Custom hours ({old_hours:g} h), check manually'
    duration = SHIFT_TO_DURATION.get(row.get('shift_half') or '', 'full')
    result = calc(d, duration)
    if not result:
        # 5-day mode skips rest days entirely; an existing row becomes 0 h.
        return 0.0, 0.0, True, None
    r = result[0]
    return float(r['hours']), float(r['days']), bool(r['is_off_day']), None


def plan_changes(rows, calc, holiday, awol_type_id=None):
    """Split filed rows into (changes, skipped). Pure apart from calc/holiday."""
    changes, skipped = [], []
    for row in rows:
        if awol_type_id is not None and int(row['leave_type_id']) == int(awol_type_id):
            skipped.append({**row, 'reason': 'AWOL'})
            continue
        hours, days, is_off, reason = expected_for_row(row, calc, holiday)
        if reason:
            skipped.append({**row, 'reason': reason})
            continue
        old_days = float(row['days_deducted'])
        if (abs(hours - float(row['hours_deducted'])) < 0.005
                and abs(days - old_days) < 0.00005
                and bool(is_off) == bool(row['is_off_day'])):
            continue
        changes.append({**row, 'new_hours': hours, 'new_days': round(days, 4),
                        'new_is_off': int(bool(is_off)),
                        'delta_days': round(days - old_days, 4)})
    return changes, skipped


def _load_rows(c, emp_number, from_date, lock=False):
    marks = ','.join(['%s'] * len(ACTIVE_STATUSES))
    sql = f"""
        SELECT r.id, r.emp_number, r.employee_id, r.leave_type_id, r.leave_date,
               r.hours_deducted, r.days_deducted, r.is_off_day, r.status,
               r.shift_half, r.leave_request_id, r.credit_pending,
               lt.name AS leave_type_name
        FROM leave4day_requests r
        JOIN ohrm_leave_type lt ON lt.id = r.leave_type_id
        WHERE r.emp_number = %s AND r.leave_date >= %s
          AND r.deleted_at IS NULL AND r.status IN ({marks})
        ORDER BY r.leave_date, r.id
    """
    if lock:
        sql += " FOR UPDATE"
    c.execute(sql, [emp_number, from_date] + list(ACTIVE_STATUSES))
    return c.fetchall()


def _plan(c, emp_number, from_date, lock=False):
    rows = _load_rows(c, emp_number, from_date, lock=lock)
    calc = lambda d, dur: _h['calculate_deduction'](emp_number, d, d, dur)
    awol = _h['awol_type']()
    return plan_changes(rows, calc, _h['is_holiday'], awol['id'] if awol else None)


def _parse_from(raw):
    try:
        return datetime.strptime(raw, '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return date.today().replace(day=1)


def _employee(c, emp_number):
    c.execute("""SELECT h.emp_number, h.employee_id,
                        CONCAT(h.emp_firstname, ' ', h.emp_lastname) AS name,
                        es.work_mode AS individual_mode, es.updated_at AS mode_updated_at
                 FROM hs_hr_employee h
                 LEFT JOIN leave4day_emp_settings es ON es.emp_number = h.emp_number
                 WHERE h.emp_number = %s""", (emp_number,))
    return c.fetchone()


@leave_recalc_bp.route('/admin/emp-settings/recalc', methods=['GET', 'POST'])
def recalc_filed_leave():
    if 'user' not in session:
        return redirect(url_for('login'))
    if not _allowed():
        flash('You do not have permission to access this page.', 'danger')
        return redirect(url_for('dashboard'))

    src = request.form if request.method == 'POST' else request.args
    employee_id = (src.get('employee_id') or '').strip()
    from_date = _parse_from(src.get('from'))
    emp_number = _h['resolve_emp_number'](None, employee_id) if employee_id else None

    if request.method == 'POST':
        if not validate_csrf():
            flash('Security check failed, please try again.', 'danger')
            return redirect(url_for('leave_recalc.recalc_filed_leave',
                                    employee_id=employee_id, **{'from': from_date.isoformat()}))
        if not emp_number:
            flash('Employee not found.', 'danger')
            return redirect(url_for('leave_recalc.recalc_filed_leave'))
        return _apply(emp_number, employee_id, from_date)

    emp, changes, skipped = None, [], []
    if employee_id:
        if not emp_number:
            flash('Employee not found.', 'danger')
        else:
            db = _h['get_db']()
            try:
                with db.cursor() as c:
                    emp = _employee(c, emp_number)
                    changes, skipped = _plan(c, emp_number, from_date)
            finally:
                db.close()
    return render_template('admin/leave_recalc.html', emp=emp, employee_id=employee_id,
                           from_date=from_date, changes=changes, skipped=skipped,
                           work_mode=_h['get_work_mode'](emp_number) if emp_number else None,
                           total_delta=round(sum(ch['delta_days'] for ch in changes), 4))


def _apply(emp_number, employee_id, from_date):
    db = _h['get_db']()
    applied = []
    try:
        with db.cursor() as c:
            # Re-plan under row locks so the preview can't be stale.
            changes, _ = _plan(c, emp_number, from_date, lock=True)
            for ch in changes:
                c.execute("""UPDATE leave4day_requests
                             SET hours_deducted = %s, days_deducted = %s, is_off_day = %s
                             WHERE id = %s""",
                          (ch['new_hours'], ch['new_days'], ch['new_is_off'], ch['id']))
                c.execute("""UPDATE ohrm_leave SET length_hours = %s, length_days = %s
                             WHERE leave_request_id = %s AND emp_number = %s AND date = %s""",
                          (ch['new_hours'], ch['new_days'], ch['leave_request_id'],
                           emp_number, ch['leave_date']))
                if not ch['credit_pending'] and ch['delta_days']:
                    item = [(ch['leave_date'], abs(ch['delta_days']))]
                    _h['adjust_leave_entitlement'](c, emp_number, ch['leave_type_id'], item,
                                                   refund=ch['delta_days'] < 0)
                applied.append(ch)
        db.commit()
    except Exception as e:
        db.rollback()
        flash(f'Error, nothing was changed: {e}', 'danger')
        return redirect(url_for('leave_recalc.recalc_filed_leave', employee_id=employee_id,
                                **{'from': from_date.isoformat()}))
    finally:
        db.close()

    if applied:
        _audit(applied, employee_id)
        total = round(sum(ch['delta_days'] for ch in applied), 4)
        flash(f'Recalculated {len(applied)} leave day(s). Balance change: '
              f'{total:+g} day(s) used.', 'success')
    else:
        flash('Nothing to change: filed leave already matches the current work mode and schedule.',
              'info')
    return redirect(url_for('leave_recalc.recalc_filed_leave', employee_id=employee_id,
                            **{'from': from_date.isoformat()}))


def _audit(applied, employee_id):
    """Best effort: the leave tables have no history, so record each change."""
    try:
        conn = _h['get_central_db']()
        try:
            with conn.cursor() as c:
                for ch in applied:
                    c.execute("""INSERT INTO employee_audit_log
                                 (employee_id, field_name, old_value, new_value, changed_by, change_source)
                                 VALUES (%s, 'leave_hours', %s, %s, %s, 'leave_recalc')""",
                              (employee_id,
                               f"{ch['leave_date']} {ch['leave_type_name']} #{ch['leave_request_id']}: "
                               f"{float(ch['hours_deducted']):g} h / {float(ch['days_deducted']):g} d",
                               f"{ch['new_hours']:g} h / {ch['new_days']:g} d",
                               session['user'].get('name') or session['user'].get('email')))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass
