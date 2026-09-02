"""
tardiness.py
Flask blueprint for the Tardiness page.

Logic:
  - For a given date, find all employees who have a schedule that day
    (employee_schedules, joined via userdata.companyid).
  - Skip rest days (is_rest_day = 1) and unparseable shift_time values.
  - Find their first 'in' record for that day (dailytimerecord).
  - Late = time_in > shift_start. minutes_late = time_in - shift_start.
  - No grace period.

Register in app.py:

    from tardiness import tardiness_bp
    app.register_blueprint(tardiness_bp)
"""

import re
from datetime import datetime, date, timedelta

from flask import Blueprint, render_template, request, session, redirect, url_for, flash
from db_core import get_db_connection
from orangehrm_db import get_orangehrm_connection

tardiness_bp = Blueprint("tardiness", __name__, url_prefix="/tardiness")


# ---------------------------------------------------------------------------
# Shift time parsing
# ---------------------------------------------------------------------------

# Matches things like: 2pm, 10am, 12nn, 12mn, 2:30pm, 6 pm
_TIME_RE = re.compile(
    r"^\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm|nn|mn)\s*$",
    re.IGNORECASE,
)


def _parse_clock_part(part: str):
    """Parse a single time token like '2pm', '12nn', '6:30am' into (hour, minute)."""
    m = _TIME_RE.match(part)
    if not m:
        return None

    hour = int(m.group(1))
    minute = int(m.group(2)) if m.group(2) else 0
    suffix = m.group(3).lower()

    if suffix == "nn":  # 12 noon
        hour = 12
    elif suffix == "mn":  # 12 midnight
        hour = 0
    elif suffix == "am":
        if hour == 12:
            hour = 0
    elif suffix == "pm":
        if hour != 12:
            hour += 12

    if hour > 23 or minute > 59:
        return None

    return hour, minute


def parse_shift_time(shift_time: str, schedule_date: date):
    """
    Parse a shift_time string like '2pm-11pm' or '10pm-7am' into
    (shift_start_datetime, shift_end_datetime), handling overnight shifts.

    Returns None if the value can't be parsed (e.g. '#REF!').
    """
    if not shift_time:
        return None

    cleaned = shift_time.replace(" ", "").lower()
    parts = cleaned.split("-")
    if len(parts) != 2:
        return None

    start_parsed = _parse_clock_part(parts[0])
    end_parsed = _parse_clock_part(parts[1])
    if not start_parsed or not end_parsed:
        return None

    start_h, start_m = start_parsed
    end_h, end_m = end_parsed

    shift_start = datetime.combine(schedule_date, datetime.min.time()).replace(
        hour=start_h, minute=start_m
    )
    shift_end = datetime.combine(schedule_date, datetime.min.time()).replace(
        hour=end_h, minute=end_m
    )

    # Overnight shift: end time is "earlier" than start time on the clock,
    # so it actually falls on the next day.
    if shift_end <= shift_start:
        shift_end += timedelta(days=1)

    return shift_start, shift_end


# ---------------------------------------------------------------------------
# Data access
# ---------------------------------------------------------------------------

