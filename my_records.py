"""
my_records.py
-------------
Agent-facing self-service view. Every authenticated employee sees ONLY
their own tardiness and overbreak records. No sub-admin permission is
required -- the ONLY authorization is the per-employee WHERE clause,
always scoped to the logged-in agent's own identity from the session.
No employee ID is ever read from the request.

Unified across accounts:
  - Tardiness  : same biometric pipeline for everyone (get_late_records_for_range)
  - Overbreak  : Numa/Arctic derive from break_logs (live break tool, >90 min/day);
                 all other accounts read overbreak_records (Google-Sheets synced).
  The overbreak source is auto-detected from the agent's gsheet_employees.account.
"""

from functools import wraps
from datetime import date

from flask import (
    Blueprint, render_template, request, session, redirect, url_for, abort
)

from db_core import get_db_connection

# Reuse the admin tardiness engine + payroll helpers verbatim.
from tardiness import get_late_records_for_range
from payroll_period import (
    get_previous_period,
    get_next_period,
    get_default_payroll_period,
)

my_records_bp = Blueprint("my_records", __name__, url_prefix="/my")

# Break-tool constants (mirror break_log/routes.py).
OVERBREAK_LIMIT_MIN = 90
OVERBREAK_LIMIT_SEC = OVERBREAK_LIMIT_MIN * 60
BREAKLOG_ACCOUNTS = ("Numa", "Arctic", "Test")


def login_required(f):
    """Self-contained login gate (matches floor_map / tl_view convention).
    Requires a valid session only -- NO sub-admin permission."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper


def _resolve_agent():
    """Resolve the logged-in agent from the SESSION email only.
    Returns dict with employee_id + account, or None if unresolved
    (e.g. portal-user fallback login with no linked employee)."""
    email = (session.get("user") or {}).get("email", "")
    if not email:
        return None
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT employee_id, account
            FROM gsheet_employees
            WHERE LOWER(email) = %s AND status = 'Active'
            LIMIT 1
            """,
            (email.lower(),),
        )
        row = cur.fetchone()
        if not row:
            return None
        if isinstance(row, dict):
            return {"employee_id": row["employee_id"], "account": row["account"]}
        return {"employee_id": row[0], "account": row[1]}
    finally:
        cur.close()
        conn.close()


def _resolve_companyid(emp_id):
    """Tardiness keys on userdata.companyid; usually == employee_id but can
    diverge. Fall back to emp_id when no active userdata row exists."""
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT companyid FROM userdata WHERE companyid = %s AND active = 1 LIMIT 1",
            (emp_id,),
        )
        row = cur.fetchone()
        if row:
            return row["companyid"] if isinstance(row, dict) else row[0]
        return emp_id
    finally:
        cur.close()
        conn.close()


def _overbreaks_from_breaklogs(emp_id, period_start, period_end):
    """Numa/Arctic: derive per-day overbreaks from the live break tool.
    Simplified for agents -- any day over the 90-min allowance, with
    minutes over. No cycle counting, no review status, no IR numbers."""
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT shift_date,
                   CAST(ROUND(SUM(duration)/60) AS SIGNED)      AS total_minutes,
                   CAST(ROUND(SUM(duration)/60) - %s AS SIGNED) AS over_minutes
            FROM break_logs
            WHERE employee_id = %s
              AND break_end IS NOT NULL
              AND shift_date BETWEEN %s AND %s
            GROUP BY shift_date
            HAVING SUM(duration) > %s
            ORDER BY shift_date DESC
            """,
            (OVERBREAK_LIMIT_MIN, emp_id,
             period_start.isoformat(), period_end.isoformat(),
             OVERBREAK_LIMIT_SEC),
        )
        out = []
        for r in cur.fetchall():
            r = r if isinstance(r, dict) else {
                "shift_date": r[0], "total_minutes": r[1], "over_minutes": r[2]
            }
            out.append({
                "record_date": r["shift_date"],
                "over_minutes": r["over_minutes"],
                "total_minutes": r["total_minutes"],
            })
        return out
    finally:
        cur.close()
        conn.close()


def _overbreaks_from_records(emp_id):
    """Non-Numa/Arctic: read the Google-Sheets synced overbreak_records.
    Trimmed -- no IR info, no TL, no batch/sync internals."""
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT record_date, break_duration, validity,
                   payroll_month, payroll_cycle
            FROM overbreak_records
            WHERE employee_id = %s
            ORDER BY record_date DESC
            """,
            (emp_id,),
        )
        return cur.fetchall()
    finally:
        cur.close()
        conn.close()


@my_records_bp.route("/records")
@login_required
def records():
    agent = _resolve_agent()
    if not agent:
        abort(403)

    emp_id = agent["employee_id"]
    account = (agent["account"] or "").strip()
    uses_breaklog = account in BREAKLOG_ACCOUNTS

    active_tab = request.args.get("tab", "tardiness")
    if active_tab not in ("tardiness", "overbreak"):
        active_tab = "tardiness"

    p_from = request.args.get("from")
    p_to = request.args.get("to")
    if p_from and p_to:
        try:
            period_start = date.fromisoformat(p_from)
            period_end = date.fromisoformat(p_to)
        except ValueError:
            period_start, period_end = get_default_payroll_period()
    else:
        period_start, period_end = get_default_payroll_period()

    nav = request.args.get("nav")
    if nav == "prev":
        period_start, period_end = get_previous_period(period_start, period_end)
    elif nav == "next":
        period_start, period_end = get_next_period(period_start, period_end)

    my_companyid = _resolve_companyid(emp_id)
    all_late = get_late_records_for_range(period_start, period_end)
    late_records = [r for r in all_late if r.get("companyid") == my_companyid]
    late_records.sort(key=lambda r: r["record_date"], reverse=True)

    if uses_breaklog:
        overbreak_records = _overbreaks_from_breaklogs(emp_id, period_start, period_end)
    else:
        overbreak_records = _overbreaks_from_records(emp_id)

    return render_template(
        "my_records.html",
        active_tab=active_tab,
        late_records=late_records,
        overbreak_records=overbreak_records,
        overbreak_is_breaklog=uses_breaklog,
        period_start=period_start,
        period_end=period_end,
        account=account,
    )