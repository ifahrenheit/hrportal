"""
Coaching blueprint for the Cohere HR portal (leavesystem).

TRUE DUAL-RUN with the existing PHP coaching page. Both apps share the same
`coaching_sessions` table in central_db. Because Employees.EmployeeID,
coaching_sessions.agent_id, and gsheet_employees.employee_id are all the SAME
YYMMDD-NN value, there is no ID mapping to do:

  - PHP writes agent_id / supervisor_id (varchar YYMMDD-NN).
  - Portal reads/writes those SAME columns. A row created by either side is
    immediately visible to the other. No sync, no resolver.
  - The table's spare agent_employee_id / supervisor_employee_id columns are
    kept in step (portal writes the same value) but nothing depends on them.

Compatibility notes:
  - PHP has no deleted_at. To keep both sides consistent, the portal "delete"
    sets status='cancelled' (already in the PHP status enum) instead of
    introducing a soft-delete column.
  - Display name comes from gsheet_employees.schedule_name.
  - coaching_sessions / Employees are utf8mb4_0900_ai_ci; gsheet_employees is
    utf8mb4_unicode_ci, so every JOIN adds COLLATE utf8mb4_unicode_ci on the
    gsheet side.

Register in app.py:
    from coaching import coaching_bp
    app.register_blueprint(coaching_bp)

Requires get_db_connection() (central_db) already defined in app.py.
"""

import io
import csv
import pymysql
from datetime import datetime
from functools import wraps
from flask import (
    Blueprint, render_template, request, redirect, url_for,
    session, flash, abort, Response,
)

coaching_bp = Blueprint(
    "coaching", __name__,
    url_prefix="/coaching",
    template_folder="templates",
)

COACHING_TYPES = {
    "performance": "Performance",
    "behavioral": "Behavioral",
    "skill_development": "Skill Development",
    "quality": "Quality",
    "other": "Other",
}

# Portal status set offered in the TL/SOM dropdown.
STATUSES = {
    "completed": "Completed",
    "pending": "Pending",
    "for_followup": "For Follow-up",
}

# Legacy values (from the PHP era) still shown correctly for old rows,
# but not offered in the dropdown.
STATUS_LABELS_ALL = {
    "completed": "Completed",
    "pending": "Pending",
    "for_followup": "For Follow-up",
    "pending_followup": "For Follow-up",   # legacy alias
    "cancelled": "Cancelled",              # legacy
}

# Status -> inline style using theme vars (works light + dark).
STATUS_BADGE = {
    "completed":        "background:var(--ok,#16a34a);color:#fff;",
    "pending":          "background:var(--danger,#dc2626);color:#fff;",
    "for_followup":     "background:#c05621;color:#fff;",
    "pending_followup": "background:#c05621;color:#fff;",   # legacy alias
    "cancelled":        "background:var(--surface-2,#e2e8f0);color:var(--text-muted,#6b7280);",
}

# Collation applied to the gsheet_employees side of every join.
GC = "COLLATE utf8mb4_unicode_ci"


@coaching_bp.app_context_processor
def _inject_status_badge():
    return {
        "coaching_status_style": STATUS_BADGE,
        "coaching_status_label": STATUS_LABELS_ALL,
        "is_manager": can_reports(),
    }


# --- Connection + access control -----------------------------------------
def _db():
    from app import get_db_connection
    return get_db_connection()


def _perms():
    return session.get("permissions", {}) or {}


def can_coaching():
    """Create / view own sessions. Gated purely on the can_coaching
    permission (granted to the 9 TLs, Finest, BO/L2 TLs, etc.), with
    admin as the universal override. Nav gate must match this exactly."""
    return bool(session.get("is_admin") or _perms().get("can_coaching"))


def can_reports():
    """Manager equivalent: all sessions + reports."""
    return bool(session.get("is_admin") or _perms().get("can_coaching_reports"))


def coaching_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("login"))
        if not (can_coaching() or can_reports()):
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def reports_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("login"))
        if not can_reports():
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def _me():
    return session["user"]["employee_id"]


