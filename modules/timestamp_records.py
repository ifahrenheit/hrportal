"""
Timestamp Records blueprint: read-only view of the raw biometric punches
(clock in / clock out) for payroll verification, pulled straight from the
"Cohere Raw Logs" Google Drive folder via helpers/drive_timestamps.py.

- The page itself renders only the filter shell; rows come from the JSON
  endpoint so a slow Drive call shows a loading state instead of a hung page.
- The CSV has no names (header is companyid,Day,Time,Type), so First/Last
  Name come from OrangeHRM hs_hr_employee (orangehrm2, via
  helpers.orangehrm_db like modules/tardiness.py), matched on employee_id
  case-insensitively and ignoring surrounding spaces, as MySQL does. When an id has more than one row (rehires) the active one wins.
  Ids with no match are shown with blank names, never guessed.
- Access: admin, or a sub-admin given the dedicated can_timestamp_records
  permission (Sub-Admins page, Analytics & Reports). The JSON route checks the same thing and
  answers 401/403 as JSON. Browser input is only a from/to date range -- Drive file and
  folder ids are never taken from the request.
- No DB writes, no Drive writes.

Register in app.py:
    from modules.timestamp_records import timestamp_records_bp
    app.register_blueprint(timestamp_records_bp)
"""

import logging
from datetime import date, datetime

from flask import Blueprint, render_template, redirect, url_for, session, flash, request, jsonify

from helpers import drive_timestamps as dt
from helpers.orangehrm_db import get_orangehrm_connection
from helpers.payroll_period import (
    get_current_payroll_period,
    get_default_payroll_period,
    get_previous_period,
)

log = logging.getLogger(__name__)

timestamp_records_bp = Blueprint("timestamp_records", __name__)

VIEW_PERMISSIONS = ("can_timestamp_records",)
FIRST_DATE = date(2025, 1, 1)   # oldest data in "Cohere Raw Logs"
MAX_RANGE_DAYS = 62             # bounds Drive downloads per request (~2 months)
PERIOD_CHOICES = 12             # payroll periods offered in the dropdown


def _logged_in():
    return "user" in session


def _can_view():
    if session.get("is_admin"):
        return True
    perms = session.get("permissions", {}) or {}
    return any(perms.get(p) for p in VIEW_PERMISSIONS)


def _parse_range(args):
    """
    (start, end) dates from ?from=YYYY-MM-DD&to=YYYY-MM-DD, or an error
    message. Bounded so one request can't pull years of files.
    """
    try:
        start = datetime.strptime(args.get("from", ""), "%Y-%m-%d").date()
        end = datetime.strptime(args.get("to", ""), "%Y-%m-%d").date()
    except ValueError:
        return None, "Choose a valid From and To date."
    if start > end:
        return None, "From must be on or before To."
    if start < FIRST_DATE or end > date(2100, 12, 31):
        return None, f"Records start from {FIRST_DATE.strftime('%B %Y')}."
    if (end - start).days + 1 > MAX_RANGE_DAYS:
        return None, f"Choose a range of {MAX_RANGE_DAYS} days or fewer."
    return (start, end), None


def _period_choices(today=None):
    """Recent payroll periods, newest first: the one in progress, then closed ones."""
    start, end = get_current_payroll_period(today)
    out = []
    for i in range(PERIOD_CHOICES):
        out.append({"start": start.isoformat(), "end": end.isoformat(),
                    "label": f"{start.strftime('%b')} {start.day}, {start.year} – "
                             f"{end.strftime('%b')} {end.day}, {end.year}"
                             + (" (in progress)" if i == 0 else "")})
        start, end = get_previous_period(start, end)
    return out


def _id_key(employee_id):
    return (employee_id or "").strip().upper()


def _employee_names(employee_ids):
    """_id_key(employee_id) -> (first, last) from OrangeHRM; active row preferred."""
    if not employee_ids:
        return {}
    ids = sorted(employee_ids)
    names = {}
    conn = get_orangehrm_connection()
    try:
        with conn.cursor() as cur:
            placeholders = ",".join(["%s"] * len(ids))
            # Terminated rows first so the active row (termination_id NULL)
            # overwrites them below; ties fall to the newest emp_number.
            cur.execute(
                f"""SELECT employee_id, emp_firstname, emp_lastname
                    FROM hs_hr_employee
                    WHERE employee_id IN ({placeholders})
                    ORDER BY (termination_id IS NULL), emp_number""",
                ids,
            )
            for r in cur.fetchall():
                names[_id_key(r["employee_id"])] = (r["emp_firstname"] or "", r["emp_lastname"] or "")
    finally:
        conn.close()
    return names


@timestamp_records_bp.route("/admin/timestamp-records")
def timestamp_records_page():
    if not _logged_in():
        return redirect(url_for("login"))
    if not _can_view():
        flash("You don't have access to Timestamp Records.", "danger")
        return redirect(url_for("dashboard"))

    # Built without calling Drive so a slow Drive can't hang the page.
    # Default: the last completed payroll period, as on the Tardiness page.
    start, end = get_default_payroll_period()
    return render_template("timestamp_records/index.html",
                           periods=_period_choices(), sel_from=start.isoformat(), sel_to=end.isoformat(),
                           min_date=FIRST_DATE.isoformat(), max_days=MAX_RANGE_DAYS)


@timestamp_records_bp.route("/admin/timestamp-records/data")
def timestamp_records_data():
    if not _logged_in():
        return jsonify(ok=False, error="Your session has expired. Please log in again."), 401
    if not _can_view():
        return jsonify(ok=False, error="Access denied."), 403

    rng, err = _parse_range(request.args)
    if err:
        return jsonify(ok=False, error=err), 400
    start, end = rng
    refresh = request.args.get("refresh") == "1"

    try:
        result = dt.load_range(start, end, refresh=refresh)
    except dt.DriveError as e:
        return jsonify(ok=False, error=str(e)), 502

    rows = result["rows"]
    try:
        names = _employee_names({r["employee_id"] for r in rows})
    except Exception:
        log.exception("Timestamp Records: employee name lookup failed")
        return jsonify(ok=False, error="Could not load employee names from the database."), 500

    rows.sort(key=lambda r: (r["ts"], r["employee_id"]), reverse=True)
    out = []
    unmatched = set()
    for r in rows:
        first, last = names.get(_id_key(r["employee_id"]), ("", ""))
        if _id_key(r["employee_id"]) not in names:
            unmatched.add(r["employee_id"])
        out.append({
            "employee_id": r["employee_id"],
            "first_name": first,
            "last_name": last,
            "day": dt.fmt_day(r["ts"]),
            "time": dt.fmt_time(r["ts"]),
            "type": r["type"],
            "iso": r["ts"].strftime("%Y-%m-%dT%H:%M"),
        })

    resp = jsonify(
        ok=True,
        start=start.isoformat(),
        end=end.isoformat(),
        missing_months=result["missing_months"],
        duplicates=result["duplicates"],
        files=result["files"],
        downloaded=result["downloaded"],
        file_errors=[p for p in result["problems"] if p.get("whole_file")],
        problems=[p for p in result["problems"] if not p.get("whole_file")][:200],
        problem_count=sum(1 for p in result["problems"] if not p.get("whole_file")),
        unmatched_ids=sorted(unmatched),
        rows=out,
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp
