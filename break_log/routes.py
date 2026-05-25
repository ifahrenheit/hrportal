"""
Break Log Blueprint
Ported from break-log.php, supervisor-break-logs.php,
api/break-api.php, api/supervisor-api.php
"""

from flask import (Blueprint, render_template, request, jsonify,
                   session, redirect, url_for, Response)
from functools import wraps
from datetime import datetime, date, timedelta
import re, csv, io, ipaddress

break_log_bp = Blueprint('break_log', __name__)

# ── IP Allowlist (same as PHP) ────────────────────────────────────────────────
ALLOWED_IPS = [
    '203.28.65.222',
    '203.82.42.177', '203.82.42.178', '203.82.42.179',
    '203.82.42.180', '203.82.42.181', '203.82.42.182',
    '3.124.128.189',          # Numa VPN
    '192.168.0.0/16',
    '10.0.0.0/8',
    '172.16.0.0/12',
]

def _is_allowed_ip(ip):
    try:
        addr = ipaddress.ip_address(ip)
        for r in ALLOWED_IPS:
            if addr in ipaddress.ip_network(r, strict=False):
                return True
    except ValueError:
        pass
    return False


# ── Shift helpers (ported from PHP) ──────────────────────────────────────────
def _normalize_shift(s):
    """Convert '4pm-4am' → '16:00-04:00'. Leaves '22:00-07:00' unchanged."""
    if not s:
        return None
    if re.match(r'^\d{2}:\d{2}-\d{2}:\d{2}$', s):
        return s
    m = re.match(r'^(\d{1,2})(am|pm)-(\d{1,2})(am|pm)$', s, re.I)
    if not m:
        return None
    sh, sp, eh, ep = int(m[1]), m[2].lower(), int(m[3]), m[4].lower()
    if sp == 'pm' and sh != 12: sh += 12
    elif sp == 'am' and sh == 12: sh = 0
    if ep == 'pm' and eh != 12: eh += 12
    elif ep == 'am' and eh == 12: eh = 0
    return f'{sh:02d}:00-{eh:02d}:00'


def _shift_date(conn, employee_id):
    """
    Returns the business shift date for an employee.
    Handles overnight shifts with 9 AM cutoff rule.
    """
    now = datetime.now()
    today = now.strftime('%Y-%m-%d')
    yesterday = (now - timedelta(days=1)).strftime('%Y-%m-%d')
    cur_min = now.hour * 60 + now.minute

    if now.hour < 9:
        with conn.cursor() as c:
            c.execute("""
                SELECT shift_time FROM employee_schedules
                WHERE employee_id = %s AND schedule_date = %s AND is_rest_day = 0
                LIMIT 1
            """, (employee_id, yesterday))
            row = c.fetchone()
        if row:
            st = _normalize_shift(row['shift_time'])
            if st:
                m = re.match(r'^(\d{2}):\d{2}-(\d{2}):(\d{2})$', st)
                if m:
                    sh, eh, em = int(m[1]), int(m[2]), int(m[3])
                    if sh > eh and cur_min < (eh * 60 + em):
                        return yesterday
    return today


def _serialize_row(row):
    """Convert datetime/date objects to strings for JSON serialization."""
    d = dict(row)
    for k, v in d.items():
        if isinstance(v, datetime):
            d[k] = v.isoformat()
        elif isinstance(v, date):
            d[k] = v.strftime('%Y-%m-%d')
    return d


# ── Auth decorators ───────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user' not in session:
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated


def supervisor_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user' not in session:
            return redirect(url_for('index'))
        if not (session.get('is_admin') or session.get('is_supervisor') or session.get('is_sub_admin')):
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated

def _get_employee_id(email):
    """Resolve employee_id from gsheet_employees using email."""
    from app import get_central_db
    db = get_central_db()
    try:
        with db.cursor() as c:
            c.execute("""
                SELECT employee_id, account, group_name 
                FROM gsheet_employees
                WHERE LOWER(email) = %s AND status = 'Active'
                LIMIT 1
            """, (email.lower(),))
            return c.fetchone()  # returns full row: employee_id, account, group_name
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# AGENT BREAK LOG PAGE
# ─────────────────────────────────────────────────────────────────────────────
@break_log_bp.route('/break-log')
@login_required
def break_log():
    from app import get_central_db

    user_ip = request.remote_addr
    if not _is_allowed_ip(user_ip):
        return render_template('break_log/ip_denied.html', ip=user_ip), 403

    email = session['user'].get('email', '')
    emp = _get_employee_id(email)

    if not emp or emp['account'] not in ('Arctic', 'Numa'):
        return render_template('break_log/access_denied.html',
                               account_type=emp['account'] if emp else 'Unknown'), 403

    employee_id = emp['employee_id']
    db = get_central_db()
    try:
        shift_date = _shift_date(db, employee_id)
    finally:
        db.close()

    return render_template('break_log/break_log.html',
                           account_type=emp['account'],
                           group_name=emp.get('group_name'),
                           shift_date=shift_date)


# ─────────────────────────────────────────────────────────────────────────────
# AGENT BREAK API
# Replaces api/break-api.php
# ─────────────────────────────────────────────────────────────────────────────
@break_log_bp.route('/api/break', methods=['GET', 'POST'])
@login_required
def break_api():
    try:
        from app import get_central_db

        email = session['user'].get('email', '')
        emp = _get_employee_id(email)
        if not emp:
            return jsonify({'success': False, 'error': 'Employee not found'})
        employee_id = emp['employee_id']

        if request.method == 'GET':
            action = request.args.get('action')

            if action == 'check_active':
                db = get_central_db()
                try:
                    with db.cursor() as c:
                        c.execute("""
                            SELECT id, break_start FROM break_logs
                            WHERE employee_id = %s
                              AND break_end IS NULL
                              AND break_start >= NOW() - INTERVAL 24 HOUR
                            LIMIT 1
                        """, (employee_id,))
                        active = c.fetchone()
                finally:
                    db.close()

                if active:
                    return jsonify({'success': True, 'activeBreak': {
                        'id': active['id'],
                        'break_start': active['break_start'].isoformat()
                    }})
                return jsonify({'success': True, 'activeBreak': None})

            elif action == 'get_logs':
                db = get_central_db()
                try:
                    shift_date = _shift_date(db, employee_id)
                    with db.cursor() as c:
                        c.execute("""
                            SELECT id, break_start, break_end,
                                   TIMESTAMPDIFF(SECOND, break_start, break_end) AS duration
                            FROM break_logs
                            WHERE employee_id = %s
                              AND shift_date = %s
                              AND break_end IS NOT NULL
                            ORDER BY break_start
                        """, (employee_id, shift_date))
                        logs = c.fetchall()
                    total = sum(r['duration'] or 0 for r in logs)
                finally:
                    db.close()

                return jsonify({
                    'success': True,
                    'breaks': [_serialize_row(r) for r in logs],
                    'total_break_time': total
                })

            return jsonify({'success': False, 'error': 'Invalid action'})

        data = request.get_json() or {}
        action = data.get('action')

        if action == 'start':
            db = get_central_db()
            try:
                with db.cursor() as c:
                    c.execute("""
                        SELECT id FROM break_logs
                        WHERE employee_id = %s AND break_end IS NULL
                          AND break_start >= NOW() - INTERVAL 24 HOUR
                        LIMIT 1
                    """, (employee_id,))
                    if c.fetchone():
                        return jsonify({'success': False,
                                        'error': 'You already have an active break'})

                    shift_date = _shift_date(db, employee_id)

                    c.execute("""
                        SELECT account FROM gsheet_employees
                        WHERE employee_id = %s AND status = 'Active' LIMIT 1
                    """, (employee_id,))
                    emp = c.fetchone()
                    account_type = emp['account'] if emp else 'Unknown'

                    now = datetime.now()
                    c.execute("""
                        INSERT INTO break_logs (employee_id, employee_name, account_type, break_start, shift_date)
                        VALUES (%s, %s, %s, %s, %s)
                    """, (employee_id, session['user'].get('name', ''), account_type, now, shift_date))
                    db.commit()
                    log_id = c.lastrowid
            finally:
                db.close()

            return jsonify({
                'success': True,
                'message': 'Break started',
                'break_start': now.isoformat(),
                'log_id': log_id
            })

        elif action == 'end':
            db = get_central_db()
            try:
                with db.cursor() as c:
                    c.execute("""
                        SELECT id, break_start FROM break_logs
                        WHERE employee_id = %s AND break_end IS NULL
                          AND break_start >= NOW() - INTERVAL 24 HOUR
                        LIMIT 1
                    """, (employee_id,))
                    active = c.fetchone()
                    if not active:
                        return jsonify({'success': False, 'error': 'No active break found'})

                    now = datetime.now()
                    c.execute("UPDATE break_logs SET break_end = %s WHERE id = %s",
                              (now, active['id']))
                    db.commit()
                    duration = int((now - active['break_start']).total_seconds())
            finally:
                db.close()

            return jsonify({
                'success': True,
                'message': 'Break ended',
                'duration': duration
            })

        return jsonify({'success': False, 'error': 'Invalid action'})

    except Exception as e:
        import traceback
        return jsonify({'success': False, 'error': str(e), 'trace': traceback.format_exc()})