def _scope(alias="cs"):
    """Three-tier visibility:
      - can_reports (SOM/QA/Finest/admin) -> ALL sessions
      - TL (session tl_name) -> all sessions for agents on THEIR team
        (gsheet_employees.tl = their tl_name)
      - fallback -> only sessions they personally conducted
    """
    if can_reports():
        return "", []
    tl_name = session.get("tl_name")
    if tl_name:
        return (
            f" AND {alias}.agent_id {GC} IN "
            f"(SELECT employee_id {GC} FROM gsheet_employees "
            f"WHERE tl = %s) ",
            [tl_name],
        )
    return f" AND {alias}.supervisor_id = %s ", [_me()]


# --- Roster helpers -------------------------------------------------------
def _agents():
    """Active roster for the agent dropdown.
    can_reports (SOM/QA/Finest/admin) -> everyone.
    TL -> only their own team (gsheet tl = their tl_name).
    """
    tl_name = session.get("tl_name")
    scope_sql, scope_params = "", []
    if not can_reports() and tl_name:
        scope_sql = " AND tl = %s "
        scope_params = [tl_name]
    conn = _db()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(f"""
                SELECT employee_id, schedule_name, email
                FROM gsheet_employees
                WHERE status = 'Active'
                  AND employee_id NOT LIKE '%%-old'
                  {scope_sql}
                ORDER BY schedule_name ASC
            """, scope_params)
            return cur.fetchall()
    finally:
        conn.close()


def _supervisors():
    conn = _db()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(f"""
                SELECT DISTINCT e.employee_id, e.schedule_name
                FROM coaching_sessions cs
                JOIN gsheet_employees e
                  ON e.employee_id = cs.supervisor_id {GC}
                ORDER BY e.schedule_name ASC
            """)
            return cur.fetchall()
    finally:
        conn.close()


def _distinct_col(col):
    """Distinct non-empty values of a gsheet column for filter dropdowns."""
    conn = _db()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(f"""
                SELECT DISTINCT {col} AS v
                FROM gsheet_employees
                WHERE {col} IS NOT NULL AND {col} <> ''
                ORDER BY {col} ASC
            """)
            return [r["v"] for r in cur.fetchall()]
    finally:
        conn.close()


# --- Dashboard ------------------------------------------------------------
@coaching_bp.route("/")
@coaching_required
def index():
    scope, sp = _scope()
    conn = _db()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(f"""
                SELECT
                  COUNT(*)                                                  AS total,
                  SUM(session_date >= DATE_FORMAT(CURDATE(),'%%Y-%%m-01'))  AS this_month,
                  SUM(status = 'pending_followup')                          AS pending
                FROM coaching_sessions cs
                WHERE 1=1 {scope}
            """, sp)
            stats = cur.fetchone() or {}

            cur.execute(f"""
                SELECT cs.*,
                       a.schedule_name AS agent_name,
                       s.schedule_name AS supervisor_name
                FROM coaching_sessions cs
                LEFT JOIN gsheet_employees a ON a.employee_id = cs.agent_id {GC}
                LEFT JOIN gsheet_employees s ON s.employee_id = cs.supervisor_id {GC}
                WHERE 1=1 {scope}
                ORDER BY cs.session_date DESC, cs.id DESC
                LIMIT 10
            """, sp)
            recent = cur.fetchall()

            # Team Performance Overview — per-supervisor breakdown (managers only).
            team = []
            if can_reports():
                cur.execute(f"""
                    SELECT
                      s.employee_id, s.schedule_name, s.email,
                      COUNT(*) AS total,
                      SUM(cs.session_date >= DATE_FORMAT(CURDATE(),'%%Y-%%m-01')) AS this_month,
                      SUM(cs.status = 'pending_followup') AS pending,
                      MAX(cs.session_date) AS last_session
                    FROM coaching_sessions cs
                    LEFT JOIN gsheet_employees s
                      ON s.employee_id = cs.supervisor_id {GC}
                    GROUP BY s.employee_id, s.schedule_name, s.email
                    ORDER BY total DESC
                """)
                team = cur.fetchall()
    finally:
        conn.close()

    return render_template(
        "coaching/index.html",
        stats=stats, recent=recent, team=team,
        types=COACHING_TYPES, statuses=STATUSES,
        is_manager=can_reports(),
    )


