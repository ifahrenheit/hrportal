"""
undertime_notify.py
Standalone daily cron script -- mirror of tardiness_notify.py for UNDERTIME
(OUT punch earlier than the scheduled shift end; see modules/undertime.py
for how the OUT is determined, incl. the FTS OUT override).

DRY-RUN BY DEFAULT. Nothing is written, filed or emailed without --live.

    # cron (processes yesterday, live):
    30 18 * * * /var/www/html/leavesystem/venv/bin/python /var/www/html/leavesystem/undertime_notify.py --live >> /var/www/html/leavesystem/undertime_notify.log 2>&1

    python3 undertime_notify.py                         # dry run, yesterday
    python3 undertime_notify.py 2026-09-20              # dry run, that day
    python3 undertime_notify.py --live [2026-09-20]     # real run
    python3 undertime_notify.py --dry-run 2026-08-23 2026-09-07   # range sim
    python3 undertime_notify.py --test-email [addr]     # sample emails only

18:30 is late enough for yesterday's whole OUT window (shift_end + 6h,
modules/undertime.py SETTLE_AFTER_END) to have passed for any shift ending
by 12:30 today -- e.g. an 11pm-10am overnight shift settles at 16:00. Raw
punches sync within minutes. A shift still unsettled is logged and skipped,
never counted.

Same rules as tardiness, per payroll cycle, keyed by (employee_id,
period_start) in undertime_cycle_state:
  - count_since_reset   -> triggers at 3 undertime days, then resets
  - minutes_since_reset -> triggers at 31+ minutes (i.e. more than 30),
                           then resets
Each trigger auto-files an Incident Report (helpers/ir_autofile.py, routed
to the supervisor via supervisor_mapping) and sends a memo email to the TL
(tl_view_map) + UNDERTIME_NOTIFY_RECIPIENTS + BCC -- one email per
recipient. Several IRs per cycle are possible, same as tardiness; if both
rules fire on the same day only one IR is filed.
"""

import os
import json
import smtplib
import sys
from datetime import date, datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from dotenv import load_dotenv

load_dotenv('/var/www/html/leavesystem/.env')

from db_core import get_db_connection
from helpers.payroll_period import get_payroll_period_for_date
from modules.undertime import get_undertime_for_date
from helpers.ir_autofile import file_incident_report, ir_notice_html

LOG = "[undertime_notify]"

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SMTP_SERVER = os.environ.get("SMTP_SERVER")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")

UNDERTIME_SENDER_NAME = os.environ.get("UNDERTIME_SENDER_NAME", "Undertime Email Alert")

# Falls back to the tardiness list so the same people are notified until a
# dedicated UNDERTIME_NOTIFY_RECIPIENTS is set in .env.
STATIC_RECIPIENTS = [
    e.strip() for e in os.environ.get(
        "UNDERTIME_NOTIFY_RECIPIENTS", os.environ.get("TARDINESS_NOTIFY_RECIPIENTS", "")
    ).split(",") if e.strip()
]
BCC_RECIPIENTS = [
    e.strip() for e in os.environ.get(
        "UNDERTIME_NOTIFY_BCC", os.environ.get("TARDINESS_NOTIFY_BCC", "andrewvincentt@gmail.com")
    ).split(",") if e.strip()
]

COUNT_THRESHOLD = 3
MINUTES_THRESHOLD = 31

PROCESS_DATE = date.today() - timedelta(days=1)

# Go-live date: live runs never process (count, file or email) a shift date
# before this, so no IRs are filed retroactively. Dry runs are unaffected.
LIVE_START_DATE = date.fromisoformat(os.environ.get("UNDERTIME_LIVE_START", "2026-09-28"))


# ---------------------------------------------------------------------------
# Email sending -- one message per recipient
# ---------------------------------------------------------------------------

