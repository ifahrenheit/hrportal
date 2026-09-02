"""
TL View blueprint - read-only leave/request visibility for Team Leads.

Access rules:
  - A non-admin TL (session['is_tl']) is locked to their own team
    (session['tl_name']), no team selector shown.
  - An admin (session['is_admin']) — whether or not they are also a TL —
    sees a team selector populated from tl_view_map, and must pick a team
    before any records are shown. This lets admins spot-check any TL's
    view without using Login As.
  - Anyone who is neither admin nor TL gets 403.

This blueprint is intentionally separate from the admin all-leaves /
file-requests routes: it shares query shape but not code, so there is no
path by which an approve/reject/cancel/delete action could ever leak into
a TL-facing template. Every route here renders templates that contain no
action buttons or forms that mutate data.
"""
import os
from functools import wraps
from flask import Blueprint, render_template, request, session, abort, redirect, url_for
import pymysql
import pymysql.cursors

tl_view_bp = Blueprint('tl_view', __name__, url_prefix='/tl-view')


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def get_db():
    """Connection to orangehrm2 (leave4day_requests, hs_hr_employee, ohrm_leave_type live here)."""
    return pymysql.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        user=os.environ.get("DB_USER", "employee_sync"),
        password=os.environ.get("DB_PASSWORD", ""),
        database=os.environ.get("DB_NAME", "orangehrm2"),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor
    )


def get_central_db():
    """Connection to central_db (gsheet_employees, tl_view_map live here)."""
    return pymysql.connect(
        host=os.environ.get("MAIN_DB_HOST", "localhost"),
        user=os.environ.get("MAIN_DB_USER", "employee_sync"),
        password=os.environ.get("MAIN_DB_PASSWORD", ""),
        database=os.environ.get("MAIN_DB_NAME", "central_db"),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor
    )


def _require_tl_or_admin():
    if not (session.get('is_admin') or session.get('is_tl')):
        abort(403)


@tl_view_bp.route('/')
@login_required
def tl_view_index():
    _require_tl_or_admin()
    return redirect(url_for('tl_view.tl_view_leave'))


@tl_view_bp.route('/leave')
@login_required
def tl_view_leave():
    _require_tl_or_admin()

    is_admin = session.get('is_admin', False)

    # Admins (whether or not they're also a TL) get a team selector and
    # must explicitly pick a team. Non-admin TLs are locked to their own.
    tl_list = []
    if is_admin:
        tl_name = request.args.get('tl', '')
        cdb = get_central_db()
        try:
            with cdb.cursor() as cc:
                cc.execute("SELECT tl_name FROM tl_view_map ORDER BY tl_name")
                tl_list = [r['tl_name'] for r in cc.fetchall()]
        finally:
            cdb.close()
    else:
        tl_name = session.get('tl_name', '')

    date_from  = request.args.get('date_from', '')
    date_to    = request.args.get('date_to', '')
    status     = request.args.get('status', '')
    leave_type = request.args.get('leave_type', '')
    emp_search = request.args.get('emp_search', '')

    records = []
    leave_types = []
    emp_display = emp_search

    db = get_db()
    try:
        with db.cursor() as c:
            c.execute("SELECT id, name FROM ohrm_leave_type ORDER BY name")
            leave_types = c.fetchall()

            # Only query records once a team is established (own team for
            # a TL, or admin's explicit selection)
            if tl_name:
                where  = ["g.TL = %s"]
                params = [tl_name]

                if date_from:
                    where.append("r.leave_date >= %s")
                    params.append(date_from)
                if date_to:
                    where.append("r.leave_date <= %s")
                    params.append(date_to)
                if status:
                    where.append("r.status = %s")
                    params.append(status)
                if leave_type:
                    where.append("r.leave_type_id = %s")
                    params.append(leave_type)
                if emp_search:
                    where.append("(e.emp_firstname LIKE %s OR e.emp_lastname LIKE %s OR e.employee_id LIKE %s)")
                    params.extend([f'%{emp_search}%', f'%{emp_search}%', f'%{emp_search}%'])

                query = f"""
                    SELECT r.*, lt.name AS leave_type_name,
                        e.emp_firstname, e.emp_lastname, e.employee_id,
                        g.TL, g.group_name
                    FROM leave4day_requests r
                    JOIN ohrm_leave_type lt ON r.leave_type_id = lt.id
                    JOIN hs_hr_employee e   ON r.emp_number = e.emp_number
                    LEFT JOIN central_db.gsheet_employees g ON g.employee_id COLLATE utf8mb4_unicode_ci = e.employee_id
                    WHERE {' AND '.join(where)}
                    ORDER BY r.filed_at DESC
                    LIMIT 500
                """
                c.execute(query, params)
                records = c.fetchall()
    finally:
        db.close()

    if emp_search and records:
        try:
            cdb2 = get_central_db()
            with cdb2.cursor() as cc:
                cc.execute('SELECT schedule_name FROM gsheet_employees WHERE employee_id = %s LIMIT 1', (emp_search,))
                gs = cc.fetchone()
                emp_display = gs['schedule_name'] if gs and gs['schedule_name'] else (records[0]['emp_firstname'] + ' ' + records[0]['emp_lastname'])
            cdb2.close()
        except Exception:
            emp_display = records[0]['emp_firstname'] + ' ' + records[0]['emp_lastname']

    return render_template('tl_view/leave.html',
                           records=records, user=session['user'],
                           leave_types=leave_types, tl_name=tl_name,
                           is_admin=is_admin, tl_list=tl_list,
                           date_from=date_from, date_to=date_to,
                           status=status, leave_type=leave_type,
                           emp_search=emp_search, emp_display=emp_display)

