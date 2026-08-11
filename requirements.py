"""
Requirements Page blueprint for the Cohere HR portal (leavesystem).

- Roster joined live from central_db.gsheet_employees (via get_central_db, same as /pim).
- Checkbox state in central_db.employee_requirements (one row per employee).
- VIEW gated by can_requirements (admins + sub-admins with the flag: TL/Mgr/Director/HR).
- EDIT gated by REQUIREMENTS_EDITORS .env allowlist (admins always).
- Filters mirror what PIM has: search (schedule_name / employee_id), account, group_name.

Register in app.py:
    from requirements import requirements_bp
    app.register_blueprint(requirements_bp)
"""

import os
import pymysql
from datetime import datetime
from flask import (
    Blueprint, render_template, request, session,
    redirect, url_for, jsonify, abort
)

requirements_bp = Blueprint("requirements", __name__)


def _central_conn():
    """central_db connection. Lazy import of app.get_central_db to avoid a
    circular import at module load."""
    from app import get_central_db
    return get_central_db()


# Ordered (db_column, display_label) -- column order on the sheet.
REQUIREMENT_FIELDS = [
    ("tin",       "TIN"),
    ("sss",       "SSS"),
    ("phic",      "PHIC"),
    ("hdmf",      "HDMF"),
    ("education", "Education"),
    ("birth",     "Birth"),
    ("xray",      "Xray"),
    ("vax_doc",   "Vax Doc"),
    ("nbi",       "NBI"),
    ("valid_id",  "Valid ID"),
    ("ub_number", "UB#"),
]
REQUIREMENT_KEYS = [c for c, _ in REQUIREMENT_FIELDS]


def _logged_in():
    return "user" in session


def _can_view():
    if session.get("is_admin"):
        return True
    return bool(session.get("permissions", {}).get("can_requirements"))


def _edit_allowlist():
    raw = os.getenv("REQUIREMENTS_EDITORS", "")
    return {x.strip().lower() for x in raw.split(",") if x.strip()}


def _can_edit():
    if session.get("is_admin"):
        return True
    u = session.get("user", {})
    ident = {str(u.get("email", "")).lower(), str(u.get("employee_id", "")).lower()}
    return bool(ident & _edit_allowlist())


@requirements_bp.route("/requirements")
def requirements_page():
    if not _logged_in():
        return redirect(url_for("login"))
    if not _can_view():
        abort(403)

    conn = _central_conn()
    try:
        col_sql = ", ".join("r.%s" % c for c in REQUIREMENT_KEYS)
        sql = """
            SELECT
                e.employee_id,
                e.schedule_name,
                e.account,
                e.group_name,
                e.tl,
                e.status,
                {cols},
                r.updated_by,
                r.updated_at
            FROM gsheet_employees e
            LEFT JOIN employee_requirements r
                   ON r.employee_id = e.employee_id
                  COLLATE utf8mb4_unicode_ci
            WHERE e.status IN ('Active', 'Training', 'Pending')
            ORDER BY e.schedule_name ASC
        """.format(cols=col_sql)
        cur = conn.cursor(pymysql.cursors.DictCursor)
        cur.execute(sql)
        rows = cur.fetchall()

        cur.execute("""
            SELECT DISTINCT account FROM gsheet_employees
            WHERE account IS NOT NULL AND account <> ''
            ORDER BY account
        """)
        accounts = [r["account"] for r in cur.fetchall()]

        cur.execute("""
            SELECT DISTINCT group_name FROM gsheet_employees
            WHERE group_name IS NOT NULL AND group_name <> ''
            ORDER BY group_name
        """)
        groups = [r["group_name"] for r in cur.fetchall()]

        cur.execute("""
            SELECT tl_name FROM tl_view_map
            WHERE tl_name IS NOT NULL AND tl_name <> ''
            ORDER BY tl_name
        """)
        tls = [r["tl_name"] for r in cur.fetchall()]
        cur.close()
    finally:
        conn.close()

    for row in rows:
        for key in REQUIREMENT_KEYS:
            row[key] = int(row.get(key) or 0)

    return render_template(
        "requirements/requirements.html",
        rows=rows,
        fields=REQUIREMENT_FIELDS,
        accounts=accounts,
        groups=groups,
        tls=tls,
        can_edit=_can_edit(),
    )


@requirements_bp.route("/requirements/toggle", methods=["POST"])
def requirements_toggle():
    if not _logged_in():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    if not _can_edit():
        return jsonify({"ok": False, "error": "forbidden"}), 403

    data = request.get_json(silent=True) or {}
    employee_id = (data.get("employee_id") or "").strip()
    field = (data.get("field") or "").strip()
    value = 1 if data.get("value") else 0

    if not employee_id or field not in REQUIREMENT_KEYS:
        return jsonify({"ok": False, "error": "bad_request"}), 400

    who = session.get("user", {}).get("name") or session.get("user", {}).get("email")
    now = datetime.now()

    conn = _central_conn()
    try:
        sql = """
            INSERT INTO employee_requirements (employee_id, {field}, updated_by, updated_at)
            VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                {field} = VALUES({field}),
                updated_by = VALUES(updated_by),
                updated_at = VALUES(updated_at)
        """.format(field=field)
        cur = conn.cursor()
        cur.execute(sql, (employee_id, value, who, now))
        conn.commit()
        cur.close()
    except Exception as exc:
        conn.rollback()
        return jsonify({"ok": False, "error": str(exc)}), 500
    finally:
        conn.close()

    return jsonify({
        "ok": True,
        "employee_id": employee_id,
        "field": field,
        "value": value,
        "updated_by": who,
        "updated_at": now.strftime("%Y-%m-%d %H:%M"),
    })