# --- Agents directory -----------------------------------------------------
@coaching_bp.route("/agents")
@coaching_required
def agents():
    scope, sp = _scope()
    conn = _db()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(f"""
                SELECT
                  e.employee_id, e.schedule_name, e.email,
                  COUNT(cs.id)                        AS session_count,
                  MAX(cs.session_date)                AS last_session,
                  SUM(cs.status = 'pending_followup') AS pending
                FROM gsheet_employees e
                LEFT JOIN coaching_sessions cs
                  ON cs.agent_id = e.employee_id {GC} {scope}
                WHERE e.employee_id NOT LIKE '%%-old'
                GROUP BY e.employee_id, e.schedule_name, e.email
                HAVING session_count > 0
                ORDER BY e.schedule_name ASC
            """, sp)
            rows = cur.fetchall()
    finally:
        conn.close()
    return render_template("coaching/agents.html", rows=rows)


# --- Agent profile --------------------------------------------------------
@coaching_bp.route("/agent/<employee_id>")
@coaching_required
def agent_profile(employee_id):
    scope, sp = _scope()
    conn = _db()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("""
                SELECT employee_id, schedule_name, email
                FROM gsheet_employees WHERE employee_id = %s LIMIT 1
            """, (employee_id,))
            agent = cur.fetchone()
            if not agent:
                abort(404)

            params = [employee_id] + sp
            cur.execute(f"""
                SELECT cs.*, s.schedule_name AS supervisor_name
                FROM coaching_sessions cs
                LEFT JOIN gsheet_employees s ON s.employee_id = cs.supervisor_id {GC}
                WHERE cs.agent_id = %s {scope}
                ORDER BY cs.session_date DESC, cs.id DESC
            """, params)
            sessions_ = cur.fetchall()

            cur.execute(f"""
                SELECT coaching_type, COUNT(*) AS n
                FROM coaching_sessions cs
                WHERE cs.agent_id = %s {scope}
                GROUP BY coaching_type
            """, params)
            by_type = cur.fetchall()
    finally:
        conn.close()

    return render_template(
        "coaching/agent_profile.html",
        agent=agent, sessions=sessions_, by_type=by_type,
        types=COACHING_TYPES, statuses=STATUSES,
    )


# --- All sessions (filters) ----------------------------------------------
@coaching_bp.route("/sessions")
@coaching_required
def all_sessions():
    f_agent = request.args.get("agent", "").strip()
    f_type = request.args.get("type", "").strip()
    f_status = request.args.get("status", "").strip()
    f_from = request.args.get("date_from", "").strip()
    f_to = request.args.get("date_to", "").strip()

    where, params = ["1=1"], []
    scope, sp = _scope()
    if scope:
        where.append(scope.strip()[4:])  # drop leading 'AND '
        params += sp
    if f_agent:
        where.append("cs.agent_id = %s"); params.append(f_agent)
    if f_type:
        where.append("cs.coaching_type = %s"); params.append(f_type)
    if f_status:
        where.append("cs.status = %s"); params.append(f_status)
    if f_from:
        where.append("cs.session_date >= %s"); params.append(f_from)
    if f_to:
        where.append("cs.session_date <= %s"); params.append(f_to)

    conn = _db()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(f"""
                SELECT cs.*,
                       a.schedule_name AS agent_name,
                       s.schedule_name AS supervisor_name
                FROM coaching_sessions cs
                LEFT JOIN gsheet_employees a ON a.employee_id = cs.agent_id {GC}
                LEFT JOIN gsheet_employees s ON s.employee_id = cs.supervisor_id {GC}
                WHERE {' AND '.join(where)}
                ORDER BY cs.session_date DESC, cs.id DESC
            """, params)
            rows = cur.fetchall()
    finally:
        conn.close()

    return render_template(
        "coaching/all_sessions.html",
        rows=rows, agents=_agents(),
        types=COACHING_TYPES, statuses=STATUSES,
        filters={"agent": f_agent, "type": f_type, "status": f_status,
                 "date_from": f_from, "date_to": f_to},
    )


