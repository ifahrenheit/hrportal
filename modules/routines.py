"""
Routine Tracker blueprint for the Cohere HR portal (leavesystem).

A lightweight recurring-health-check log for the IT group (think Monday /
Microsoft Lists): admins define routine tasks, IT staff log a pass/fail check
against them, and the flat log table feeds a dashboard and a CSV export for
Power BI.

Tables (central_db, DDL in scripts/routine_tracker_schema.sql):
    routine_tasks -- task definitions (title, category, frequency, active)
    routine_logs  -- one row per check performed (typed, flat, no blobs)

Access: admins and holders of the existing `can_it_tickets` permission may view,
log and export. Only admins add / edit / (de)activate task definitions.
/routines/logs.csv additionally accepts a static bearer token
(ROUTINES_EXPORT_TOKEN in .env) so Power BI, which cannot do the Keycloak login,
can pull it. The token endpoint is disabled unless that env var is set.

Register in app.py:
    from modules.routines import routines_bp
    app.register_blueprint(routines_bp)
"""

import csv
import io
import os
import secrets
from datetime import date, datetime, time, timedelta

from flask import (
    Blueprint, Response, abort, flash, redirect, render_template, request,
    session, url_for,
)

from csrf import validate_csrf
from db_core import get_db_connection

routines_bp = Blueprint("routines", __name__, url_prefix="/routines")

FREQUENCIES = ("daily", "weekly", "biweekly", "monthly")
FREQ_LABELS = {
    "daily": "Daily", "weekly": "Weekly",
    "biweekly": "Biweekly", "monthly": "Monthly",
}
# How the "due" window reads on the dashboard ("not yet logged <this ...>").
WINDOW_LABELS = {
    "daily": "today", "weekly": "this week",
    "biweekly": "this fortnight", "monthly": "this month",
}
RESULTS = ("pass", "fail")

# Biweekly windows are fixed 14-day blocks counted from this Monday, so they
# stay continuous across year boundaries (ISO-week parity would not: 53-week
# years put two odd weeks back to back).
BIWEEKLY_ANCHOR = date(2024, 1, 1)

MAX_TITLE = 150
MAX_CATEGORY = 60
MAX_TEXT = 2000
MAX_LOG_ROWS = 5000
DEFAULT_CATEGORY = "IT group"


# ─── Access ──────────────────────────────────────────────────────────────────
def _is_admin():
    return bool(session.get("is_admin"))


def _has_access():
    return _is_admin() or bool(session.get("permissions", {}).get("can_it_tickets"))


def _guard(allow_token=False):
    """Auth + role check, called first in every route.

    Returns None when the request may proceed, else a response to return.
    """
    if allow_token and _token_ok():
        return None
    if "user" not in session:
        return redirect(url_for("login"))
    if not _has_access():
        flash("You do not have permission to access the Routine Tracker.", "danger")
        return redirect(url_for("dashboard"))
    return None


def _guard_admin():
    """Like _guard(), but task definitions are admin-only."""
    denied = _guard()
    if denied:
        return denied
    if not _is_admin():
        flash("Only admins can change routine task definitions.", "danger")
        return redirect(url_for("routines.task_list"))
    return None


def _token_ok():
    expected = os.getenv("ROUTINES_EXPORT_TOKEN", "")
    header = request.headers.get("Authorization", "")
    if not expected or not header.startswith("Bearer "):
        return False
    return secrets.compare_digest(header[7:].strip(), expected)


def _actor():
    u = session.get("user", {})
    return u.get("email") or u.get("name") or "unknown"


def _csrf_or_403():
    if not validate_csrf():
        abort(403)


# ─── Due-window logic ────────────────────────────────────────────────────────
def window_start(frequency, today):
    """First day of the window that `today` falls in for this frequency."""
    if frequency == "daily":
        return today
    if frequency == "weekly":  # ISO week: Monday start
        return today - timedelta(days=today.weekday())
    if frequency == "biweekly":
        blocks = (today - BIWEEKLY_ANCHOR).days // 14
        return BIWEEKLY_ANCHOR + timedelta(days=14 * blocks)
    if frequency == "monthly":
        return today.replace(day=1)
    raise ValueError(f"unknown frequency: {frequency!r}")