# ─────────────────────────────────────────────────────────────────────────────
# SUPERVISOR DASHBOARD PAGE
# ─────────────────────────────────────────────────────────────────────────────
@break_log_bp.route('/supervisor-break-logs')
@supervisor_required
def supervisor_break_logs():
    from app import get_central_db

    now = datetime.now()
    default_date = (
        (now - timedelta(days=1)).strftime('%Y-%m-%d')
        if now.hour < 9 else now.strftime('%Y-%m-%d')
    )
    filter_date = request.args.get('date', default_date)

    db = get_central_db()
    try:
        with db.cursor() as c:
            c.execute("""
                SELECT DISTINCT e.EmployeeID,
                       CONCAT(e.FirstName, ' ', e.LastName) AS name,
                       g.account
                FROM central_db.Employees e
                INNER JOIN gsheet_employees g
                  ON e.EmployeeID COLLATE utf8mb4_unicode_ci = g.employee_id
                WHERE g.account IN ('Arctic', 'Numa', 'GYG')
                  AND g.status = 'Active'
                ORDER BY g.account, name
            """)
            agents_list = c.fetchall()
    finally:
        db.close()

    return render_template('break_log/supervisor.html',
                           agents_list=agents_list,
                           filter_date=filter_date)


# ─────────────────────────────────────────────────────────────────────────────
# SUPERVISOR API
# Replaces api/supervisor-api.php
# ─────────────────────────────────────────────────────────────────────────────
@break_log_bp.route('/api/supervisor/breaks')
@supervisor_required
def supervisor_api():
    from app import get_central_db

    now = datetime.now()
    default_date = (
        (now - timedelta(days=1)).strftime('%Y-%m-%d')
        if now.hour < 9 else now.strftime('%Y-%m-%d')
    )

    # ── Build date condition from filter type ─────────────────────────────────
    filter_type = request.args.get('dateFilterType', 'specific')
    date_condition = ''
    date_params = []

    if filter_type == 'specific':
        d = request.args.get('date', default_date)
        date_condition = 'AND bl.shift_date = %s'
        date_params = [d]

    elif filter_type == 'last':
        n = int(request.args.get('lastNumber', 7))
        unit = request.args.get('lastUnit', 'days')
        delta_map = {'days': timedelta(days=n), 'weeks': timedelta(weeks=n),
                     'months': timedelta(days=n * 30), 'years': timedelta(days=n * 365)}
        from_date = (now - delta_map.get(unit, timedelta(days=n))).strftime('%Y-%m-%d')
        date_condition = 'AND bl.shift_date >= %s'
        date_params = [from_date]

    elif filter_type == 'range':
        date_condition = 'AND bl.shift_date BETWEEN %s AND %s'
        date_params = [
            request.args.get('startDate', default_date),
            request.args.get('endDate', default_date)
        ]

    elif filter_type == 'year':
        date_condition = 'AND YEAR(bl.shift_date) = %s'
        date_params = [request.args.get('yearNumber', now.year)]

    elif filter_type == 'month':
        date_condition = 'AND YEAR(bl.shift_date) = %s AND MONTH(bl.shift_date) = %s'
        date_params = [
            request.args.get('monthYear', now.year),
            request.args.get('monthSelect', now.month)
        ]

    elif filter_type == 'this':
        unit = request.args.get('thisUnit', 'day')
        if unit == 'day':
            date_condition = 'AND bl.shift_date = %s'
            date_params = [now.strftime('%Y-%m-%d')]
        elif unit == 'week':
            week_start = (now - timedelta(days=now.weekday())).strftime('%Y-%m-%d')
            date_condition = 'AND bl.shift_date >= %s'
            date_params = [week_start]
        elif unit == 'month':
            date_condition = 'AND YEAR(bl.shift_date) = %s AND MONTH(bl.shift_date) = %s'
            date_params = [now.year, now.month]
        elif unit == 'year':
            date_condition = 'AND YEAR(bl.shift_date) = %s'
            date_params = [now.year]

    # ── Agent / account filter ────────────────────────────────────────────────
    agent = request.args.get('agent', 'all')
    account = request.args.get('account', 'all')
    agent_condition = ''
    agent_params = []

    if agent != 'all':
        agent_condition += ' AND bl.employee_id = %s'
        agent_params.append(agent)
    if account != 'all':
        agent_condition += ' AND bl.account_type = %s'
        agent_params.append(account)

    db = get_central_db()
    try:
        with db.cursor() as c:
            # Active breaks (no date filter — always show current)
            c.execute(f"""
                SELECT bl.id, bl.employee_id, bl.break_start, bl.account_type,
                       CONCAT(e.FirstName, ' ', e.LastName) AS employee_name,
                       es.shift_time, es.is_rest_day
                FROM break_logs bl
                LEFT JOIN central_db.Employees e
                  ON bl.employee_id COLLATE utf8mb4_unicode_ci = e.EmployeeID
                LEFT JOIN employee_schedules es
                  ON bl.employee_id = es.employee_id AND bl.shift_date = es.schedule_date
                WHERE bl.break_end IS NULL
                  AND bl.break_start >= NOW() - INTERVAL 24 HOUR
                  {agent_condition}
                ORDER BY bl.break_start
            """, agent_params)
            active_breaks = c.fetchall()

            # Completed breaks (with date filter)
            c.execute(f"""
                SELECT bl.id, bl.employee_id, bl.shift_date, bl.break_start,
                       bl.break_end, bl.account_type,
                       TIMESTAMPDIFF(SECOND, bl.break_start, bl.break_end) AS duration,
                       CONCAT(e.FirstName, ' ', e.LastName) AS employee_name,
                       es.shift_time, es.is_rest_day
                FROM break_logs bl
                LEFT JOIN central_db.Employees e
                  ON bl.employee_id COLLATE utf8mb4_unicode_ci = e.EmployeeID
                LEFT JOIN employee_schedules es
                  ON bl.employee_id = es.employee_id AND bl.shift_date = es.schedule_date
                WHERE bl.break_end IS NOT NULL
                  {date_condition}
                  {agent_condition}
                ORDER BY bl.employee_id, bl.break_start
            """, date_params + agent_params)
            completed = c.fetchall()
    finally:
        db.close()

    # ── CSV export ────────────────────────────────────────────────────────────
    if request.args.get('export') == 'csv':
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(['Date', 'Agent', 'Account', 'Schedule',
                    'Break Start', 'Break End', 'Duration (s)'])
        for r in completed:
            w.writerow([r['shift_date'], r['employee_name'], r['account_type'],
                        r['shift_time'] or '', r['break_start'],
                        r['break_end'], r['duration']])
        out.seek(0)
        return Response(out.getvalue(), mimetype='text/csv',
                        headers={'Content-Disposition':
                                 'attachment;filename=break_logs.csv'})

    # ── Stats ─────────────────────────────────────────────────────────────────
    total_time = sum(r['duration'] or 0 for r in completed)
    avg = total_time // len(completed) if completed else 0

    return jsonify({
        'success': True,
        'active_breaks':    [_serialize_row(r) for r in active_breaks],
        'completed_breaks': [_serialize_row(r) for r in completed],
        'stats': {
            'active_count':  len(active_breaks),
            'total_breaks':  len(completed),
            'total_time':    total_time,
            'avg_duration':  avg,
        }
    })