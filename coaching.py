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
import os
import csv
import uuid
import pymysql
from datetime import datetime
from functools import wraps
from werkzeug.utils import secure_filename
from flask import (
    Blueprint, render_template, request, redirect, url_for,
    session, flash, abort, Response, current_app,
)
from csrf import validate_csrf

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
    "product_process": "Product Process",
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


# --- Reminder / notification helpers -------------------------------------
COACHING_SLA_DAYS = 3          # past this, a pending session is "overdue"
REMINDER_FIRST_DAY = 2         # first auto-reminder fires N days after creation
PENDING_STATUSES = ("pending", "for_followup", "pending_followup")


def _tl_email_for(tl_name):
    """TL login email from tl_view_map (canonical TL source)."""
    if not tl_name:
        return None
    conn = _db()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("""SELECT login_email FROM tl_view_map
                           WHERE tl_name = %s LIMIT 1""", (tl_name,))
            r = cur.fetchone()
            return r["login_email"] if r else None
    finally:
        conn.close()


def _recipients(session_id):
    """Return (agent_email, [cc_emails], ctx) for a session.
    agent = gsheet_employees.email; SOM = agent's approver;
    TL = tl_view_map.login_email for the agent's tl name."""
    conn = _db()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(f"""
                SELECT cs.id, cs.topic, cs.session_date, cs.status,
                       cs.reminder_count,
                       a.schedule_name AS agent_name, a.email AS agent_email,
                       a.tl AS agent_tl, a.approver AS som_email,
                       s.schedule_name AS supervisor_name
                FROM coaching_sessions cs
                LEFT JOIN gsheet_employees a ON a.employee_id = cs.agent_id {GC}
                LEFT JOIN gsheet_employees s ON s.employee_id = cs.supervisor_id {GC}
                WHERE cs.id = %s LIMIT 1
            """, (session_id,))
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return None, [], {}
    agent_email = (row.get("agent_email") or "").strip()
    cc = []
    tl_email = _tl_email_for(row.get("agent_tl"))
    if tl_email:
        cc.append(tl_email.strip())
    som = (row.get("som_email") or "").strip()
    if som and som not in cc:
        cc.append(som)
    return agent_email, cc, row


def _coaching_email_html(ctx, overdue):
    link = url_for("coaching.my_session", session_id=ctx["id"], _external=True)
    topic = ctx.get("topic") or "Coaching session"
    sup = ctx.get("supervisor_name") or "your team lead"
    date = ctx.get("session_date") or ""
    banner = ("#c05621" if overdue else "#2563eb")
    head = ("Your coaching action plan is overdue"
            if overdue else "Action needed: complete your coaching action plan")
    return f"""\
<div style="font-family:Segoe UI,Arial,sans-serif;max-width:560px;margin:0 auto;color:#1f2937;">
  <div style="background:{banner};color:#fff;padding:16px 20px;border-radius:10px 10px 0 0;">
    <h2 style="margin:0;font-size:1.15rem;">{head}</h2>
  </div>
  <div style="border:1px solid #e5e7eb;border-top:none;border-radius:0 0 10px 10px;padding:20px;">
    <p>Hi {ctx.get('agent_name') or 'there'},</p>
    <p>{sup} conducted a coaching session with you{(' on ' + str(date)) if date else ''} —
       topic: <strong>{topic}</strong>.</p>
    <p>Please log in, review the feedback, and complete your
       <strong>SMART Action Plan</strong>.</p>
    <p style="text-align:center;margin:24px 0;">
      <a href="{link}" style="background:{banner};color:#fff;text-decoration:none;
         padding:11px 22px;border-radius:8px;font-weight:600;display:inline-block;">
        Complete My Action Plan</a>
    </p>
    <p style="font-size:.85rem;color:#6b7280;">
      A plan is <strong>SMART</strong> when it's Specific, Measurable, Achievable,
      Relevant, and Time-bound.</p>
    {"<p style='font-size:.85rem;color:#c05621;font-weight:600;'>This session is past the "
     + str(COACHING_SLA_DAYS) + "-day completion window. Please act today.</p>" if overdue else ""}
    <hr style="border:none;border-top:1px solid #e5e7eb;margin:18px 0;">
    <p style="font-size:.75rem;color:#9ca3af;">Cohere HR Portal · automated message</p>
  </div>
</div>"""