def is_due(frequency, last_checked, today):
    """Due = never checked, or last check is older than the current window.

    A failed check still counts as "performed" -- due tracks whether the check
    was done, not whether it passed.
    """
    if last_checked is None:
        return True
    return last_checked < datetime.combine(window_start(frequency, today), time.min)


def _due_tasks(cur, today):
    cur.execute(
        """
        SELECT t.id, t.title, t.category, t.frequency, MAX(l.checked_at) AS last_checked
        FROM routine_tasks t
        LEFT JOIN routine_logs l ON l.task_id = t.id
        WHERE t.active = 1
        GROUP BY t.id, t.title, t.category, t.frequency
        """
    )
    order = {f: i for i, f in enumerate(FREQUENCIES)}
    due = []
    for row in cur.fetchall():
        if is_due(row["frequency"], row["last_checked"], today):
            row["window"] = WINDOW_LABELS[row["frequency"]]
            due.append(row)
    due.sort(key=lambda r: (order[r["frequency"]], r["title"].lower()))
    return due


# ─── Helpers ─────────────────────────────────────────────────────────────────
def _parse_date(raw):
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _parse_int(raw):
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _clean(raw, limit):
    return (raw or "").strip()[:limit]


def _log_filters(default_days=None):
    """Read + validate the shared log filters from the query string."""
    f = {
        "task_id": _parse_int(request.args.get("task_id")),
        "result": request.args.get("result", ""),
        "frequency": request.args.get("frequency", ""),
        "date_from": _parse_date(request.args.get("date_from")),
        "date_to": _parse_date(request.args.get("date_to")),
    }
    if f["result"] not in RESULTS:
        f["result"] = ""
    if f["frequency"] not in FREQUENCIES:
        f["frequency"] = ""
    if default_days and "date_from" not in request.args and "date_to" not in request.args:
        f["date_from"] = date.today() - timedelta(days=default_days - 1)
    return f


def _log_query(f, limit=None):
    """Build the parameterised flat-log SELECT for the given filters."""
    where, params = [], []
    if f["task_id"] is not None:
        where.append("l.task_id = %s")
        params.append(f["task_id"])
    if f["result"]:
        where.append("l.result = %s")
        params.append(f["result"])
    if f["frequency"]:
        where.append("t.frequency = %s")
        params.append(f["frequency"])
    if f["date_from"]:
        where.append("l.checked_at >= %s")
        params.append(datetime.combine(f["date_from"], time.min))
    if f["date_to"]:  # inclusive of the whole end day
        where.append("l.checked_at < %s")
        params.append(datetime.combine(f["date_to"] + timedelta(days=1), time.min))
    sql = """
        SELECT l.id AS log_id, l.task_id, t.title AS task_title, t.category,
               t.frequency, l.checked_by, l.checked_at, l.result, l.comment
        FROM routine_logs l
        JOIN routine_tasks t ON t.id = l.task_id
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY l.checked_at DESC, l.id DESC"
    if limit:
        sql += " LIMIT %s"
        params.append(limit)
    return sql, params


def _all_tasks(cur, active_only=False):
    cur.execute(
        "SELECT id, title, category, frequency, active FROM routine_tasks"
        + (" WHERE active = 1" if active_only else "")
        + " ORDER BY title"
    )
    return cur.fetchall()


# ─── Task list ───────────────────────────────────────────────────────────────
@routines_bp.route("/")
def task_list():
    denied = _guard()
    if denied:
        return denied
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT t.id, t.title, t.description, t.category, t.frequency, t.active,
                       t.created_by, t.created_at, MAX(l.checked_at) AS last_checked
                FROM routine_tasks t
                LEFT JOIN routine_logs l ON l.task_id = t.id
                GROUP BY t.id, t.title, t.description, t.category, t.frequency,
                         t.active, t.created_by, t.created_at
                ORDER BY t.active DESC, t.title
                """
            )
            tasks = cur.fetchall()
            cur.execute("SELECT DISTINCT category FROM routine_tasks ORDER BY category")
            categories = [r["category"] for r in cur.fetchall()]
    finally:
        conn.close()
    return render_template(
        "routines/tasks.html", tasks=tasks, categories=categories,
        frequencies=FREQUENCIES, freq_labels=FREQ_LABELS,
        can_manage=_is_admin(), active_tab="tasks",
        default_category=DEFAULT_CATEGORY,
    )


