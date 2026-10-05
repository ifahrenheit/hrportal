"""
File Request for Employee -- /admin/file-request

Lets HR file an FTS, CWS or OT / Rest Day Work request on an employee's
behalf, including Separated employees, who can no longer log in to file
their own. It is the request-side sibling of /admin/file-leave
(admin_file_leave) and uses the same permission, can_file_for_emp.

The rows written are the same ones the self-service forms write
(file_fts / file_cws / file_ot in app.py), with the same validation and
duplicate checks, so the requests show up and get approved exactly like
self-filed ones. Admins can tick "File as approved", which sets
status / approver_name / approved_at the way /api/approvals/action does --
useful for Separated employees, who no longer have an approver to route to.

Every filing is recorded in central_db.employee_audit_log
(change_source='file_for_employee'), since the request tables have no
"filed by" column.

app.py runs as __main__, so this module must not `from app import ...`
(that re-executes app.py as a second module). init_file_for_emp() is
called from app.py instead and hands over the helpers this module needs.
"""
import re
from datetime import datetime, date, timedelta
from functools import wraps

import markupsafe
import pymysql.cursors
from flask import (Blueprint, render_template, request, session, redirect,
                   url_for, flash, current_app)

from csrf import validate_csrf

file_for_emp_bp = Blueprint('file_for_emp', __name__)

# Filled by init_file_for_emp(); see the module docstring.
_h = {}


def init_file_for_emp(**helpers):
    """helpers: get_central_db, get_db, send_email, get_supervisor_email,
    fetch_requests (app._fetch_pim_requests), ticket_required_types."""
    _h.update(helpers)


# Same option lists as templates/file_requests/ot.html.
OT_TYPES = [
    ('PRE', 'PRE (Before Shift)'), ('POST', 'POST (After Shift)'),
    ('OVERHEAD_OT', 'Overhead OT'), ('TEAM_MEETING', 'Team Meeting'),
    ('QA_REFRESHER', 'QA Refresher Training'), ('OT260', 'OT260'),
    ('DAILY_OT', 'Daily OT'),
]
RD_CATEGORIES = [
    ('REGULAR', 'Regular Work'), ('OVERHEAD_OT', 'Overhead OT'),
    ('TEAM_MEETING', 'Team Meeting'), ('OT260', 'OT260'), ('DAILY_OT', 'Daily OT'),
]
TIME_OPTIONS = [f"{h:02d}:{m}:00" for h in range(24) for m in ('00', '30')]
REQ_TYPES = ('fts', 'cws', 'ot')


def can_file_for_emp():
    return bool(session.get('is_admin') or session.get('permissions', {}).get('can_file_for_emp'))


