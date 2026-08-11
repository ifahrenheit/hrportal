# csr_percentage.py
# Blueprint for the CSR Percentage report.
#
# Business rule: a DSAT counts as the agent's CSR failure only when
#     root_cause = 'CSR'  AND  rep_responsible IS NULL / empty
# If rep_responsible names someone, QA attributed that DSAT to that other rep,
# so it must NOT count against the agent whose email is on the response.
#
# Self-contained by design: the scope/email helpers are duplicated from app.py
# rather than imported, because blueprints must not import from `app`
# (circular import). Keep _csr_scope() in sync with app.py's _csat_scope()
# if the CSAT permission model changes.
from datetime import date
from functools import wraps

from flask import (
    Blueprint, render_template, request, jsonify, session,
    redirect, url_for, flash, g
)
import pymysql
import pymysql.cursors

from db_core import get_db_connection

csr_bp = Blueprint('csr', __name__, url_prefix='/csr-percentage')


# ---------------------------------------------------------------------------
# Auth helpers (local — do not import from app)
# ---------------------------------------------------------------------------
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get('user'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return wrapper


def _has_perm(name):
    return bool(session.get('permissions', {}).get(name))


def _csr_scope():
    """(allowed, tl_name): admin/can_csat/SOM -> (True, None); CSAT TL -> (True, tl_name)."""
    if not session.get('user'):
        return (False, None)
    if session.get('is_admin') or _has_perm('can_csat') or session.get('is_supervisor'):
        return (True, None)
    email = session['user'].get('email')
    if not email:
        return (False, None)
    conn = get_db_connection()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as c:
            c.execute("SELECT tl_name FROM csat_tl_map WHERE login_email = %s", (email,))
            row = c.fetchone()
    finally:
        conn.close()
    if row:
        return (True, row['tl_name'] if isinstance(row, dict) else row[0])
    return (False, None)


def _active_emails(conn):
    """Currently-Active agent emails (lowercased). Cached per request on g."""
    if hasattr(g, '_csr_active_emails'):
        return g._csr_active_emails
    with conn.cursor(pymysql.cursors.DictCursor) as c:
        c.execute("""
            SELECT LOWER(TRIM(email)) AS em
            FROM gsheet_employees
            WHERE status='Active' AND email IS NOT NULL AND email<>''
        """)
        emails = [r['em'] for r in c.fetchall() if r['em']]
    g._csr_active_emails = emails
    return emails


def _cs_emails(conn):
    """CS-group agent emails (lowercased). Cached per request on g."""
    if hasattr(g, '_csr_cs_emails'):
        return g._csr_cs_emails
    with conn.cursor(pymysql.cursors.DictCursor) as c:
        c.execute("""
            SELECT DISTINCT LOWER(TRIM(agent_email)) AS em
            FROM csat_responses
            WHERE group_name='CS' AND agent_email IS NOT NULL AND agent_email<>''
        """)
        emails = [r['em'] for r in c.fetchall() if r['em']]
    g._csr_cs_emails = emails
    return emails


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------
@csr_bp.route('/')
@login_required
def page():
    allowed, tl_name = _csr_scope()
    if not allowed:
        flash('You do not have permission to access the CSR Percentage report.', 'danger')
        return redirect(url_for('dashboard'))
    return render_template('csr_percentage.html',
                           user=session['user'],
                           is_admin=session.get('is_admin', False),
                           is_tl_view=(tl_name is not None),
                           scoped_tl=tl_name)


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------
@csr_bp.route('/data')
@login_required
def data():
    allowed, tl_name = _csr_scope()
    if not allowed:
        return jsonify({"error": "forbidden"}), 403

    today = date.today()
    start = request.args.get('start') or today.replace(month=1, day=1).isoformat()
    end   = request.args.get('end') or today.isoformat()

    # granularity: month (default) | week | day
    gran = (request.args.get('gran') or 'month').lower()
    if gran not in ('month', 'week', 'day'):
        gran = 'month'
    if gran == 'week':
        # use the source `week` column (plain number, e.g. "23"), prefixed with the
        # year so periods sort correctly and stay unique across year boundaries
        period_expr = "CONCAT(YEAR(cr.performed_at_date), '-W', LPAD(cr.week, 2, '0'))"
    elif gran == 'day':
        period_expr = "DATE_FORMAT(cr.performed_at_date, '%%Y-%%m-%%d')"
    else:
        period_expr = "DATE_FORMAT(cr.performed_at_date, '%%Y-%%m')"

    conds  = ["cr.csat_score = 0", "cr.performed_at_date BETWEEN %s AND %s"]
    params = [start, end]

    if tl_name:
        conds.append("cr.tl = %s")
        params.append(tl_name)
    else:
        f_tl = request.args.get('tl')
        if f_tl and f_tl != 'All':
            tls = [t.strip() for t in f_tl.split(',') if t.strip() and t.strip() != 'All']
            if tls:
                conds.append("cr.tl IN (" + ','.join(['%s'] * len(tls)) + ")")
                params.extend(tls)

    f_batch = request.args.get('batch')
    if f_batch and f_batch != 'All':
        batches = [b.strip() for b in f_batch.split(',') if b.strip() and b.strip() != 'All']
        if batches:
            conds.append("cr.batch IN (" + ','.join(['%s'] * len(batches)) + ")")
            params.extend(batches)

    conn = get_db_connection()
    try:
        # scope filters (default ON, same convention as the CSAT dashboard)
        if request.args.get('active_only', '1') not in ('0', 'false', 'off', 'no'):
            act = _active_emails(conn)
            if act:
                conds.append("LOWER(cr.agent_email) IN (" + ','.join(['%s'] * len(act)) + ")")
                params.extend(act)
            else:
                conds.append("1=0")

        if request.args.get('cs_only', '1') not in ('0', 'false', 'off', 'no'):
            cs = _cs_emails(conn)
            if cs:
                conds.append("LOWER(cr.agent_email) IN (" + ','.join(['%s'] * len(cs)) + ")")
                params.extend(cs)
            else:
                conds.append("1=0")

        where = " AND ".join(conds)

        with conn.cursor(pymysql.cursors.DictCursor) as c:
            c.execute(f"""
                SELECT cr.agent_email AS email,
                       {period_expr} AS ym,
                       MAX(cr.tl) AS tl,
                       MAX(cr.batch) AS batch,
                       COUNT(*) AS all_dsats,
                       SUM(cr.root_cause = 'CSR'
                           AND (cr.rep_responsible IS NULL OR cr.rep_responsible = '')) AS csr_dsats
                FROM csat_responses cr
                WHERE {where}
                GROUP BY cr.agent_email, {period_expr}
            """, params)
            rows = c.fetchall()

            agents = {}
            months = set()
            for r in rows:
                em, ym = r['email'], r['ym']
                months.add(ym)
                a = agents.setdefault(em, {
                    "email": em, "tl": r['tl'], "batch": r['batch'],
                    "months": {}, "tot_csr": 0, "tot_dsats": 0
                })
                if not a["tl"] and r['tl']:
                    a["tl"] = r['tl']
                dsats = int(r['all_dsats'] or 0)
                csr   = int(r['csr_dsats'] or 0)
                a["months"][ym] = {
                    "csr": csr,
                    "dsats": dsats,
                    "pct": round(100.0 * csr / dsats, 2) if dsats else None
                }
                a["tot_csr"]   += csr
                a["tot_dsats"] += dsats

            # display names from gsheet_employees
            if agents:
                emails = list(agents.keys())
                ph = ','.join(['%s'] * len(emails))
                c.execute(
                    "SELECT LOWER(TRIM(email)) AS em, schedule_name "
                    "FROM gsheet_employees WHERE LOWER(TRIM(email)) IN (" + ph + ")",
                    [e.lower().strip() for e in emails]
                )
                name_map = {row['em']: row['schedule_name'] for row in c.fetchall()}
                for em, a in agents.items():
                    a["name"] = name_map.get((em or '').lower().strip()) or em

        out = []
        for em, a in agents.items():
            a["total_pct"] = (round(100.0 * a["tot_csr"] / a["tot_dsats"], 2)
                              if a["tot_dsats"] else None)
            out.append(a)
        out.sort(key=lambda x: (x.get("name") or x["email"]).lower())

        return jsonify({
            "range": {"start": start, "end": end},
            "gran": gran,
            "periods": sorted(months),
            "agents": out
        })
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Drill-down: DSAT details for one agent + one period (for coaching)
# ---------------------------------------------------------------------------
@csr_bp.route('/responses')
@login_required
def responses():
    allowed, tl_name = _csr_scope()
    if not allowed:
        return jsonify({"error": "forbidden"}), 403

    email = (request.args.get('email') or '').strip()
    period = (request.args.get('period') or '').strip()   # e.g. 2026-04 | 2026-W23 | 2026-07-24
    gran = (request.args.get('gran') or 'month').lower()
    if not email or not period:
        return jsonify({"error": "email and period required"}), 400

    # translate the period token into a WHERE condition on performed_at_date / week
    conds = ["cr.csat_score = 0", "LOWER(cr.agent_email) = %s"]
    params = [email.lower()]

    if gran == 'week':
        # period looks like '2026-W23'
        try:
            yr, wk = period.split('-W')
            conds.append("YEAR(cr.performed_at_date) = %s AND cr.week = %s")
            params.extend([int(yr), int(wk)])
        except Exception:
            return jsonify({"error": "bad week period"}), 400
    elif gran == 'day':
        conds.append("cr.performed_at_date = %s")
        params.append(period)
    else:  # month, '2026-04'
        conds.append("DATE_FORMAT(cr.performed_at_date, '%%Y-%%m') = %s")
        params.append(period)

    # TL-scoped users only see their own team
    if tl_name:
        conds.append("cr.tl = %s")
        params.append(tl_name)

    where = " AND ".join(conds)

    conn = get_db_connection()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as c:
            c.execute(f"""
                SELECT cr.performed_at_date AS date, cr.ticket_id, cr.channel_type,
                       cr.csat_rate, cr.root_cause, cr.rep_responsible,
                       cr.qa_comment, cr.qa_name
                FROM csat_responses cr
                WHERE {where}
                ORDER BY cr.performed_at_date, cr.ticket_id
            """, params)
            rows = c.fetchall()

        out = []
        for r in rows:
            rep = (r.get('rep_responsible') or '').strip()
            is_csr = (r.get('root_cause') == 'CSR') and (rep == '')
            out.append({
                "date": r['date'].isoformat() if r['date'] else '',
                "ticket_id": r['ticket_id'],
                "channel": r['channel_type'],
                "rate": r['csat_rate'],
                "root_cause": r.get('root_cause') or '',
                "rep_responsible": rep,
                "qa_comment": r.get('qa_comment') or '',
                "qa_name": r.get('qa_name') or '',
                "is_csr": is_csr
            })

        return jsonify({
            "email": email, "period": period, "gran": gran,
            "total": len(out),
            "csr_count": sum(1 for x in out if x["is_csr"]),
            "responses": out
        })
    finally:
        conn.close()