def send_email(to_address, subject, body_html):
    """Sends one message to exactly one recipient."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{UNDERTIME_SENDER_NAME} <{SMTP_USER}>"
    msg["To"] = to_address
    msg.attach(MIMEText(body_html, "html"))

    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(SMTP_USER, [to_address], msg.as_string())


def send_to_all(recipients, subject, body_html, live):
    """
    recipients: ordered, de-duplicated list (TL first, then static list,
    then BCC). Each gets its own message; a failure for one recipient is
    logged and doesn't stop the others or the run.
    """
    for rcpt in recipients:
        if not live:
            print(f"{LOG}[DRY RUN] Would email '{subject}' to {rcpt}")
            continue
        try:
            send_email(rcpt, subject, body_html)
            print(f"{LOG} Sent '{subject}' to {rcpt}")
        except Exception as e:
            print(f"{LOG} Email to {rcpt} failed: {e}")


# ---------------------------------------------------------------------------
# Recipient lookup (TL via tl_view_map, same as tardiness)
# ---------------------------------------------------------------------------

def get_team_lead_email(cur, companyid):
    cur.execute(
        """
        SELECT m.login_email
        FROM gsheet_employees ge
        JOIN tl_view_map m ON m.tl_name = ge.tl
        WHERE ge.employee_id = %s
        LIMIT 1
        """,
        (companyid,),
    )
    row = cur.fetchone()
    return row["login_email"] if row else None


def build_recipient_list(cur, companyid):
    """Returns (recipients, tl_email); recipients de-duplicated, TL first."""
    tl_email = get_team_lead_email(cur, companyid)
    seen, recipients = set(), []
    for addr in [tl_email] + STATIC_RECIPIENTS + BCC_RECIPIENTS:
        if addr and addr.lower() not in seen:
            seen.add(addr.lower())
            recipients.append(addr)
    return recipients, tl_email


# ---------------------------------------------------------------------------
# Email + IR content
# ---------------------------------------------------------------------------

def _entry_line(entry, with_minutes=True):
    d = entry["date"]
    d_str = d.strftime("%B %d") if hasattr(d, "strftime") else str(d)
    out = entry.get("time_out") or "—"
    src = " via FTS" if entry.get("out_source") == "FTS" else ""
    if with_minutes:
        return f"{d_str} - {entry['minutes']} min early (Time Out: {out}{src}, Shift End: {entry.get('shift_end') or '—'})"
    return f"{d_str} - Time Out: {out}{src} (Shift End: {entry.get('shift_end') or '—'})"


def _format_breakdown_html(breakdown):
    if not breakdown:
        return "<p style='color:#888;'>No breakdown available.</p>"
    items = "".join(f"<li>{_entry_line(e)}</li>" for e in breakdown)
    return f"<ul style='margin:8px 0;padding-left:20px;'>{items}</ul>"


def build_ir_summary_count(fname, lname, breakdown):
    details = "\n".join(_entry_line(e, with_minutes=False) for e in breakdown)
    return (
        f"I would like to file an attendance incident report and request the issuance of a "
        f"Request for Written Explanation for agent {fname} {lname}. Per company policy, "
        f"accumulating three instances of undertime within a single payroll cycle requires "
        f"formal intervention. {fname} has reached this threshold during the current cycle.\n\n"
        f"Incident Details:\n{details}"
    )


def build_ir_summary_minutes(fname, lname, total_minutes_in_cycle, breakdown):
    details = "\n".join(_entry_line(e) for e in breakdown)
    return (
        f"I would like to file an attendance incident report and request the issuance of a "
        f"Request for Written Explanation for agent {fname} {lname}. Per company policy, "
        f"accumulating more than 30 minutes of undertime within a single payroll cycle requires "
        f"formal intervention. {fname} has reached this threshold during the current cycle, "
        f"with {total_minutes_in_cycle} minutes total.\n\n"
        f"Incident Details:\n{details}"
    )


def build_count_email(fname, lname, companyid, period_start, period_end, total_count_in_cycle,
                      tl_email, breakdown=None, report_number=None):
    subject = f"Undertime Memo: {lname}, {fname} - 3 Undertimes Recorded ({period_start} to {period_end})"
    body = f"""
    <p>This is an automated undertime memo notice.</p>
    <p><strong>{lname}, {fname}</strong> ({companyid}) has reached <strong>3 undertime instances</strong>
    within the current payroll cycle ({period_start.strftime('%B %d')} - {period_end.strftime('%B %d, %Y')}).</p>
    {ir_notice_html(report_number) if report_number else ''}
    <p>Breakdown of the 3 undertime instances triggering this memo:</p>
    {_format_breakdown_html(breakdown)}
    <p>Total undertime instances so far this cycle: <strong>{total_count_in_cycle}</strong></p>
    <p>Team Lead on file: {tl_email or 'Not found in tl_view_map'}</p>
    <hr>
    <p style="color:#888;font-size:12px;">This is an automated message from the Undertime Report system. This counter resets after every 3rd undertime instance, so this memo may repeat later in the same cycle if undertime continues.</p>
    """
    return subject, body


def build_minutes_email(fname, lname, companyid, period_start, period_end, total_minutes_in_cycle,
                        tl_email, breakdown=None, report_number=None):
    subject = f"Undertime Memo: {lname}, {fname} - 31+ Minutes Accumulated ({period_start} to {period_end})"
    triggering_minutes = sum(e["minutes"] for e in (breakdown or []))
    body = f"""
    <p>This is an automated undertime memo notice.</p>
    <p><strong>{lname}, {fname}</strong> ({companyid}) has accumulated <strong>more than 30 minutes</strong>
    of total undertime within the current payroll cycle ({period_start.strftime('%B %d')} - {period_end.strftime('%B %d, %Y')}).</p>
    {ir_notice_html(report_number) if report_number else ''}
    <p><strong>{triggering_minutes} minutes total</strong> - breakdown of the undertime instances triggering this memo:</p>
    {_format_breakdown_html(breakdown)}
    <p>Total undertime minutes so far this cycle: <strong>{total_minutes_in_cycle}</strong></p>
    <p>Team Lead on file: {tl_email or 'Not found in tl_view_map'}</p>
    <hr>
    <p style="color:#888;font-size:12px;">This is an automated message from the Undertime Report system. This counter resets after crossing the 31-minute mark, so this memo may repeat later in the same cycle if undertime continues.</p>
    """
    return subject, body


# ---------------------------------------------------------------------------
# State (undertime_cycle_state)
# ---------------------------------------------------------------------------

def _new_state(employee_id, period_start):
    return {
        "employee_id": employee_id,
        "period_start": period_start,
        "count_since_reset": 0,
        "minutes_since_reset": 0,
        "total_count_in_cycle": 0,
        "total_minutes_in_cycle": 0,
        "last_processed_date": None,
        "count_triggers_sent": 0,
        "minutes_triggers_sent": 0,
        "count_breakdown": [],
        "minutes_breakdown": [],
    }


def get_or_create_state(cur, employee_id, period_start):
    cur.execute(
        "SELECT * FROM undertime_cycle_state WHERE employee_id = %s AND period_start = %s",
        (employee_id, period_start),
    )
    row = cur.fetchone()
    if row:
        row["count_breakdown"] = _deserialize_breakdown(row.get("count_breakdown"))
        row["minutes_breakdown"] = _deserialize_breakdown(row.get("minutes_breakdown"))
        return row
    return _new_state(employee_id, period_start)


_ENTRY_KEYS = ("minutes", "time_out", "shift_end", "out_source")


def _serialize_breakdown(breakdown):
    items = []
    for e in breakdown:
        d = e["date"]
        item = {"date": d.isoformat() if hasattr(d, "isoformat") else str(d)}
        item.update({k: e.get(k) for k in _ENTRY_KEYS})
        items.append(item)
    return json.dumps(items)


def _deserialize_breakdown(raw):
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except (TypeError, ValueError):
        return []
    result = []
    for e in items:
        try:
            d = date.fromisoformat(e["date"])
        except (KeyError, ValueError, TypeError):
            continue
        entry = {"date": d}
        entry.update({k: e.get(k) for k in _ENTRY_KEYS})
        result.append(entry)
    return result


def save_state(cur, s):
    values = (
        s["count_since_reset"], s["minutes_since_reset"],
        s["total_count_in_cycle"], s["total_minutes_in_cycle"],
        s["last_processed_date"], s["count_triggers_sent"], s["minutes_triggers_sent"],
        _serialize_breakdown(s["count_breakdown"]), _serialize_breakdown(s["minutes_breakdown"]),
    )
    cur.execute(
        """
        INSERT INTO undertime_cycle_state
            (employee_id, period_start, count_since_reset, minutes_since_reset,
             total_count_in_cycle, total_minutes_in_cycle, last_processed_date,
             count_triggers_sent, minutes_triggers_sent, count_breakdown, minutes_breakdown)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            count_since_reset=%s, minutes_since_reset=%s,
            total_count_in_cycle=%s, total_minutes_in_cycle=%s,
            last_processed_date=%s, count_triggers_sent=%s, minutes_triggers_sent=%s,
            count_breakdown=%s, minutes_breakdown=%s
        """,
        (s["employee_id"], s["period_start"], *values, *values),
    )


# ---------------------------------------------------------------------------
# Shared rule step (used by process_day and dry_run_range)
# ---------------------------------------------------------------------------

def _entry_for(r, process_date):
    return {
        "date": process_date,
        "minutes": r["minutes_early"] or 0,
        "time_out": r["time_out"].strftime("%I:%M %p") if r.get("time_out") else None,
        "shift_end": r["shift_end"].strftime("%I:%M %p") if r.get("shift_end") else None,
        "out_source": r.get("out_source"),
    }


def apply_rules(cur, s, r, process_date, period_start, period_end, live):
    """
    Adds one undertime day to state s and fires any triggers. Returns a list
    of fired trigger types. Files at most one IR per call (both rules firing
    the same day share it), exactly like tardiness_notify.process_day().
    """
    companyid, fname, lname = r["companyid"], r["fname"], r["lname"]
    entry = _entry_for(r, process_date)

    s["count_since_reset"] += 1
    s["minutes_since_reset"] += entry["minutes"]
    s["total_count_in_cycle"] += 1
    s["total_minutes_in_cycle"] += entry["minutes"]
    s["last_processed_date"] = process_date
    s["count_breakdown"].append(entry)
    s["minutes_breakdown"].append(entry)

    fired = []
    report_number_today = None
    recipients, tl_email = build_recipient_list(cur, companyid)

    if s["count_since_reset"] >= COUNT_THRESHOLD:
        summary = build_ir_summary_count(fname, lname, s["count_breakdown"])
        report_number_today = file_incident_report(
            cur, companyid, f"{fname} {lname}", process_date, summary,
            dry_run=not live, log_prefix=LOG,
        )
        subject, body = build_count_email(
            fname, lname, companyid, period_start, period_end,
            s["total_count_in_cycle"], tl_email,
            breakdown=s["count_breakdown"], report_number=report_number_today,
        )
        send_to_all(recipients, subject, body, live)
        fired.append(("COUNT", list(s["count_breakdown"])))
        s["count_since_reset"] = 0
        s["count_triggers_sent"] += 1
        s["count_breakdown"] = []

    if s["minutes_since_reset"] >= MINUTES_THRESHOLD:
        if report_number_today is None:
            summary = build_ir_summary_minutes(fname, lname, s["total_minutes_in_cycle"], s["minutes_breakdown"])
            report_number_today = file_incident_report(
                cur, companyid, f"{fname} {lname}", process_date, summary,
                dry_run=not live, log_prefix=LOG,
            )
        subject, body = build_minutes_email(
            fname, lname, companyid, period_start, period_end,
            s["total_minutes_in_cycle"], tl_email,
            breakdown=s["minutes_breakdown"], report_number=report_number_today,
        )
        send_to_all(recipients, subject, body, live)
        fired.append(("MINUTES", list(s["minutes_breakdown"])))
        s["minutes_since_reset"] = 0
        s["minutes_triggers_sent"] += 1
        s["minutes_breakdown"] = []

    return fired


def _log_skips(day_records, process_date):
    not_settled = [r for r in day_records if r["status"] == "NOT_SETTLED"]
    if not_settled:
        print(f"{LOG} {process_date}: {len(not_settled)} shift(s) not settled yet, skipped: "
              + ", ".join(f"{r['lname']}, {r['fname']} ({r['shift_time_raw']})" for r in not_settled))


# ---------------------------------------------------------------------------
# Main processing
# ---------------------------------------------------------------------------

def process_day(process_date: date, live: bool):
    if live and process_date < LIVE_START_DATE:
        print(f"{LOG}[LIVE] {process_date} is before go-live {LIVE_START_DATE}; nothing processed.")
        return

    period_start, period_end = get_payroll_period_for_date(process_date)
    mode = "LIVE" if live else "DRY RUN"

    day_records = get_undertime_for_date(process_date)
    ut_records = [r for r in day_records if r["status"] == "UNDERTIME"]

    print(f"{LOG}[{mode}] Processing {process_date.isoformat()} "
          f"(period {period_start} to {period_end}): {len(ut_records)} undertime record(s)")
    _log_skips(day_records, process_date)

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        for r in ut_records:
            companyid, fname, lname = r["companyid"], r["fname"], r["lname"]
            s = get_or_create_state(cur, companyid, period_start)

            # Idempotency guard. Stricter than tardiness' "== date": a day on
            # or before the last processed one is skipped, so re-running a
            # later date after back-filling an earlier one can't count it twice.
            if s["last_processed_date"] and process_date <= s["last_processed_date"]:
                print(f"{LOG} {lname}, {fname} already processed through "
                      f"{s['last_processed_date']}, skipping {process_date}.")
                continue

            print(f"{LOG}   {lname}, {fname} ({companyid}): {r['minutes_early']} min early "
                  f"(out {r['time_out']:%H:%M} via {r['out_source']}, shift {r['shift_time_raw']})")
            if not live:
                apply_rules(cur, s, r, process_date, period_start, period_end, live)
                continue

            # Commit per employee, so a failure on one employee can't roll
            # back IRs already filed (and emailed) for the ones before it.
            try:
                apply_rules(cur, s, r, process_date, period_start, period_end, live)
                save_state(cur, s)
                conn.commit()
            except Exception as e:
                conn.rollback()
                print(f"{LOG} ERROR processing {lname}, {fname} ({companyid}) for {process_date}, "
                      f"rolled back: {e}")

        if not live:
            conn.rollback()
    finally:
        cur.close()
        conn.close()


def dry_run_range(date_from: date, date_to: date):
    """
    Simulates every day in [date_from, date_to] with in-memory state only:
    never touches undertime_cycle_state, never files an IR, never emails.
    State resets at each payroll-cycle boundary, same as the real table.
    """
    if date_to < date_from:
        date_from, date_to = date_to, date_from

    conn = get_db_connection()
    cur = conn.cursor()
    sim_state = {}
    events = []
    try:
        current = date_from
        while current <= date_to:
            period_start, period_end = get_payroll_period_for_date(current)
            day_records = get_undertime_for_date(current)
            ut_records = [r for r in day_records if r["status"] == "UNDERTIME"]
            if ut_records:
                print(f"{LOG}[DRY RUN] {current.isoformat()}: {len(ut_records)} undertime record(s)")
            _log_skips(day_records, current)

            for r in ut_records:
                key = (r["companyid"], period_start)
                if key not in sim_state:
                    sim_state[key] = dict(_new_state(r["companyid"], period_start),
                                          fname=r["fname"], lname=r["lname"])
                s = sim_state[key]
                for trigger_type, breakdown in apply_rules(
                        cur, s, r, current, period_start, period_end, live=False):
                    events.append({
                        "date": current, "type": trigger_type, "fname": r["fname"],
                        "lname": r["lname"], "companyid": r["companyid"],
                        "total_count": s["total_count_in_cycle"],
                        "total_minutes": s["total_minutes_in_cycle"],
                        "breakdown": breakdown,
                    })
            current += timedelta(days=1)
    finally:
        conn.rollback()
        cur.close()
        conn.close()

    print()
    print(f"=== DRY RUN SUMMARY: {date_from.isoformat()} to {date_to.isoformat()} ===")
    if not events:
        print("No one would have triggered an undertime IR in this range.")
    for ev in events:
        if ev["type"] == "COUNT":
            print(f"  [{ev['date']}] COUNT trigger: {ev['lname']}, {ev['fname']} ({ev['companyid']}) "
                  f"- {ev['total_count']} undertime(s) in cycle so far")
        else:
            trig = sum(e["minutes"] for e in ev["breakdown"])
            print(f"  [{ev['date']}] MINUTES trigger: {ev['lname']}, {ev['fname']} ({ev['companyid']}) "
                  f"- {trig} min - {ev['total_minutes']} total min in cycle so far")
        for e in ev["breakdown"]:
            print(f"      {_entry_line(e)}")

    print()
    print("=== Final per-employee totals (in-memory, NOT saved) ===")
    for (companyid, period_start), s in sim_state.items():
        print(f"  [{period_start}] {s['lname']}, {s['fname']} ({companyid}): "
              f"{s['total_count_in_cycle']} undertime(s), {s['total_minutes_in_cycle']} min total, "
              f"{s['count_triggers_sent']} count-trigger(s), {s['minutes_triggers_sent']} minutes-trigger(s)")


def send_test_email(to_address="andrewvincentt@gmail.com"):
    """Sample COUNT + MINUTES emails with dummy data to to_address only."""
    ps, pe = date(2026, 9, 8), date(2026, 9, 22)
    bd = [
        {"date": date(2026, 9, 10), "minutes": 12, "time_out": "06:48 AM", "shift_end": "07:00 AM", "out_source": "PUNCH"},
        {"date": date(2026, 9, 12), "minutes": 5, "time_out": "06:55 AM", "shift_end": "07:00 AM", "out_source": "FTS"},
        {"date": date(2026, 9, 15), "minutes": 18, "time_out": "06:42 AM", "shift_end": "07:00 AM", "out_source": "PUNCH"},
    ]
    for builder, kwargs in (
        (build_count_email, {"total_count_in_cycle": 3}),
        (build_minutes_email, {"total_minutes_in_cycle": 35}),
    ):
        subject, body = builder("Test", "Employee", "000000-00", ps, pe, tl_email="test.tl@cohere.ph",
                                breakdown=bd, report_number="IR-TEST-000000", **kwargs)
        send_email(to_address, "[TEST] " + subject, body)
    print(f"{LOG} Test emails sent to {to_address} only (no DB lookups, no IR filed).")


if __name__ == "__main__":
    args = sys.argv[1:]

    if args and args[0] == "--test-email":
        send_test_email(args[1] if len(args) > 1 else "andrewvincentt@gmail.com")
    elif args and args[0] == "--dry-run":
        if len(args) != 3:
            print("Usage: python3 undertime_notify.py --dry-run YYYY-MM-DD YYYY-MM-DD")
            sys.exit(1)
        dry_run_range(date.fromisoformat(args[1]), date.fromisoformat(args[2]))
    else:
        live = "--live" in args
        rest = [a for a in args if a != "--live"]
        target = date.fromisoformat(rest[0]) if rest else PROCESS_DATE
        process_day(target, live=live)