def _require_perm(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get('user'):
            return redirect(url_for('login'))
        if not can_file_for_emp():
            flash('You do not have permission to access this page.', 'danger')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return wrapper


def _load_employee(employee_id):
    conn = _h['get_central_db']()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as c:
            c.execute("""
                SELECT employee_id, schedule_name, status, exit_date, email,
                       approver, tl, group_name
                FROM gsheet_employees WHERE employee_id = %s
            """, (employee_id,))
            return c.fetchone()
    finally:
        conn.close()


def _emp_number(employee_id):
    db = _h['get_db']()
    try:
        with db.cursor() as c:
            c.execute("SELECT emp_number FROM hs_hr_employee WHERE employee_id = %s LIMIT 1", (employee_id,))
            row = c.fetchone()
            return row['emp_number'] if row else None
    finally:
        db.close()


def _parse_date(value):
    try:
        return datetime.strptime(value, '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return None


def _exit_date(emp):
    d = emp.get('exit_date')
    if isinstance(d, datetime):
        return d.date()
    return d if isinstance(d, date) else None


# ── Per-type validate + insert. Each returns (errors, summary, details_html,
#    label, inserted_id). Rules mirror the self-service routes in app.py. ──

def _file_fts(c, emp, form, approved):
    fts_date = _parse_date(form.get('fts_date', '').strip())
    fts_time = form.get('fts_time', '').strip()
    fts_type = form.get('fts_type', '').strip()
    errors = []
    if not fts_date:
        errors.append('FTS date is required.')
    if not re.fullmatch(r'\d{2}:\d{2}(:\d{2})?', fts_time):
        errors.append('FTS time is required.')
    if fts_type not in ('IN', 'OUT'):
        errors.append('FTS type must be IN or OUT.')
    if errors:
        return errors, None, None, 'FTS', None
    c.execute("""SELECT id FROM fts_requests
                 WHERE employeeID = %s AND fts_date = %s AND fts_time = %s AND fts_type = %s""",
              (emp['employee_id'], fts_date, fts_time, fts_type))
    dup = c.fetchone()
    if dup:
        return [f'This exact FTS already exists (request #{dup["id"]}).'], None, None, 'FTS', None
    c.execute("""INSERT INTO fts_requests
                 (employeeID, employee_name, fts_date, fts_time, fts_type, status, approver,
                  approver_name, approved_at)
                 VALUES (%s, %s, %s, %s, %s, %s, '', %s, %s)""",
              (emp['employee_id'], emp['schedule_name'], fts_date, fts_time, fts_type,
               *_approval_cols(approved)))
    summary = f"FTS {fts_type} {fts_date} {fts_time[:5]}"
    details = (f"<li><b>Date:</b> {fts_date}</li><li><b>Time:</b> {markupsafe.escape(fts_time[:5])}</li>"
               f"<li><b>Type:</b> {fts_type}</li>")
    return [], summary, details, 'FTS', c.lastrowid


def _file_cws(c, emp, form, approved):
    original_date = _parse_date(form.get('original_date', '').strip())
    original_time = form.get('original_time', '').strip()
    new_date      = _parse_date(form.get('new_date', '').strip())
    new_time      = form.get('new_time', '').strip()
    reason        = form.get('reason', '').strip()
    if not all([original_date, original_time, new_date, new_time, reason]):
        return ['All CWS fields are required.'], None, None, 'CWS', None
    c.execute("""SELECT id FROM cws_requests
                 WHERE employee_id = %s AND original_date = %s AND original_time = %s
                   AND new_date = %s AND new_time = %s AND deleted_at IS NULL""",
              (emp['employee_id'], original_date, original_time, new_date, new_time))
    dup = c.fetchone()
    if dup:
        return [f'This exact CWS already exists (request #{dup["id"]}).'], None, None, 'CWS', None
    c.execute("""INSERT INTO cws_requests
                 (employee_id, original_date, original_time, new_date, new_time, reason, status,
                  approver_name, approved_at)
                 VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
              (emp['employee_id'], original_date, original_time, new_date, new_time, reason,
               *_approval_cols(approved)))
    summary = f"CWS {original_date} {original_time} -> {new_date} {new_time}"
    esc = markupsafe.escape
    details = (f"<li><b>Original:</b> {original_date} {esc(original_time)}</li>"
               f"<li><b>New:</b> {new_date} {esc(new_time)}</li><li><b>Reason:</b> {esc(reason)}</li>")
    return [], summary, details, 'CWS', c.lastrowid


def _file_ot(c, emp, form, approved):
    work_day_type = form.get('work_day_type', '').strip()
    ot_date       = _parse_date(form.get('ot_date', '').strip())
    start_time    = form.get('start_time', '').strip()
    end_time      = form.get('end_time', '').strip()
    ot_type       = form.get('ot_type', '').strip()
    regular_rate  = form.get('regular_rate', '').strip()
    work_category = form.get('work_category', 'REGULAR').strip()
    took_break    = form.get('took_break', '').strip()
    tickets_raw   = form.get('ticket_numbers', '').strip()
    label = 'Overtime' if work_day_type == 'REGULAR' else 'Rest Day Work'

    errors = []
    if work_day_type not in ('REGULAR', 'REST_DAY'):
        errors.append('Please select a Work Day Type.')
    if not ot_date:
        errors.append('Date is required.')
    if start_time not in TIME_OPTIONS or end_time not in TIME_OPTIONS:
        errors.append('Start and end time are required.')
    if work_day_type == 'REGULAR':
        if ot_type not in dict(OT_TYPES):
            errors.append('OT Type is required.')
        if regular_rate not in ('Yes', 'No'):
            errors.append('Regular Rate is required.')
    if work_day_type == 'REST_DAY':
        if work_category not in dict(RD_CATEGORIES):
            errors.append('Work category is required.')
        if took_break not in ('Y', 'N'):
            errors.append('Please indicate whether a 1-hour break was taken.')

    ticket_list = []
    needs_tickets = ((work_day_type == 'REGULAR' and ot_type in _h['ticket_required_types']) or
                     (work_day_type == 'REST_DAY' and work_category in ('REGULAR', 'OT260')))
    if needs_tickets:
        if not tickets_raw:
            errors.append('Ticket numbers are required for this request type.')
        else:
            invalid = []
            for t in [t.strip() for t in re.split(r'[\s,\n\r\t]+', tickets_raw) if t.strip()]:
                if not t.isalnum():
                    invalid.append(f"{t} (invalid characters)")
                elif len(t) < 5:
                    invalid.append(f"{t} (too short, min 5 characters)")
                else:
                    ticket_list.append(t)
            if invalid:
                errors.append(f"Invalid ticket(s): {', '.join(invalid)}")
    if errors:
        return errors, None, None, label, None

    dt_start = datetime.strptime(start_time, '%H:%M:%S')
    dt_end   = datetime.strptime(end_time, '%H:%M:%S')
    if dt_end <= dt_start:
        dt_end += timedelta(days=1)
    ot_hours = round((dt_end - dt_start).seconds / 3600, 2)
    hours_per_ticket = round(ot_hours / len(ticket_list), 2) if ticket_list else ot_hours
    eid = emp['employee_id']
    status, approver_name, approved_at = _approval_cols(approved)

    if work_day_type == 'REGULAR':
        c.execute("""SELECT id FROM ot_requests
                     WHERE employee_id = %s AND ot_date = %s AND status IN ('Pending','Approved')
                       AND deleted_at IS NULL
                       AND ((start_time = %s AND end_time = %s) OR (start_time < %s AND end_time > %s))""",
                  (eid, ot_date, start_time, end_time, end_time, start_time))
        dup = c.fetchone()
        if dup:
            return [f'An OT request with overlapping time already exists on this date (request #{dup["id"]}).'], None, None, label, None
        c.execute("""INSERT INTO ot_requests
                     (employee_id, ot_date, ot_type, regular_rate, start_time, end_time, status,
                      tickets_submitted, timestamp, approver_name, approved_at)
                     VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW(), %s, %s)""",
                  (eid, ot_date, ot_type, regular_rate, start_time, end_time,
                   status, 1 if ticket_list else 0, approver_name, approved_at))
        new_id, fk, desc = c.lastrowid, 'ot_request_id', 'OT ticket'
        summary = f"OT {ot_type} {ot_date} {start_time[:5]}-{end_time[:5]}"
    else:
        c.execute("""SELECT id, status FROM rd_requests
                     WHERE employee_id = %s AND rd_date = %s
                       AND status NOT IN ('Cancelled','Rejected','Deleted')
                       AND ((start_time = %s AND end_time = %s) OR (start_time < %s AND end_time > %s))""",
                  (eid, ot_date, start_time, end_time, end_time, start_time))
        dup = c.fetchone()
        if dup:
            return [f'A {dup["status"]} RDW request with overlapping time already exists on this date (request #{dup["id"]}).'], None, None, label, None
        c.execute("""INSERT INTO rd_requests
                     (employee_id, rd_date, start_time, end_time, work_category, took_break, status,
                      approver_name, approved_at)
                     VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                  (eid, ot_date, start_time, end_time, work_category, took_break,
                   status, approver_name, approved_at))
        new_id, fk, desc = c.lastrowid, 'rd_request_id', 'RDW ticket'
        summary = f"RDW {work_category} {ot_date} {start_time[:5]}-{end_time[:5]}"

    for ticket_number in ticket_list:
        c.execute(f"""INSERT INTO ot_tickets
                      ({fk}, agent_id, ot_date, ot_start_time, ot_end_time, ot_hours,
                       ticket_type, priority, status, ticket_number, issue_description, resolution_notes)
                      VALUES (%s, %s, %s, %s, %s, %s, 'general', 'medium', 'pending', %s, %s, '')""",
                  (new_id, eid, ot_date, start_time, end_time, hours_per_ticket, ticket_number, desc))
    if ticket_list:
        summary += f" ({len(ticket_list)} ticket(s))"
    details = (f"<li><b>Date:</b> {ot_date}</li><li><b>Time:</b> {start_time[:5]} → {end_time[:5]}</li>"
               f"<li><b>{'Type' if work_day_type == 'REGULAR' else 'Category'}:</b> "
               f"{ot_type if work_day_type == 'REGULAR' else work_category}</li>"
               f"<li><b>Tickets:</b> {len(ticket_list) or 'N/A'}</li>")
    return [], summary, details, label, new_id


def _approval_cols(approved):
    """(status, approver_name, approved_at) for the INSERT -- the same three
    columns /api/approvals/action sets when it approves."""
    if approved:
        return ('Approved', session['user']['name'], datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    return ('Pending', None, None)


_FILERS = {'fts': _file_fts, 'cws': _file_cws, 'ot': _file_ot}


def _notify(emp, label, details_html, approved):
    """Separated employees get no email (the mailbox is usually gone). A
    Pending request also goes to the approver, the same as a self-filed one."""
    filer = markupsafe.escape(session['user']['name'])
    name  = markupsafe.escape(emp['schedule_name'] or emp['employee_id'])
    state = 'filed and approved' if approved else 'filed and is awaiting approval'
    if emp['status'] != 'Separated' and emp.get('email'):
        _h['send_email'](emp['email'], f'{label} Request Filed on Your Behalf',
            f"<p>Hi {name},</p><p>A <b>{label}</b> request was {state} on your behalf by {filer}.</p>"
            f"<ul>{details_html}</ul><p><a href='https://hrportal.cohere.ph'>View HR Portal</a></p>")
    if not approved:
        emp_number = _emp_number(emp['employee_id'])
        sup_email = _h['get_supervisor_email'](emp_number) if emp_number else None
        if sup_email:
            _h['send_email'](sup_email, f'{label} Request for {name}',
                f"<p>Hi,</p><p>{filer} filed a <b>{label}</b> request on behalf of <b>{name}</b>. "
                f"It is awaiting your approval.</p><ul>{details_html}</ul>"
                f"<p><a href='https://hrportal.cohere.ph/file-requests/approvals'>Approve / Reject on HR Portal</a></p>")


@file_for_emp_bp.route('/admin/file-request', methods=['GET', 'POST'])
@_require_perm
def index():
    employee_id = (request.values.get('employee_id') or '').strip()
    req_type = request.values.get('type', 'fts')
    if req_type not in REQ_TYPES:
        req_type = 'fts'
    is_admin = bool(session.get('is_admin'))

    emp = _load_employee(employee_id) if employee_id else None
    if employee_id and not emp:
        flash(f'Employee {employee_id} was not found.', 'danger')
        return redirect(url_for('file_for_emp.index'))

    if request.method == 'POST':
        if not validate_csrf():
            flash('Security check failed, please try again.', 'danger')
            return redirect(url_for('file_for_emp.index', employee_id=employee_id, type=req_type))
        if not emp:
            flash('Select an employee first.', 'danger')
            return redirect(url_for('file_for_emp.index'))
        approved = is_admin and request.form.get('file_approved') == '1'

        # A Separated employee can't have worked after their exit date.
        exit_d = _exit_date(emp)
        key_date = _parse_date(request.form.get(
            {'fts': 'fts_date', 'cws': 'original_date', 'ot': 'ot_date'}[req_type], '').strip())
        if exit_d and key_date and key_date > exit_d:
            flash(f"{emp['schedule_name']} separated on {exit_d:%b %d, %Y}; "
                  f"a request dated {key_date:%b %d, %Y} can't be filed.", 'danger')
            return render_page(emp, req_type, request.form)

        conn = _h['get_central_db']()
        try:
            with conn.cursor(pymysql.cursors.DictCursor) as c:
                errors, summary, details, label, new_id = _FILERS[req_type](c, emp, request.form, approved)
                if errors:
                    conn.rollback()
                    flash(' | '.join(errors), 'danger')
                    return render_page(emp, req_type, request.form)
                c.execute("""INSERT INTO employee_audit_log
                             (employee_id, field_name, old_value, new_value, changed_by, change_source)
                             VALUES (%s, 'filed_request', NULL, %s, %s, 'file_for_employee')""",
                          (emp['employee_id'], f"#{new_id} {summary} [{'Approved' if approved else 'Pending'}]",
                           session['user']['name']))
            conn.commit()
        except Exception:
            conn.rollback()
            current_app.logger.exception(f"file-for-employee {req_type} failed for {employee_id}")
            flash('Something went wrong; nothing was filed.', 'danger')
            return render_page(emp, req_type, request.form)
        finally:
            conn.close()

        current_app.logger.info(f"file-for-employee: {summary} #{new_id} for {employee_id} "
                                f"by {session['user']['name']} ({'Approved' if approved else 'Pending'})")
        try:
            _notify(emp, label, details, approved)
        except Exception as e:
            current_app.logger.warning(f"file-for-employee notify failed: {e}")
        flash(f"{label} request #{new_id} filed for {emp['schedule_name']} "
              f"as {'Approved' if approved else 'Pending'}.", 'success')
        return redirect(url_for('file_for_emp.index', employee_id=employee_id, type=req_type))

    return render_page(emp, req_type, {})


def render_page(emp, req_type, form):
    conn = _h['get_central_db']()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as c:
            c.execute("""
                SELECT employee_id, schedule_name, status, exit_date FROM gsheet_employees
                WHERE schedule_name IS NOT NULL AND schedule_name != ''
                ORDER BY status = 'Separated', schedule_name
            """)
            employees = c.fetchall()
        recent = []
        if emp:
            today = date.today()
            recent = _h['fetch_requests'](conn, emp['employee_id'], today - timedelta(days=120),
                                          today + timedelta(days=60))
    finally:
        conn.close()
    supervisor_email = None
    if emp:
        emp_number = _emp_number(emp['employee_id'])
        supervisor_email = _h['get_supervisor_email'](emp_number) if emp_number else None
    return render_template('admin/file_request_for_employee.html',
                           employees=employees, emp=emp, req_type=req_type, form=form,
                           exit_date=_exit_date(emp) if emp else None,
                           supervisor_email=supervisor_email, recent=recent,
                           is_admin=bool(session.get('is_admin')),
                           ot_types=OT_TYPES, rd_categories=RD_CATEGORIES,
                           time_options=TIME_OPTIONS,
                           ticket_required=sorted(_h['ticket_required_types']))