@routines_bp.route("/tasks/save", methods=["POST"])
def task_save():
    denied = _guard_admin()
    if denied:
        return denied
    _csrf_or_403()

    task_id = _parse_int(request.form.get("id"))
    title = _clean(request.form.get("title"), MAX_TITLE)
    description = _clean(request.form.get("description"), MAX_TEXT) or None
    category = _clean(request.form.get("category"), MAX_CATEGORY) or DEFAULT_CATEGORY
    frequency = request.form.get("frequency", "")

    if not title:
        flash("Task title is required.", "danger")
        return redirect(url_for("routines.task_list"))
    if frequency not in FREQUENCIES:
        flash("Pick a valid frequency.", "danger")
        return redirect(url_for("routines.task_list"))

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            if task_id:
                cur.execute(
                    """UPDATE routine_tasks
                       SET title=%s, description=%s, category=%s, frequency=%s
                       WHERE id=%s""",
                    (title, description, category, frequency, task_id),
                )
                msg = "Task updated."
            else:
                cur.execute(
                    """INSERT INTO routine_tasks
                       (title, description, category, frequency, active, created_by)
                       VALUES (%s, %s, %s, %s, 1, %s)""",
                    (title, description, category, frequency, _actor()),
                )
                msg = "Task added."
        conn.commit()
    finally:
        conn.close()
    flash(msg, "success")
    return redirect(url_for("routines.task_list"))


@routines_bp.route("/tasks/<int:task_id>/toggle", methods=["POST"])
def task_toggle(task_id):
    denied = _guard_admin()
    if denied:
        return denied
    _csrf_or_403()
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE routine_tasks SET active = 1 - active WHERE id = %s", (task_id,))
            cur.execute("SELECT active FROM routine_tasks WHERE id = %s", (task_id,))
            row = cur.fetchone()
        conn.commit()
    finally:
        conn.close()
    if row is None:
        abort(404)
    flash("Task activated." if row["active"] else "Task deactivated.", "success")
    return redirect(url_for("routines.task_list"))


# ─── Log a check ─────────────────────────────────────────────────────────────
@routines_bp.route("/log", methods=["GET", "POST"])
def log_check():
    denied = _guard()
    if denied:
        return denied

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            if request.method == "POST":
                _csrf_or_403()
                task_id = _parse_int(request.form.get("task_id"))
                result = request.form.get("result", "")
                comment = _clean(request.form.get("comment"), MAX_TEXT) or None

                cur.execute(
                    "SELECT id, title FROM routine_tasks WHERE id = %s AND active = 1",
                    (task_id,),
                )
                task = cur.fetchone()
                if task is None or result not in RESULTS:
                    flash("Pick an active task and a pass/fail result.", "danger")
                    return redirect(url_for("routines.log_check", task_id=task_id))

                cur.execute(
                    """INSERT INTO routine_logs (task_id, checked_by, checked_at, result, comment)
                       VALUES (%s, %s, %s, %s, %s)""",
                    (task["id"], _actor(), datetime.now(), result, comment),
                )
                conn.commit()
                flash(f"Logged {result.upper()} for “{task['title']}”.", "success")
                return redirect(url_for("routines.log_check"))

            tasks = _all_tasks(cur, active_only=True)
            due_ids = {d["id"] for d in _due_tasks(cur, date.today())}
            cur.execute(
                """SELECT l.checked_at, l.result, l.comment, t.title AS task_title
                   FROM routine_logs l JOIN routine_tasks t ON t.id = l.task_id
                   WHERE l.checked_by = %s
                   ORDER BY l.checked_at DESC, l.id DESC LIMIT 10""",
                (_actor(),),
            )
            mine = cur.fetchall()
    finally:
        conn.close()

    return render_template(
        "routines/log.html", tasks=tasks, due_ids=due_ids, mine=mine,
        selected_task=_parse_int(request.args.get("task_id")),
        freq_labels=FREQ_LABELS, active_tab="log",
    )


# ─── Log history ─────────────────────────────────────────────────────────────
@routines_bp.route("/logs")
def log_history():
    denied = _guard()
    if denied:
        return denied
    f = _log_filters(default_days=30)
    sql, params = _log_query(f, limit=MAX_LOG_ROWS + 1)
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
            tasks = _all_tasks(cur)
    finally:
        conn.close()
    truncated = len(rows) > MAX_LOG_ROWS
    rows = rows[:MAX_LOG_ROWS]
    csv_args = {k: v.isoformat() if isinstance(v, date) else v
                for k, v in f.items() if v not in (None, "")}
    return render_template(
        "routines/logs.html", rows=rows, tasks=tasks, filters=f,
        frequencies=FREQUENCIES, freq_labels=FREQ_LABELS, results=RESULTS,
        truncated=truncated, max_rows=MAX_LOG_ROWS, csv_args=csv_args,
        active_tab="logs",
    )