# --- Single session -------------------------------------------------------
def _get_session(session_id):
    conn = _db()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(f"""
                SELECT cs.*,
                       a.schedule_name AS agent_name, a.email AS agent_email,
                       a.account AS agent_account, a.group_name AS agent_group,
                       s.schedule_name AS supervisor_name
                FROM coaching_sessions cs
                LEFT JOIN gsheet_employees a ON a.employee_id = cs.agent_id {GC}
                LEFT JOIN gsheet_employees s ON s.employee_id = cs.supervisor_id {GC}
                WHERE cs.id = %s LIMIT 1
            """, (session_id,))
            return cur.fetchone()
    finally:
        conn.close()


@coaching_bp.route("/session/<int:session_id>")
@coaching_required
def view_session(session_id):
    row = _get_session(session_id)
    if not row:
        abort(404)
    if not can_reports() and row["supervisor_id"] != _me():
        abort(403)
    return render_template(
        "coaching/view_session.html",
        s=row, types=COACHING_TYPES, statuses=STATUSES,
        can_edit=(can_reports() or row["supervisor_id"] == _me()),
    )


# --- Create / edit --------------------------------------------------------
def _parse_form():
    def g(k):
        v = request.form.get(k, "").strip()
        return v or None
    return {
        "agent_id": g("agent_id"),
        "session_date": g("session_date"),
        "session_time": g("session_time"),
        "coaching_type": g("coaching_type"),
        "topic": g("topic"),
        "discussion_notes": g("discussion_notes"),
        "strengths": g("strengths"),
        "areas_for_improvement": g("areas_for_improvement"),
        "follow_up_date": g("follow_up_date"),
        "status": g("status") or "pending",
    }


def _required_ok(d):
    # TL/SOM required: agent, date, time, type, topic,
    # discussion_notes, areas_for_improvement. Strengths optional.
    return all([
        d["agent_id"], d["session_date"], d["session_time"],
        d["coaching_type"], d["topic"], d["discussion_notes"],
        d["areas_for_improvement"],
    ])


@coaching_bp.route("/session/new", methods=["GET", "POST"])
@coaching_required
def new_session():
    if request.method == "POST":
        d = _parse_form()
        if not _required_ok(d):
            flash("Agent, date, time, type, topic, discussion notes, and areas for improvement are required.", "error")
            return render_template("coaching/session_form.html", mode="new", s=d,
                                   agents=_agents(), types=COACHING_TYPES, statuses=STATUSES)
        conn = _db()
        try:
            with conn.cursor() as cur:
                # Populate both id columns with the same YYMMDD-NN so PHP
                # (agent_id) and any *_employee_id reader stay consistent.
                cur.execute("""
                    INSERT INTO coaching_sessions
                      (agent_id, agent_employee_id,
                       supervisor_id, supervisor_employee_id,
                       session_date, session_time, coaching_type, topic,
                       discussion_notes, strengths, areas_for_improvement,
                       follow_up_date, status)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, (
                    d["agent_id"], d["agent_id"],
                    _me(), _me(),
                    d["session_date"], d["session_time"], d["coaching_type"], d["topic"],
                    d["discussion_notes"], d["strengths"], d["areas_for_improvement"],
                    d["follow_up_date"], d["status"],
                ))
                new_id = cur.lastrowid
            conn.commit()
        finally:
            conn.close()
        flash("Coaching session created.", "success")
        return redirect(url_for("coaching.view_session", session_id=new_id))

    return render_template("coaching/session_form.html", mode="new", s={},
                           agents=_agents(), types=COACHING_TYPES, statuses=STATUSES)


@coaching_bp.route("/session/<int:session_id>/edit", methods=["GET", "POST"])
@coaching_required
def edit_session(session_id):
    row = _get_session(session_id)
    if not row:
        abort(404)
    if not can_reports() and row["supervisor_id"] != _me():
        abort(403)

    if request.method == "POST":
        d = _parse_form()
        if not _required_ok(d):
            flash("Agent, date, time, type, topic, discussion notes, and areas for improvement are required.", "error")
            d["id"] = session_id
            return render_template("coaching/session_form.html", mode="edit", s=d,
                                   agents=_agents(), types=COACHING_TYPES, statuses=STATUSES)
        conn = _db()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE coaching_sessions SET
                      agent_id=%s, agent_employee_id=%s,
                      session_date=%s, session_time=%s, coaching_type=%s,
                      topic=%s, discussion_notes=%s, strengths=%s,
                      areas_for_improvement=%s,
                      follow_up_date=%s, status=%s
                    WHERE id=%s
                """, (
                    d["agent_id"], d["agent_id"],
                    d["session_date"], d["session_time"], d["coaching_type"],
                    d["topic"], d["discussion_notes"], d["strengths"],
                    d["areas_for_improvement"],
                    d["follow_up_date"], d["status"], session_id,
                ))
            conn.commit()
        finally:
            conn.close()
        flash("Session updated.", "success")
        return redirect(url_for("coaching.view_session", session_id=session_id))

    return render_template("coaching/session_form.html", mode="edit", s=row,
                           agents=_agents(), types=COACHING_TYPES, statuses=STATUSES)