def get_employees_on_leave(target_date: date):
    """
    Returns a set of companyid/employee_id strings for employees who have
    a Pending or Approved leave record on target_date, per ohrm_leave in
    the orangehrm2 database (separate DB/credentials from central_db).

    Status codes (OrangeHRM standard):
        1 = Pending Approval
        2 = Rejected
        3 = Approved
        4 = Cancelled
    We exclude on 1 and 3 (Pending or Approved) per the requirement that
    employees on leave -- even half-day -- shouldn't show as Late, since
    their actual shift expectations for that day are no longer the normal
    full-day schedule.
    """
    conn = get_orangehrm_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT DISTINCT h.employee_id
            FROM ohrm_leave ol
            JOIN hs_hr_employee h ON h.emp_number = ol.emp_number
            WHERE ol.date = %s
              AND ol.status IN (1, 3)
            """,
            (target_date,),
        )
        return {row["employee_id"] for row in cur.fetchall()}
    finally:
        cur.close()
        conn.close()


def get_cws_moves_for_date(target_date: date):
    """
    Returns (moved_out, moved_in) for target_date, based on cws_requests
    (Change Work Schedule). A CWS moves a shift from original_date to
    new_date, optionally with a different time:

      moved_out: set of employee_id whose shift on target_date was moved
                 AWAY to a different date. They should be skipped entirely
                 on target_date (treated like a rest day) -- their shift
                 expectation here no longer exists.

      moved_in: dict of employee_id -> new_time (string) for employees
                whose shift was moved INTO target_date from elsewhere.
                Their tardiness on target_date should be evaluated against
                new_time, not whatever employee_schedules says (which may
                not even have a row for them on this date at all).

    Both Pending and Approved requests count, per the same policy as
    leave -- a request doesn't need final approval to affect the report.
    """
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT employee_id, original_date, new_date, new_time
            FROM cws_requests
            WHERE status IN ('Pending', 'Approved')
              AND (original_date = %s OR new_date = %s)
            """,
            (target_date, target_date),
        )
        rows = cur.fetchall()
    finally:
        cur.close()
        conn.close()

    moved_out = set()
    moved_in = {}

    for row in rows:
        if row["original_date"] == target_date:
            moved_out.add(row["employee_id"])
        if row["new_date"] == target_date:
            moved_in[row["employee_id"]] = row["new_time"]

    return moved_out, moved_in