CSV_COLUMNS = ("log_id", "task_id", "task_title", "category", "frequency",
               "checked_by", "checked_at", "result", "comment")
_CSV_TEXT = {"task_title", "category", "checked_by", "comment"}


def _csv_cell(col, value):
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    value = str(value)
    # Neutralise spreadsheet formula injection in free-text columns.
    if col in _CSV_TEXT and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        value = "'" + value
    return value


@routines_bp.route("/logs.csv")
def log_csv():
    denied = _guard(allow_token=True)
    if denied:
        return denied
    f = _log_filters()  # no default date window: export is the full table
    sql, params = _log_query(f)
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    finally:
        conn.close()

    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(CSV_COLUMNS)
    for r in rows:
        w.writerow([_csv_cell(c, r[c]) for c in CSV_COLUMNS])
    return Response(
        "﻿" + buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": 'attachment; filename="routine_logs.csv"'},
    )


# ─── Dashboard ───────────────────────────────────────────────────────────────
RANGES = (7, 30, 90)


@routines_bp.route("/dashboard")
def dashboard():
    denied = _guard()
    if denied:
        return denied
    days = _parse_int(request.args.get("days"))
    if days not in RANGES:
        days = 30
    today = date.today()
    start = today - timedelta(days=days - 1)
    since = datetime.combine(start, time.min)

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT DATE(checked_at) AS d, result, COUNT(*) AS n
                   FROM routine_logs WHERE checked_at >= %s
                   GROUP BY DATE(checked_at), result""",
                (since,),
            )
            per_day = {}
            for r in cur.fetchall():
                per_day.setdefault(r["d"], {})[r["result"]] = r["n"]

            cur.execute(
                """SELECT t.id, t.title, t.frequency, COUNT(*) AS total,
                          SUM(l.result = 'fail') AS fails
                   FROM routine_logs l JOIN routine_tasks t ON t.id = l.task_id
                   WHERE l.checked_at >= %s
                   GROUP BY t.id, t.title, t.frequency""",
                (since,),
            )
            per_task = list(cur.fetchall())  # PyMySQL returns () when empty

            cur.execute(
                """SELECT l.checked_at, l.checked_by, l.comment, t.title AS task_title
                   FROM routine_logs l JOIN routine_tasks t ON t.id = l.task_id
                   WHERE l.result = 'fail' AND l.checked_at >= %s
                   ORDER BY l.checked_at DESC, l.id DESC LIMIT 10""",
                (since,),
            )
            recent_fails = cur.fetchall()

            due = _due_tasks(cur, today)
    finally:
        conn.close()

    labels, pass_series, fail_series = [], [], []
    for i in range(days):
        d = start + timedelta(days=i)
        counts = per_day.get(d, {})
        labels.append(d.strftime("%b %d"))
        pass_series.append(counts.get("pass", 0))
        fail_series.append(counts.get("fail", 0))

    for t in per_task:
        t["fails"] = int(t["fails"] or 0)
        t["fail_rate"] = round(100.0 * t["fails"] / t["total"], 1)
    per_task.sort(key=lambda t: (-t["fail_rate"], -t["total"], t["title"].lower()))

    total = sum(pass_series) + sum(fail_series)
    fails = sum(fail_series)
    kpis = {
        "total": total,
        "fails": fails,
        "pass_rate": round(100.0 * (total - fails) / total, 1) if total else None,
        "due": len(due),
    }
    chart = {
        "labels": labels, "pass": pass_series, "fail": fail_series,
        "tasks": [{"title": t["title"], "rate": t["fail_rate"],
                   "fails": t["fails"], "total": t["total"]} for t in per_task],
    }
    return render_template(
        "routines/dashboard.html", kpis=kpis, chart=chart, per_task=per_task,
        due=due, recent_fails=recent_fails, days=days, ranges=RANGES,
        freq_labels=FREQ_LABELS, active_tab="dashboard",
    )