@coaching_bp.route("/session/<int:session_id>/delete", methods=["POST"])
@coaching_required
def delete_session(session_id):
    """Dual-run-safe 'delete' = mark cancelled (PHP understands this status).
    We do NOT hard-delete: a hard delete would silently vanish from the PHP UI
    with no audit trail, and the PHP FK is ON DELETE CASCADE against Employees."""
    row = _get_session(session_id)
    if not row:
        abort(404)
    if not can_reports() and row["supervisor_id"] != _me():
        abort(403)
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE coaching_sessions SET status='cancelled' WHERE id=%s",
                (session_id,),
            )
        conn.commit()
    finally:
        conn.close()
    flash("Session cancelled.", "success")
    return redirect(url_for("coaching.all_sessions"))


# --- Agent-facing views ---------------------------------------------------
def _agent_login_required(view):
    """Any authenticated employee. Agent sees only their own sessions."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


@coaching_bp.route("/my")
@_agent_login_required
def my_sessions():
    """List the logged-in employee's own coaching sessions."""
    me = _me()
    conn = _db()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(f"""
                SELECT cs.*, s.schedule_name AS supervisor_name
                FROM coaching_sessions cs
                LEFT JOIN gsheet_employees s ON s.employee_id = cs.supervisor_id {GC}
                WHERE cs.agent_id = %s
                ORDER BY cs.session_date DESC, cs.id DESC
            """, (me,))
            rows = cur.fetchall()
            # The employee's own group/account for the header chip.
            cur.execute("""
                SELECT schedule_name, account, group_name
                FROM gsheet_employees WHERE employee_id = %s LIMIT 1
            """, (me,))
            meinfo = cur.fetchone() or {}
    finally:
        conn.close()
    return render_template("coaching/my_sessions.html", rows=rows,
                           meinfo=meinfo, types=COACHING_TYPES)