def _notify_agent_pending(session_id, kind="auto"):
    """Email the agent (cc TL + SOM) to complete their action plan.
    Stamps last_reminder_at + increments reminder_count.
    kind: 'created' | 'manual' | 'auto'. Returns (ok, message)."""
    from app import send_email
    agent_email, cc, ctx = _recipients(session_id)
    if not agent_email:
        return False, "No agent email on file."
    if ctx.get("status") not in PENDING_STATUSES:
        return False, "Session is not pending."

    # SLA: overdue if created/session older than SLA days.
    overdue = False
    try:
        sd = ctx.get("session_date")
        if sd:
            overdue = (datetime.now().date() - sd).days > COACHING_SLA_DAYS
    except Exception:
        pass

    subject = ("Overdue: complete your coaching action plan" if overdue
               else "Action needed: complete your coaching action plan")
    html = _coaching_email_html(ctx, overdue)

    # send_email(to,...) takes a single string; comma-join so all recipients get it.
    recipients = ", ".join([agent_email] + cc)
    send_email(recipients, subject, html)

    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute("""UPDATE coaching_sessions
                           SET last_reminder_at = NOW(),
                               reminder_count = reminder_count + 1
                           WHERE id = %s""", (session_id,))
        conn.commit()
    finally:
        conn.close()
    return True, f"Reminder sent to {agent_email}" + (f" (cc {', '.join(cc)})" if cc else "")


@coaching_bp.route("/session/<int:session_id>")
@coaching_required
def view_session(session_id):
    row = _get_session(session_id)
    if not row:
        abort(404)
    if not can_reports() and row["supervisor_id"] != _me():
        abort(403)
    # Rate-limit the manual reminder button: allow if never sent or >4h ago.
    can_remind_now = True
    if row.get("last_reminder_at"):
        try:
            can_remind_now = (datetime.now() - row["last_reminder_at"]).total_seconds() > 4*3600
        except Exception:
            can_remind_now = True
    return render_template(
        "coaching/view_session.html",
        s=row, types=COACHING_TYPES, statuses=STATUSES,
        can_edit=(can_reports() or row["supervisor_id"] == _me()),
        attachments=_attachments(session_id),
        tl_attach_count=_attach_count(session_id, "tl"),
        attach_max=ATTACH_MAX_PER_SIDE, me=_me(),
        can_remind=(row["status"] in PENDING_STATUSES),
        can_remind_now=can_remind_now,
    )


@coaching_bp.route("/session/<int:session_id>/remind", methods=["POST"])
@coaching_required
def remind_session(session_id):
    if not validate_csrf():
        flash('Security check failed, please try again.', 'danger')
        return redirect(request.referrer or url_for('coaching.my_sessions'))
    row = _get_session(session_id)
    if not row:
        abort(404)
    if not can_reports() and row["supervisor_id"] != _me():
        abort(403)
    # Rate limit: block if a reminder went out in the last 4 hours.
    if row.get("last_reminder_at"):
        try:
            if (datetime.now() - row["last_reminder_at"]).total_seconds() < 4*3600:
                flash("A reminder was already sent recently. Try again later.", "error")
                return redirect(url_for("coaching.view_session", session_id=session_id))
        except Exception:
            pass
    ok, msg = _notify_agent_pending(session_id, kind="manual")
    flash(msg, "success" if ok else "error")
    return redirect(url_for("coaching.view_session", session_id=session_id))


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
        if not validate_csrf():
            flash('Security check failed, please try again.', 'danger')
            return redirect(url_for('coaching.new_session'))
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
        # Attach any images to the new session (session saves regardless).
        imgs = request.files.getlist("images")
        if any(f and f.filename for f in imgs):
            saved, err = _save_attachments(new_id, "tl", imgs)
            if err:
                flash(f"Session created, but image(s) not added: {err}", "warning")
            elif saved:
                flash(f"Session created with {saved} image(s).", "success")
            else:
                flash("Coaching session created.", "success")
        else:
            flash("Coaching session created.", "success")
        # Notify the agent immediately if this session needs their action plan.
        if d["status"] in PENDING_STATUSES:
            try:
                _notify_agent_pending(new_id, kind="created")
            except Exception as e:
                current_app.logger.error(f"coaching create-notify failed: {e}")
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
        if not validate_csrf():
            flash('Security check failed, please try again.', 'danger')
            return redirect(url_for('coaching.edit_session', session_id=session_id))
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
    if not validate_csrf():
        flash('Security check failed, please try again.', 'danger')
        return redirect(request.referrer or url_for('coaching.my_sessions'))
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


