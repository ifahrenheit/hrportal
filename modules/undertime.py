"""
undertime.py
Undertime detection for the auto-IR cron (undertime_notify.py). Mirror of
modules/tardiness.py's get_tardiness_for_date(), for the OUT side of a shift.

Logic:
  - Same scheduled population as tardiness: employee_schedules joined via
    userdata.companyid, CWS moves applied, rest days skipped, unparseable
    shift_time skipped. Holidays are NOT excluded (same as tardiness).
  - Any filed leave that day skips the employee. Unlike tardiness (which
    only checks ohrm_leave status 1/3), Scheduled (2) counts too -- a leave
    filed for the day means an early OUT isn't undertime.
  - IN anchor: first raw 'in' punch in [shift_start - 4h, shift_end] (same
    window as tardiness), falling back to a Pending/Approved FTS IN. No IN
    at all -> not evaluated (absence/FTS, not undertime).
  - OUT: a Pending/Approved FTS OUT inside the OUT window overrides the raw
    punch (fts_date is the calendar date of the OUT itself, so an overnight
    shift's 7am OUT is filed against the next day). Otherwise the raw 'out'
    that closes the shift (_final_out): a mid-shift OUT followed by an IN
    doesn't count, and an OUT with no later OUT after a return IN means a
    missing punch, not undertime. The window runs from the IN to
    shift_end + 6h, capped at the next scheduled shift's start (never less
    than shift_end + 1h) -- which is also what puts an orphan next-morning
    OUT of an overnight shift on the shift's day.
  - minutes_early = whole minutes (shift_end - out); undertime if > 0.
    Same whole-minute rule as tardiness' minutes_late.
  - A shift is only evaluated once its OUT window has fully passed
    (NOT_SETTLED otherwise).

Reads the RAW dailytimerecord table, same as tardiness -- not
dailytimerecordsfiltered (which build_attendance_map() / the Undertime
Report page use), so numbers can differ slightly from that page.
"""

import bisect
from datetime import datetime, date, timedelta

from db_core import get_db_connection
from helpers.orangehrm_db import get_orangehrm_connection
from modules.tardiness import parse_shift_time, get_cws_moves_for_range

# How far past shift_end an OUT punch can still belong to the shift (OT).
OUT_WINDOW_AFTER_END = timedelta(hours=6)

# Same early-arrival buffer tardiness uses for the IN window; also used to
# stop this shift's OUT window before the next shift's IN window opens.
IN_WINDOW_BEFORE_START = timedelta(hours=4)

# Floor for the OUT window when the next shift starts soon after this one.
OUT_WINDOW_MIN_AFTER_END = timedelta(hours=1)

# A shift is only evaluated once its whole OUT window has passed, so an
# unfinished shift (or a not-yet-synced OUT) is never read as undertime.
SETTLE_AFTER_END = OUT_WINDOW_AFTER_END

FTS_ACTIVE_STATUSES = ("Pending", "Approved")


def get_employees_with_filed_leave(target_date: date):
    """
    employee_ids with any filed leave on target_date per ohrm_leave:
    1 = Pending Approval, 2 = Scheduled, 3 = Taken (-1 = Rejected is not
    counted, and cancelled leaves are deleted from ohrm_leave).
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
              AND ol.status IN (1, 2, 3)
            """,
            (target_date,),
        )
        return {row["employee_id"] for row in cur.fetchall()}
    finally:
        cur.close()
        conn.close()


def _final_out(person_ins, person_outs, time_in, shift_end, window_end):
    """
    Walks the punches after time_in up to window_end and returns the OUT
    that closes the shift, or None:
      - an IN before shift_end means the agent came back (break), so any
        earlier OUT is not the final one;
      - an IN at/after shift_end belongs to the next shift and ends the walk.
    So OUT 12:00 / IN 13:00 / no final OUT is None (missing punch), not a
    5-hour undertime.
    """
    events = sorted(
        [(t, "in") for t in person_ins if time_in < t <= window_end]
        + [(t, "out") for t in person_outs if time_in < t <= window_end]
    )
    last_out = None
    for t, kind in events:
        if kind == "in":
            if t >= shift_end:
                break
            last_out = None
        else:
            last_out = t
    return last_out