@tl_view_bp.route('/requests')
@login_required
def tl_view_requests():
    _require_tl_or_admin()

    is_admin = session.get('is_admin', False)

    tl_list = []
    if is_admin:
        tl_name = request.args.get('tl', '')
        cdb0 = get_central_db()
        try:
            with cdb0.cursor() as cc:
                cc.execute("SELECT tl_name FROM tl_view_map ORDER BY tl_name")
                tl_list = [r['tl_name'] for r in cc.fetchall()]
        finally:
            cdb0.close()
    else:
        tl_name = session.get('tl_name', '')

    from datetime import date
    date_from  = request.args.get('date_from', date.today().strftime('%Y-01-01'))
    date_to    = request.args.get('date_to', date.today().strftime('%Y-%m-%d'))
    status_f   = request.args.get('status', '')
    req_type   = request.args.get('req_type', '')
    emp_search = request.args.get('emp_search', '')

    records = []
    emp_display = emp_search

    if tl_name:
        def build_filters(date_col, tbl_alias='t'):
            where  = [f"{tbl_alias}.deleted_at IS NULL"] if status_f != 'Deleted' else []
            params = []
            if date_from:
                where.append(f"{date_col} >= %s"); params.append(date_from)
            if date_to:
                where.append(f"{date_col} <= %s"); params.append(date_to)
            if status_f:
                where.append(f"{tbl_alias}.status = %s"); params.append(status_f)
            if emp_search:
                where.append("(g.schedule_name LIKE %s OR g.employee_id LIKE %s)")
                params += [f'%{emp_search}%', f'%{emp_search}%']
            where.append("g.tl = %s")
            params.append(tl_name)
            return ' AND '.join(where), params

        cdb = get_central_db()
        try:
            with cdb.cursor() as c:

                if not req_type or req_type == 'FTS':
                    w, p = build_filters('f.fts_date', 'f')
                    c.execute(f"""
                        SELECT 'FTS' AS req_type, f.id, f.employeeID AS emp_id,
                               COALESCE(g.schedule_name, f.employee_name) AS employee_name,
                               f.fts_date AS req_date,
                               CONCAT(f.fts_type, ' @ ', TIME_FORMAT(f.fts_time,'%%H:%%i')) AS details,
                               f.status, f.created_at, f.approved_at, f.approver_name,
                               g.tl AS TL, g.group_name
                        FROM fts_requests f
                        LEFT JOIN gsheet_employees g ON g.employee_id = f.employeeID COLLATE utf8mb4_unicode_ci
                        WHERE {w}
                    """, p)
                    records.extend(c.fetchall())

                if not req_type or req_type == 'OT':
                    w, p = build_filters('o.ot_date', 'o')
                    c.execute(f"""
                        SELECT 'OT' AS req_type, o.id, o.employee_id AS emp_id,
                               COALESCE(g.schedule_name, o.employee_id) AS employee_name,
                               o.ot_date AS req_date,
                               CONCAT(o.ot_type, ' ',
                                      TIME_FORMAT(o.start_time,'%%H:%%i'), '-',
                                      TIME_FORMAT(o.end_time,'%%H:%%i')) AS details,
                               o.status, o.timestamp AS created_at, o.approved_at, o.approver_name,
                               g.tl AS TL, g.group_name
                        FROM ot_requests o
                        LEFT JOIN gsheet_employees g ON g.employee_id = o.employee_id COLLATE utf8mb4_unicode_ci
                        WHERE {w}
                    """, p)
                    records.extend(c.fetchall())

                if not req_type or req_type == 'CWS':
                    w, p = build_filters('c.original_date', 'c')
                    c.execute(f"""
                        SELECT 'CWS' AS req_type, c.id, c.employee_id AS emp_id,
                               COALESCE(g.schedule_name, c.employee_id) AS employee_name,
                               c.original_date AS req_date,
                               CONCAT(c.original_date, ' ', c.original_time,
                                      ' > ', c.new_date, ' ', c.new_time) AS details,
                               c.status, c.created_at, c.approved_at, c.approver_name,
                               g.tl AS TL, g.group_name
                        FROM cws_requests c
                        LEFT JOIN gsheet_employees g ON g.employee_id = c.employee_id COLLATE utf8mb4_unicode_ci
                        WHERE {w}
                    """, p)
                    records.extend(c.fetchall())

                if not req_type or req_type == 'RDW':
                    w, p = build_filters('r.rd_date', 'r')
                    c.execute(f"""
                        SELECT 'RDW' AS req_type, r.id, r.employee_id AS emp_id,
                               COALESCE(g.schedule_name, r.employee_id) AS employee_name,
                               r.rd_date AS req_date,
                               CONCAT(r.work_category, ' ',
                                      TIME_FORMAT(r.start_time,'%%H:%%i'), '-',
                                      TIME_FORMAT(r.end_time,'%%H:%%i')) AS details,
                               r.status, r.created_at, r.approved_at, r.approver_name,
                               g.tl AS TL, g.group_name
                        FROM rd_requests r
                        LEFT JOIN gsheet_employees g ON g.employee_id = r.employee_id COLLATE utf8mb4_unicode_ci
                        WHERE {w}
                    """, p)
                    records.extend(c.fetchall())

                if not req_type or req_type == 'MAGIC_CWS':
                    # magic_cws_requests has no deleted_at column (deletion is
                    # tracked via status='Deleted' on the enum itself), and no
                    # created_at (it's filed_at) or approver_name text column
                    # (it's approved_by, an emp_number int) -- so this table
                    # gets its own filter builder rather than the shared one.
                    mw = []
                    mp = []
                    if status_f == 'Deleted':
                        mw.append("m.status = 'Deleted'")
                    elif status_f:
                        mw.append("m.status = %s"); mp.append(status_f)
                    else:
                        mw.append("m.status != 'Deleted'")
                    if date_from:
                        mw.append("m.original_date >= %s"); mp.append(date_from)
                    if date_to:
                        mw.append("m.original_date <= %s"); mp.append(date_to)
                    if emp_search:
                        mw.append("(g.schedule_name LIKE %s OR g.employee_id LIKE %s)")
                        mp += [f'%{emp_search}%', f'%{emp_search}%']
                    mw.append("g.tl = %s")
                    mp.append(tl_name)
                    mwhere = ' AND '.join(mw)
                    c.execute(f"""
                        SELECT 'MAGIC_CWS' AS req_type, m.id, m.employee_id AS emp_id,
                               COALESCE(g.schedule_name, m.employee_name) AS employee_name,
                               m.original_date AS req_date,
                               CONCAT(m.original_shift, ' (', m.original_date, ') > ',
                                      m.new_shift, ' (', m.new_date, ')') AS details,
                               m.status, m.filed_at AS created_at, m.approved_at,
                               NULL AS approver_name,
                               g.tl AS TL, g.group_name
                        FROM magic_cws_requests m
                        LEFT JOIN gsheet_employees g ON g.employee_id = m.employee_id COLLATE utf8mb4_unicode_ci
                        WHERE {mwhere}
                    """, mp)
                    records.extend(c.fetchall())

        finally:
            cdb.close()

        records.sort(key=lambda r: -(r['req_date'].toordinal() if r.get('req_date') else 0))

        if emp_search and records:
            try:
                cdb2 = get_central_db()
                with cdb2.cursor() as cc:
                    cc.execute('SELECT schedule_name FROM gsheet_employees WHERE employee_id = %s LIMIT 1', (emp_search,))
                    gs = cc.fetchone()
                    emp_display = gs['schedule_name'] if gs and gs['schedule_name'] else emp_search
                cdb2.close()
            except Exception:
                pass

    return render_template('tl_view/requests.html',
                           records=records, user=session['user'],
                           tl_name=tl_name, is_admin=is_admin, tl_list=tl_list,
                           date_from=date_from, date_to=date_to,
                           status=status_f, req_type=req_type,
                           emp_search=emp_search, emp_display=emp_display)