def _agent_login_required(view):
    """Any authenticated employee. Agent sees only their own sessions."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


# --- Attachments ----------------------------------------------------------
ATTACH_EXTS = {"jpg", "jpeg", "png", "webp"}
ATTACH_MAX_PER_SIDE = 4
ATTACH_SUBDIR = os.path.join("uploads", "coaching")   # under static/


def _attach_dir():
    d = os.path.join(current_app.root_path, "static", "uploads", "coaching")
    os.makedirs(d, exist_ok=True)
    return d


def _attachments(session_id):
    """All non-deleted attachments for a session, both sides."""
    conn = _db()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("""
                SELECT id, session_id, file_name, file_path, file_type,
                       uploaded_by, uploaded_by_side, uploaded_at
                FROM coaching_attachments
                WHERE session_id = %s
                ORDER BY uploaded_at ASC, id ASC
            """, (session_id,))
            return cur.fetchall()
    finally:
        conn.close()


def _attach_count(session_id, side):
    conn = _db()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("""SELECT COUNT(*) AS n FROM coaching_attachments
                           WHERE session_id=%s AND uploaded_by_side=%s""",
                        (session_id, side))
            return cur.fetchone()["n"]
    finally:
        conn.close()


def _save_attachments(session_id, side, files):
    """Validate + save uploaded images for one side, honoring the 4-per-side cap.
    Returns (saved_count, error_message_or_None)."""
    existing = _attach_count(session_id, side)
    room = ATTACH_MAX_PER_SIDE - existing
    if room <= 0:
        return 0, f"You already have {ATTACH_MAX_PER_SIDE} images on this session."

    incoming = [f for f in files if f and f.filename]
    if not incoming:
        return 0, None
    if len(incoming) > room:
        return 0, f"You can add {room} more image(s); you selected {len(incoming)}."

    saved = 0
    conn = _db()
    try:
        with conn.cursor() as cur:
            for f in incoming:
                ext = (f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else "")
                if ext not in ATTACH_EXTS:
                    return saved, f"'{f.filename}' is not an allowed image (jpg, png, webp)."
                safe = secure_filename(f.filename)
                fname = f"COACH-{session_id}-{side}-{uuid.uuid4().hex[:8]}-{safe}"
                f.save(os.path.join(_attach_dir(), fname))
                rel = f"{ATTACH_SUBDIR}/{fname}".replace("\\", "/")
                cur.execute("""
                    INSERT INTO coaching_attachments
                      (session_id, file_name, file_path, file_type,
                       file_size, uploaded_by, uploaded_by_side)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)
                """, (session_id, fname, rel, ext, 0, _me(), side))
                saved += 1
        conn.commit()
    finally:
        conn.close()
    return saved, None


@coaching_bp.route("/session/<int:session_id>/attach", methods=["POST"])
@coaching_required
def attach_tl(session_id):
    if not validate_csrf():
        flash('Security check failed, please try again.', 'danger')
        return redirect(url_for('coaching.view_session', session_id=session_id))
    row = _get_session(session_id)
    if not row:
        abort(404)
    if not can_reports() and row["supervisor_id"] != _me() and not session.get("is_tl"):
        abort(403)
    if row["status"] == "completed":
        flash("This session is completed; attachments are locked.", "error")
        return redirect(url_for("coaching.view_session", session_id=session_id))
    saved, err = _save_attachments(session_id, "tl", request.files.getlist("images"))
    flash(err, "error") if err else flash(f"{saved} image(s) added.", "success")
    return redirect(url_for("coaching.view_session", session_id=session_id))


@coaching_bp.route("/my/<int:session_id>/attach", methods=["POST"])
@_agent_login_required
def attach_agent(session_id):
    if not validate_csrf():
        flash('Security check failed, please try again.', 'danger')
        return redirect(url_for('coaching.my_session', session_id=session_id))
    row = _get_session(session_id)
    if not row:
        abort(404)
    if row["agent_id"] != _me():
        abort(403)
    if row["status"] == "completed":
        flash("This session is completed; attachments are locked.", "error")
        return redirect(url_for("coaching.my_session", session_id=session_id))
    saved, err = _save_attachments(session_id, "agent", request.files.getlist("images"))
    flash(err, "error") if err else flash(f"{saved} image(s) added.", "success")
    return redirect(url_for("coaching.my_session", session_id=session_id))


@coaching_bp.route("/attachment/<int:att_id>/delete", methods=["POST"])
@_agent_login_required
def attach_delete(att_id):
    if not validate_csrf():
        flash('Security check failed, please try again.', 'danger')
        return redirect(request.referrer or url_for('coaching.my_sessions'))
    conn = _db()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("""SELECT a.*, cs.status, cs.agent_id, cs.supervisor_id
                           FROM coaching_attachments a
                           JOIN coaching_sessions cs ON cs.id = a.session_id
                           WHERE a.id=%s""", (att_id,))
            att = cur.fetchone()
            if not att:
                abort(404)
            # Only the uploader may delete, and only before completion.
            if att["uploaded_by"] != _me():
                abort(403)
            if att["status"] == "completed":
                flash("Session completed; attachments are locked.", "error")
                return redirect(_attach_back(att))
            # Remove file then row.
            try:
                fp = os.path.join(current_app.root_path, "static",
                                  *att["file_path"].split("/"))
                if os.path.isfile(fp):
                    os.remove(fp)
            except Exception:
                pass
            cur.execute("DELETE FROM coaching_attachments WHERE id=%s", (att_id,))
        conn.commit()
    finally:
        conn.close()
    flash("Image removed.", "success")
    return redirect(_attach_back(att))


def _attach_back(att):
    """Return the right page to redirect to after an attachment action."""
    if att.get("agent_id") == _me() and att.get("uploaded_by_side") == "agent":
        return url_for("coaching.my_session", session_id=att["session_id"])
    return url_for("coaching.view_session", session_id=att["session_id"])


# --- Agent-facing views ---------------------------------------------------
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
        if not validate_csrf():
            flash('Security check failed, please try again.', 'danger')
            return redirect(url_for('coaching.my_session', session_id=session_id))
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
                           types=COACHING_TYPES,
                           attachments=_attachments(session_id),
                           agent_attach_count=_attach_count(session_id, "agent"),
                           attach_max=ATTACH_MAX_PER_SIDE, me=_me())


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


# --- Dashboard drill-in: sessions a supervisor conducted -----------------
@coaching_bp.route("/api/supervisor/<employee_id>/sessions")
@coaching_required
def api_supervisor_sessions(employee_id):
    """JSON list of all sessions this supervisor conducted, for the dashboard
    drill-in modal. Managers only (matches the Team Performance Overview gate)."""
    from flask import jsonify
    if not can_reports():
        abort(403)
    conn = _db()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("""
                SELECT s.schedule_name AS supervisor_name, s.email
                FROM gsheet_employees s
                WHERE s.employee_id = %s LIMIT 1
            """, (employee_id,))
            sup = cur.fetchone() or {}

            cur.execute(f"""
                SELECT cs.id, cs.session_date, cs.coaching_type, cs.topic, cs.status,
                       a.schedule_name AS agent_name, a.employee_id AS agent_id
                FROM coaching_sessions cs
                LEFT JOIN gsheet_employees a ON a.employee_id = cs.agent_id {GC}
                WHERE cs.supervisor_id = %s
                ORDER BY cs.session_date DESC, cs.id DESC
            """, (employee_id,))
            rows = cur.fetchall()
    finally:
        conn.close()

    items = [{
        "id": r["id"],
        "date": str(r["session_date"]) if r["session_date"] else "",
        "agent": r["agent_name"] or r["agent_id"] or "—",
        "type": COACHING_TYPES.get(r["coaching_type"], r["coaching_type"] or "—"),
        "topic": r["topic"] or "—",
        "status": r["status"],
        "status_label": STATUS_LABELS_ALL.get(r["status"], r["status"]),
        "status_style": STATUS_BADGE.get(r["status"], ""),
        "url": url_for("coaching.view_session", session_id=r["id"]),
    } for r in rows]

    return jsonify({
        "supervisor": sup.get("supervisor_name") or employee_id,
        "count": len(items),
        "sessions": items,
    })