def get_fts_for_range(cur, employee_ids, date_from: date, date_to: date):
    """
    Pending/Approved, non-deleted FTS rows for employee_ids with fts_date in
    [date_from, date_to]. Returns {(employee_id, 'IN'|'OUT'): [datetime, ...]}
    sorted ascending, where datetime = fts_date + fts_time.
    """
    if not employee_ids:
        return {}
    placeholders = ", ".join(["%s"] * len(employee_ids))
    status_ph = ", ".join(["%s"] * len(FTS_ACTIVE_STATUSES))
    cur.execute(
        f"""
        SELECT employeeID AS employee_id, fts_date, fts_time, fts_type
        FROM fts_requests
        WHERE employeeID IN ({placeholders})
          AND fts_date BETWEEN %s AND %s
          AND status IN ({status_ph})
          AND deleted_at IS NULL
        """,
        (*employee_ids, date_from, date_to, *FTS_ACTIVE_STATUSES),
    )
    # Key by the id as requested, not as returned: the SQL match is
    # case/accent-insensitive (utf8mb4_0900_ai_ci), a dict lookup isn't.
    requested = {eid.strip().upper(): eid for eid in employee_ids}
    by_key = {}
    for row in cur.fetchall():
        if row["fts_time"] is None:
            continue
        eid = requested.get(row["employee_id"].strip().upper(), row["employee_id"])
        fts_dt = datetime.combine(row["fts_date"], datetime.min.time()) + row["fts_time"]
        by_key.setdefault((eid, row["fts_type"].upper()), []).append(fts_dt)
    for times in by_key.values():
        times.sort()
    return by_key


def _schedules_for_dates(cur, dates):
    """
    {date: {employee_id: sched_row}} for the given dates, with CWS moves
    applied the same way get_late_records_for_range() applies them
    (moved-in overrides shift_time and un-marks rest day; moved-in employees
    with no schedule row get one synthesized; moved-out are dropped).
    """
    date_from, date_to = min(dates), max(dates)
    cur.execute(
        """
        SELECT
            es.employee_id,
            es.schedule_date,
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
        WHERE es.schedule_date BETWEEN %s AND %s
          AND u.active = 1
        ORDER BY es.schedule_date, u.lname, u.fname
        """,
        (date_from, date_to),
    )
    by_date = {}
    for row in cur.fetchall():
        by_date.setdefault(row["schedule_date"], {})[row["employee_id"]] = row

    moved_out_by_date, moved_in_by_date = get_cws_moves_for_range(date_from, date_to)

    missing = {
        eid
        for d, moved_in in moved_in_by_date.items()
        for eid in moved_in
        if eid not in by_date.get(d, {})
    }
    synth_lookup = {}
    if missing:
        placeholders = ", ".join(["%s"] * len(missing))
        cur.execute(
            f"""
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
            WHERE u.companyid IN ({placeholders}) AND u.active = 1
            """,
            tuple(missing),
        )
        for row in cur.fetchall():
            synth_lookup[row["employee_id"]] = row

    for d, moved_in in moved_in_by_date.items():
        day = by_date.setdefault(d, {})
        for eid, new_time in moved_in.items():
            if eid in day:
                overridden = dict(day[eid])
                overridden["shift_time"] = new_time
                overridden["is_rest_day"] = 0
                day[eid] = overridden
            elif eid in synth_lookup:
                day[eid] = dict(synth_lookup[eid], schedule_date=d,
                                shift_time=new_time, is_rest_day=0)

    for d, moved_out in moved_out_by_date.items():
        for eid in moved_out:
            by_date.get(d, {}).pop(eid, None)

    return by_date