def get_tardiness_for_date(target_date: date):
    """
    Returns a list of dicts, one per scheduled (non-rest-day) employee for
    target_date, with schedule info, actual time-in, and lateness.
    """
    conn = get_db_connection()
    cur = conn.cursor()  # adjust if using a different driver

    cur.execute(
        """
        SELECT
            es.employee_id,
            es.shift_time,
            es.is_rest_day,
            u.personid,
            u.fname,
            u.lname,
            ge.tl AS team_lead,
            ge.group_name AS department,
            ge.batch AS batch,
            ge.account AS account,
            ge.email AS email
        FROM employee_schedules es
        JOIN userdata u ON u.companyid = es.employee_id
        LEFT JOIN gsheet_employees ge ON ge.employee_id = es.employee_id
        WHERE es.schedule_date = %s
          AND u.active = 1
        ORDER BY u.lname, u.fname
        """,
        (target_date,),
    )
    schedules = cur.fetchall()

    employees_on_leave = get_employees_on_leave(target_date)
    moved_out, moved_in = get_cws_moves_for_date(target_date)

    # Apply CWS overrides to the schedules list before the main loop:
    #   - For employees already scheduled here AND moved in, override their
    #     shift_time with the new CWS time (and un-mark rest day, since
    #     they're now expected to work this day).
    #   - For employees moved in who have NO employee_schedules row for
    #     this date at all (most common case -- they weren't originally
    #     scheduled here), synthesize one so they still get evaluated.
    schedules_by_employee = {s["employee_id"]: s for s in schedules}

    for employee_id, new_time in moved_in.items():
        if employee_id in schedules_by_employee:
            schedules_by_employee[employee_id]["shift_time"] = new_time
            schedules_by_employee[employee_id]["is_rest_day"] = 0
        else:
            # No normal schedule row for this date -- look up the employee
            # so we can synthesize one using the CWS new_time.
            cur.execute(
                """
                SELECT
                    u.companyid AS employee_id,
                    u.personid,
                    u.fname,
                    u.lname,
                    ge.tl AS team_lead,
                    ge.group_name AS department,
                    ge.batch AS batch,
                    ge.account AS account,
                    ge.email AS email
                FROM userdata u
                LEFT JOIN gsheet_employees ge ON ge.employee_id = u.companyid
                WHERE u.companyid = %s AND u.active = 1
                """,
                (employee_id,),
            )
            emp_row = cur.fetchone()
            if emp_row:
                schedules_by_employee[employee_id] = {
                    "employee_id": emp_row["employee_id"],
                    "shift_time": new_time,
                    "is_rest_day": 0,
                    "personid": emp_row["personid"],
                    "fname": emp_row["fname"],
                    "lname": emp_row["lname"],
                    "team_lead": emp_row["team_lead"],
                    "department": emp_row["department"],
                    "batch": emp_row["batch"],
                    "account": emp_row["account"],
                    "email": emp_row["email"],
                }

    schedules = list(schedules_by_employee.values())

    results = []

    for sched in schedules:
        if sched["is_rest_day"]:
            continue  # skip rest days entirely

        if sched["employee_id"] in employees_on_leave:
            continue  # on Pending/Approved leave - don't evaluate tardiness at all

        if sched["employee_id"] in moved_out:
            continue  # shift moved to a different date via CWS - not working here at all

        parsed = parse_shift_time(sched["shift_time"], target_date)
        if not parsed:
            # Unparseable shift_time (e.g. '#REF!' or NULL with is_rest_day=0)
            results.append({
                "personid": sched["personid"],
                "companyid": sched["employee_id"],
                "fname": sched["fname"],
                "lname": sched["lname"],
                "team_lead": sched["team_lead"],
                "department": sched["department"],
                "batch": sched.get("batch"),
                "account": sched.get("account"),
                "email": sched.get("email"),
                "shift_time_raw": sched["shift_time"],
                "shift_start": None,
                "time_in": None,
                "status": "INVALID_SCHEDULE",
                "minutes_late": None,
            })
            continue

        shift_start, shift_end = parsed

        # Find the first 'in' punch for this person within the shift window.
        # We search from a bit before shift_start to shift_end, to catch
        # early arrivals and overnight shifts correctly.
        window_start = shift_start - timedelta(hours=4)
        window_end = shift_end

        cur.execute(
            """
            SELECT date AS punch_time
            FROM dailytimerecord
            WHERE personid = %s
              AND type = 'in'
              AND date BETWEEN %s AND %s
            ORDER BY date ASC
            LIMIT 1
            """,
            (sched["personid"], window_start, window_end),
        )
        punch = cur.fetchone()

        time_in = punch["punch_time"] if punch else None

        if time_in is None:
            status = "ABSENT"
            minutes_late = None
        else:
            minutes_late = int((time_in - shift_start).total_seconds() // 60)
            if minutes_late > 0:
                status = "LATE"
            else:
                status = "ON_TIME"
                minutes_late = 0

        results.append({
            "personid": sched["personid"],
            "companyid": sched["employee_id"],
            "fname": sched["fname"],
            "lname": sched["lname"],
            "team_lead": sched["team_lead"],
            "department": sched["department"],
            "batch": sched.get("batch"),
            "account": sched.get("account"),
            "email": sched.get("email"),
            "shift_time_raw": sched["shift_time"],
            "shift_start": shift_start,
            "time_in": time_in,
            "status": status,
            "minutes_late": minutes_late,
        })

    cur.close()
    conn.close()

    return results


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

# Used by the "Status" column sort: groups records by actual status value,
# not just a single status flag, so Late/Invalid/Absent/On Time all cluster
# together properly instead of looking like nothing happened when there are
# few or no absences.
_STATUS_ORDER = {"LATE": 0, "INVALID_SCHEDULE": 1, "ABSENT": 2, "ON_TIME": 3}


# ---------------------------------------------------------------------------
# Payroll period helpers now live in payroll_period.py (shared with the
# tardiness_notify.py cron script). Imported here for backward compatibility
# with the rest of this file's code, which calls these names directly.
# ---------------------------------------------------------------------------
from payroll_period import (
    get_payroll_period_for_date,
    get_previous_period,
    get_next_period,
    get_default_payroll_period,
)


def get_late_records_for_range(date_from: date, date_to: date):
    """
    Loops every day in [date_from, date_to] (inclusive), pulls that day's
    tardiness records, and keeps only the LATE ones. Each record gets a
    'record_date' field added so the range view can show which day each
    late instance happened on.

    Capped at 60 days to avoid an accidental huge range hammering the DB
    with one query per day.
    """
    if date_to < date_from:
        date_from, date_to = date_to, date_from

    max_days = 60
    if (date_to - date_from).days > max_days:
        date_to = date_from + timedelta(days=max_days)

    all_late = []
    current = date_from
    while current <= date_to:
        day_records = get_tardiness_for_date(current)
        for r in day_records:
            if r["status"] == "LATE":
                r = dict(r)  # copy, don't mutate the original
                r["record_date"] = current
                all_late.append(r)
        current += timedelta(days=1)

    return all_late


def _sort_late_records(records, sort_by, direction):
    reverse = (direction == "desc")

    if sort_by == "LATE":
        records.sort(key=lambda x: -(x["minutes_late"] or 0), reverse=reverse)
    elif sort_by == "SHIFT":
        records.sort(key=lambda x: (x["shift_start"] is None, x["shift_start"]), reverse=reverse)
    elif sort_by == "TL":
        records.sort(key=lambda x: ((x["team_lead"] or "").lower(), (x["lname"] or "").lower()), reverse=reverse)
    elif sort_by == "DATE":
        records.sort(key=lambda x: x["record_date"], reverse=reverse)
    else:
        # Default: most frequent latecomers first (by count), then by lateness
        records.sort(key=lambda x: ((x["lname"] or "").lower(), (x["fname"] or "").lower(), x["record_date"]), reverse=reverse)

    return records


# ---------------------------------------------------------------------------
# Memo Triggers (Tardiness Notification preview)
#
# This mirrors tardiness_notify.py's dry_run_range() simulation exactly --
# same thresholds, same reset-after-trigger logic -- so what's shown here
# always matches what the actual email cron would fire for the same range.
# Kept here too (rather than importing from tardiness_notify.py) since that
# script also sets up SMTP config at import time; better to keep this page
# fully self-contained.
# ---------------------------------------------------------------------------

_TRIGGER_COUNT_THRESHOLD = 3
_TRIGGER_MINUTES_THRESHOLD = 31


def get_trigger_events_for_range(date_from: date, date_to: date):
    """
    Simulates the same day-by-day counter logic as the notification cron,
    in-memory only, and returns the list of trigger events that would fire
    for this date range. Does NOT touch tardiness_cycle_state and does NOT
    send any email -- purely a preview/report.
    """
    if date_to < date_from:
        date_from, date_to = date_to, date_from

    sim_state = {}
    events = []

    current = date_from
    while current <= date_to:
        day_records = get_tardiness_for_date(current)
        late_records = [r for r in day_records if r["status"] == "LATE"]

        for r in late_records:
            personid = r["personid"]
            minutes_late = r["minutes_late"] or 0
            time_in_str = r["time_in"].strftime("%I:%M %p") if r.get("time_in") else None

            if personid not in sim_state:
                sim_state[personid] = {
                    "fname": r["fname"], "lname": r["lname"], "companyid": r["companyid"],
                    "team_lead": r["team_lead"], "department": r["department"],
                    "batch": r.get("batch"), "account": r.get("account"), "email": r.get("email"),
                    "count_since_reset": 0, "minutes_since_reset": 0,
                    "total_count_in_cycle": 0, "total_minutes_in_cycle": 0,
                    "count_breakdown": [], "minutes_breakdown": [],
                }
            s = sim_state[personid]
            entry = {"date": current, "minutes": minutes_late, "time_in": time_in_str}
            s["count_since_reset"] += 1
            s["minutes_since_reset"] += minutes_late
            s["total_count_in_cycle"] += 1
            s["total_minutes_in_cycle"] += minutes_late
            s["count_breakdown"].append(entry)
            s["minutes_breakdown"].append(entry)

            if s["count_since_reset"] >= _TRIGGER_COUNT_THRESHOLD:
                events.append({
                    "event_date": current, "trigger_type": "COUNT",
                    "personid": personid, "fname": s["fname"], "lname": s["lname"],
                    "companyid": s["companyid"], "team_lead": s["team_lead"], "department": s["department"],
                    "batch": s["batch"], "account": s["account"], "email": s["email"],
                    "detail": f"{s['total_count_in_cycle']} lates in cycle",
                    "value": s["total_count_in_cycle"],
                    "breakdown": list(s["count_breakdown"]),
                    "trigger_total": len(s["count_breakdown"]),
                })
                s["count_since_reset"] = 0
                s["count_breakdown"] = []

            if s["minutes_since_reset"] >= _TRIGGER_MINUTES_THRESHOLD:
                events.append({
                    "event_date": current, "trigger_type": "MINUTES",
                    "personid": personid, "fname": s["fname"], "lname": s["lname"],
                    "companyid": s["companyid"], "team_lead": s["team_lead"], "department": s["department"],
                    "batch": s["batch"], "account": s["account"], "email": s["email"],
                    "detail": f"{s['total_minutes_in_cycle']} min in cycle",
                    "value": s["total_minutes_in_cycle"],
                    "breakdown": list(s["minutes_breakdown"]),
                    "trigger_total": sum(e["minutes"] for e in s["minutes_breakdown"]),
                })
                s["minutes_since_reset"] = 0
                s["minutes_breakdown"] = []

        current += timedelta(days=1)

    return events


def _sort_trigger_events(events, sort_by, direction):
    reverse = (direction == "desc")

    if sort_by == "TL":
        events.sort(key=lambda x: ((x["team_lead"] or "").lower(), (x["lname"] or "").lower()), reverse=reverse)
    elif sort_by == "TYPE":
        events.sort(key=lambda x: x["trigger_type"], reverse=reverse)
    elif sort_by == "VALUE":
        events.sort(key=lambda x: x["value"], reverse=reverse)
    elif sort_by == "lname":
        events.sort(key=lambda x: ((x["lname"] or "").lower(), (x["fname"] or "").lower()), reverse=reverse)
    else:  # DATE (default)
        events.sort(key=lambda x: x["event_date"], reverse=reverse)

    return events


def _check_tardiness_permission():
    """
    Mirrors app.py's permission_required('can_tardiness') logic inline,
    to avoid a circular import (app.py imports tardiness_bp at module load
    time, before permission_required is defined further down in app.py).
    Returns a redirect Response if access should be denied, or None if OK.
    """
    if not session.get("user"):
        return redirect(url_for("login"))
    if not session.get("is_admin") and not session.get("permissions", {}).get("can_tardiness", False):
        flash("You do not have permission to access this page.", "danger")
        return redirect(url_for("dashboard"))
    return None


@tardiness_bp.route("/", methods=["GET"])
def tardiness_page():
    denied = _check_tardiness_permission()
    if denied:
        return denied

    default_period_start, default_period_end = get_default_payroll_period()

    date_from_str = request.args.get("date_from", "").strip()
    date_to_str = request.args.get("date_to", "").strip()

    try:
        date_from = datetime.strptime(date_from_str, "%Y-%m-%d").date() if date_from_str else default_period_start
        date_to = datetime.strptime(date_to_str, "%Y-%m-%d").date() if date_to_str else default_period_end
    except ValueError:
        date_from, date_to = default_period_start, default_period_end

    view = request.args.get("view", "late")
    if view not in ("late", "triggers"):
        view = "late"

    department_filter = request.args.get("department", "").strip()

    if view == "triggers":
        sort_by = request.args.get("sort", "DATE")
        direction = request.args.get("dir", "asc")
        next_dir = "desc" if direction == "asc" else "asc"

        events = get_trigger_events_for_range(date_from, date_to)

        available_departments = sorted({e["department"] for e in events if e["department"]})
        if department_filter:
            events = [e for e in events if (e["department"] or "") == department_filter]

        events = _sort_trigger_events(events, sort_by, direction)

        count_trigger_total = sum(1 for e in events if e["trigger_type"] == "COUNT")
        minutes_trigger_total = sum(1 for e in events if e["trigger_type"] == "MINUTES")

        return render_template(
            "tardiness.html",
            view=view,
            events=events,
            date_from=date_from.isoformat(),
            date_to=date_to.isoformat(),
            count_trigger_total=count_trigger_total,
            minutes_trigger_total=minutes_trigger_total,
            current_sort=sort_by,
            current_dir=direction,
            next_dir=next_dir,
            available_departments=available_departments,
            department_filter=department_filter,
        )

    sort_by = request.args.get("sort", "DATE")
    direction = request.args.get("dir", "asc")
    next_dir = "desc" if direction == "asc" else "asc"

    records = get_late_records_for_range(date_from, date_to)

    available_departments = sorted({r["department"] for r in records if r["department"]})
    if department_filter:
        records = [r for r in records if (r["department"] or "") == department_filter]

    records = _sort_late_records(records, sort_by, direction)

    return render_template(
        "tardiness.html",
        view=view,
        records=records,
        date_from=date_from.isoformat(),
        date_to=date_to.isoformat(),
        total_late=len(records),
        current_sort=sort_by,
        current_dir=direction,
        next_dir=next_dir,
        available_departments=available_departments,
        department_filter=department_filter,
    )