@coaching_bp.route("/my/<int:session_id>", methods=["GET", "POST"])
@_agent_login_required
def my_session(session_id):
    """Agent view of a single own session: read TL notes, edit Action Plan,
    click Complete. Locked once completed."""
    me = _me()
    row = _get_session(session_id)
    if not row:
        abort(404)
    # Agents may only touch their OWN sessions.
    if row["agent_id"] != me:
        abort(403)

    locked = (row["status"] == "completed")

    if request.method == "POST" and not locked:
        action_plan = (request.form.get("action_plan", "") or "").strip()
        do_complete = request.form.get("complete") == "1"

        if do_complete and not action_plan:
            flash("Please fill in your Action Plan before completing.", "error")
            return redirect(url_for("coaching.my_session", session_id=session_id))

        conn = _db()
        try:
            with conn.cursor() as cur:
                if do_complete:
                    cur.execute("""UPDATE coaching_sessions
                                   SET action_plan=%s, status='completed'
                                   WHERE id=%s AND agent_id=%s""",
                                (action_plan or None, session_id, me))
                else:
                    cur.execute("""UPDATE coaching_sessions
                                   SET action_plan=%s WHERE id=%s AND agent_id=%s""",
                                (action_plan or None, session_id, me))
            conn.commit()
        finally:
            conn.close()
        flash("Coaching completed. Thank you!" if do_complete else "Action plan saved.",
              "success")
        return redirect(url_for("coaching.my_session", session_id=session_id))

    return render_template("coaching/my_session.html", s=row, locked=locked,
                           types=COACHING_TYPES)


# --- Reports (managers only) ---------------------------------------------
def _report_where():
    f_from = request.args.get("date_from", "").strip()
    f_to = request.args.get("date_to", "").strip()
    f_sup = request.args.get("supervisor", "").strip()
    f_agent = request.args.get("agent", "").strip()
    f_group = request.args.get("group", "").strip()
    f_account = request.args.get("account", "").strip()
    where, params = ["1=1"], []
    if f_from:
        where.append("cs.session_date >= %s"); params.append(f_from)
    if f_to:
        where.append("cs.session_date <= %s"); params.append(f_to)
    if f_sup:
        where.append("cs.supervisor_id = %s"); params.append(f_sup)
    if f_agent:
        where.append("cs.agent_id = %s"); params.append(f_agent)
    if f_group:
        where.append("ag.group_name = %s"); params.append(f_group)
    if f_account:
        where.append("ag.account = %s"); params.append(f_account)
    # Respect visibility scope (TL -> team, SOM/QA/Finest -> all).
    sc, sp = _scope()
    if sc:
        where.append(sc.strip()[4:]); params += sp
    return " AND ".join(where), params, {
        "date_from": f_from, "date_to": f_to,
        "supervisor": f_sup, "agent": f_agent,
        "group": f_group, "account": f_account,
    }