def get_undertime_for_date(target_date: date, now: datetime = None):
    """
    Returns a list of dicts, one per evaluated (scheduled, non-rest-day,
    no-leave, parseable) employee for target_date:
      status: UNDERTIME | ON_TIME | NO_IN | NO_OUT | NOT_SETTLED
      minutes_early, time_in, time_out, out_source ('PUNCH' | 'FTS')
    Callers only act on UNDERTIME; NOT_SETTLED means "try again later".
    """
    now = now or datetime.now()
    next_date = target_date + timedelta(days=1)

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        schedules = _schedules_for_dates(cur, [target_date, next_date])
        today_scheds = schedules.get(target_date, {})
        next_scheds = schedules.get(next_date, {})
        on_leave = get_employees_with_filed_leave(target_date)

        candidates = []
        for eid, sched in today_scheds.items():
            if sched["is_rest_day"] or eid in on_leave:
                continue
            parsed = parse_shift_time(sched["shift_time"], target_date)
            if not parsed:
                continue
            shift_start, shift_end = parsed

            # Cap at the next shift's START (not its early-IN window), and
            # never closer than OUT_WINDOW_MIN_AFTER_END past this shift's
            # end -- a tight rotation (10pm-7am then 10am-7pm) must still see
            # a normal 07:02 OUT. An early IN for the next shift is handled
            # separately by the walk below (an IN at/after shift_end ends it).
            out_window_end = shift_end + OUT_WINDOW_AFTER_END
            nxt = next_scheds.get(eid)
            if nxt and not nxt["is_rest_day"]:
                nxt_parsed = parse_shift_time(nxt["shift_time"], next_date)
                if nxt_parsed:
                    out_window_end = max(shift_end + OUT_WINDOW_MIN_AFTER_END,
                                         min(out_window_end, nxt_parsed[0]))

            candidates.append({
                "sched": sched,
                "shift_start": shift_start,
                "shift_end": shift_end,
                "in_window_start": shift_start - IN_WINDOW_BEFORE_START,
                "out_window_end": out_window_end,
            })

        if not candidates:
            return []

        personids = {c["sched"]["personid"] for c in candidates}
        lo = min(c["in_window_start"] for c in candidates)
        hi = max(c["out_window_end"] for c in candidates)
        placeholders = ", ".join(["%s"] * len(personids))
        cur.execute(
            f"""
            SELECT personid, date AS punch_time, type
            FROM dailytimerecord
            WHERE personid IN ({placeholders})
              AND type IN ('in', 'out')
              AND date BETWEEN %s AND %s
            ORDER BY personid, date ASC
            """,
            (*personids, lo, hi),
        )
        ins, outs = {}, {}
        for row in cur.fetchall():
            bucket = ins if row["type"].lower() == "in" else outs
            bucket.setdefault(row["personid"], []).append(row["punch_time"])

        fts = get_fts_for_range(
            cur, sorted({c["sched"]["employee_id"] for c in candidates}),
            # From the day before: a pre-4am shift's IN window starts there.
            target_date - timedelta(days=1), next_date,
        )

        results = []
        for c in candidates:
            s = c["sched"]
            base = {
                "personid": s["personid"],
                "companyid": s["employee_id"],
                "fname": s["fname"],
                "lname": s["lname"],
                "team_lead": s["team_lead"],
                "department": s["department"],
                "batch": s.get("batch"),
                "account": s.get("account"),
                "email": s.get("email"),
                "shift_time_raw": s["shift_time"],
                "shift_start": c["shift_start"],
                "shift_end": c["shift_end"],
                "time_in": None,
                "time_out": None,
                "out_source": None,
                "minutes_early": None,
            }

            if now < c["shift_end"] + SETTLE_AFTER_END:
                results.append(dict(base, status="NOT_SETTLED"))
                continue

            # IN anchor: first raw IN in the tardiness window, else FTS IN.
            person_ins = ins.get(s["personid"], [])
            idx = bisect.bisect_left(person_ins, c["in_window_start"])
            time_in = person_ins[idx] if idx < len(person_ins) and person_ins[idx] <= c["shift_end"] else None
            if time_in is None:
                fts_ins = [t for t in fts.get((s["employee_id"], "IN"), [])
                           if c["in_window_start"] <= t <= c["shift_end"]]
                time_in = fts_ins[0] if fts_ins else None
            if time_in is None:
                results.append(dict(base, status="NO_IN"))
                continue

            # OUT: FTS OUT wins over the punch; otherwise the shift's final
            # raw OUT (see _final_out).
            fts_outs = [t for t in fts.get((s["employee_id"], "OUT"), [])
                        if time_in < t <= c["out_window_end"]]
            if fts_outs:
                time_out, out_source = fts_outs[-1], "FTS"
            else:
                time_out = _final_out(ins.get(s["personid"], []), outs.get(s["personid"], []),
                                      time_in, c["shift_end"], c["out_window_end"])
                out_source = "PUNCH" if time_out else None
            if time_out is None:
                results.append(dict(base, status="NO_OUT", time_in=time_in))
                continue

            minutes_early = int((c["shift_end"] - time_out).total_seconds() // 60)
            if minutes_early > 0:
                status = "UNDERTIME"
            else:
                status = "ON_TIME"
                minutes_early = 0

            results.append(dict(base, status=status, time_in=time_in,
                                time_out=time_out, out_source=out_source,
                                minutes_early=minutes_early))
        return results
    finally:
        cur.close()
        conn.close()