def _run_report(rtype, where, params):
    # Every query joins the agent's roster row as `ag` so group/account
    # filters in `where` resolve. Pending = new 'pending'/'for_followup'
    # plus legacy 'pending_followup'.
    AGJOIN = f"LEFT JOIN gsheet_employees ag ON ag.employee_id = cs.agent_id {GC}"
    PENDING = "(cs.status IN ('pending','for_followup','pending_followup'))"
    DONE = "(cs.status='completed')"
    conn = _db()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            if rtype == "by_group":
                cur.execute(f"""
                    SELECT COALESCE(ag.account,'(No account)')    AS account,
                           COALESCE(ag.group_name,'(No group)')   AS group_name,
                           COUNT(*)                               AS total,
                           SUM({DONE})                            AS completed,
                           SUM({PENDING})                         AS pending,
                           COUNT(DISTINCT cs.agent_id)            AS agents
                    FROM coaching_sessions cs
                    {AGJOIN}
                    WHERE {where}
                    GROUP BY ag.account, ag.group_name
                    ORDER BY account ASC, total DESC
                """, params)
            elif rtype == "by_supervisor":
                cur.execute(f"""
                    SELECT s.schedule_name AS supervisor_name,
                           COUNT(*) AS total,
                           SUM({DONE}) AS completed,
                           SUM({PENDING}) AS pending
                    FROM coaching_sessions cs
                    {AGJOIN}
                    LEFT JOIN gsheet_employees s ON s.employee_id = cs.supervisor_id {GC}
                    WHERE {where}
                    GROUP BY cs.supervisor_id, s.schedule_name
                    ORDER BY total DESC
                """, params)
            elif rtype == "by_agent":
                cur.execute(f"""
                    SELECT a.schedule_name AS agent_name,
                           COALESCE(ag.account,'—') AS account,
                           COALESCE(ag.group_name,'—') AS group_name,
                           COUNT(*) AS total,
                           MAX(cs.session_date) AS last_session,
                           SUM({PENDING}) AS pending
                    FROM coaching_sessions cs
                    {AGJOIN}
                    LEFT JOIN gsheet_employees a ON a.employee_id = cs.agent_id {GC}
                    WHERE {where}
                    GROUP BY cs.agent_id, a.schedule_name, ag.account, ag.group_name
                    ORDER BY total DESC
                """, params)
            elif rtype == "by_type":
                cur.execute(f"""
                    SELECT coaching_type, COUNT(*) AS total
                    FROM coaching_sessions cs
                    {AGJOIN}
                    WHERE {where}
                    GROUP BY coaching_type
                    ORDER BY total DESC
                """, params)
            elif rtype == "detailed":
                cur.execute(f"""
                    SELECT cs.session_date, cs.coaching_type, cs.topic, cs.status,
                           a.schedule_name AS agent_name,
                           COALESCE(ag.account,'—') AS account,
                           COALESCE(ag.group_name,'—') AS group_name,
                           s.schedule_name AS supervisor_name
                    FROM coaching_sessions cs
                    {AGJOIN}
                    LEFT JOIN gsheet_employees a ON a.employee_id = cs.agent_id {GC}
                    LEFT JOIN gsheet_employees s ON s.employee_id = cs.supervisor_id {GC}
                    WHERE {where}
                    ORDER BY cs.session_date DESC, cs.id DESC
                """, params)
            else:  # summary
                cur.execute(f"""
                    SELECT COUNT(*) AS total,
                           COUNT(DISTINCT cs.agent_id) AS agents,
                           COUNT(DISTINCT cs.supervisor_id) AS supervisors,
                           SUM({DONE}) AS completed,
                           SUM({PENDING}) AS pending,
                           SUM(cs.status='cancelled') AS cancelled
                    FROM coaching_sessions cs
                    {AGJOIN}
                    WHERE {where}
                """, params)
            return cur.fetchall()
    finally:
        conn.close()


@coaching_bp.route("/reports")
@reports_required
def reports():
    rtype = request.args.get("type", "summary").strip()
    where, params, filters = _report_where()
    rows = _run_report(rtype, where, params)
    total_all = sum(r["total"] for r in rows) if rtype == "by_type" else None

    # Build nested account -> [group rows] structure for the by_group view.
    grouped = None
    if rtype == "by_group":
        grouped = {}
        for r in rows:
            acct = r["account"]
            grouped.setdefault(acct, {"rows": [], "total": 0, "completed": 0,
                                      "pending": 0, "agents": 0})
            grouped[acct]["rows"].append(r)
            grouped[acct]["total"] += r["total"] or 0
            grouped[acct]["completed"] += r["completed"] or 0
            grouped[acct]["pending"] += r["pending"] or 0
            grouped[acct]["agents"] += r["agents"] or 0
        # Order accounts by total desc
        grouped = dict(sorted(grouped.items(),
                              key=lambda kv: kv[1]["total"], reverse=True))

    return render_template(
        "coaching/reports.html",
        rtype=rtype, rows=rows, filters=filters, grouped=grouped,
        supervisors=_supervisors(), agents=_agents(),
        groups=_distinct_col("group_name"), accounts=_distinct_col("account"),
        types=COACHING_TYPES, statuses=STATUSES, total_all=total_all,
    )


@coaching_bp.route("/reports/export")
@reports_required
def reports_export():
    rtype = request.args.get("type", "summary").strip()
    where, params, _ = _report_where()
    rows = _run_report(rtype, where, params)

    buf = io.StringIO()
    w = csv.writer(buf)
    if rows:
        headers = list(rows[0].keys())
        w.writerow(headers)
        for r in rows:
            w.writerow([r.get(h, "") for h in headers])
    else:
        w.writerow(["No data"])

    stamp = datetime.now().strftime("%Y%m%d")
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition":
                             f"attachment; filename=coaching_{rtype}_{stamp}.csv"})