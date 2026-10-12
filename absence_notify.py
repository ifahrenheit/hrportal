"""
absence_notify.py
Daily absence alert emails, built from the same data as /admin/absences
(get_absence_records() in app.py) instead of the Google Sheet that
/var/www/html/automation/send_absence_report.php reads.

DRY-RUN BY DEFAULT. Nothing is emailed without --live.

    # cron (processes yesterday):
    0 13 * * * /var/www/html/leavesystem/venv/bin/python /var/www/html/leavesystem/absence_notify.py --live >> /var/www/html/leavesystem/absence_notify.log 2>&1

    python3 absence_notify.py                   # dry run, yesterday
    python3 absence_notify.py 2026-10-05        # dry run, that day
    python3 absence_notify.py --live [date]     # real run

One email per employee who, on the processed date:
  - was scheduled (not a rest day) and Active in gsheet_employees,
  - has no punches for the shift at all ('Absent'; FTS IN / FTS OUT are
    failure-to-scan cases and are never emailed here),
  - has no leave filed for that date in any non-rejected status (pending,
    scheduled and approved all suppress the alert),
  - has no non-rejected CWS moving that date,
  - isn't on suspension that date (central_db.absence_records code 'SUS'),
  - isn't covered by an approved OT request (handled by get_absence_records).

The email lists every 'Absent' day in the 7-day window ending on the
processed date (minus suspension days); 2+ makes it a REPEATED ABSENCE ALERT.
To: TL (tl_view_map) + SOM (gsheet_employees.approver) + OM; BCC: RTA + admin.
If the run itself fails, ABSENCE_FAILURE_RECIPIENTS get a "FAILED to run" email.
"""

import html
import os
import smtplib
import sys
import traceback
from datetime import date, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from dotenv import load_dotenv

load_dotenv('/var/www/html/leavesystem/.env')

LOG = "[absence_notify]"

SMTP_SERVER = os.environ.get("SMTP_SERVER")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
SENDER_NAME = os.environ.get("ABSENCE_SENDER_NAME", "Absence Notifier")


def _env_list(key, default):
    return [e.strip() for e in os.environ.get(key, default).split(",") if e.strip()]


# Same fixed recipients as send_absence_report.php.
OM_RECIPIENTS = _env_list("ABSENCE_NOTIFY_OM", "jericho.garcia@cohere.ph")
BCC_RECIPIENTS = _env_list("ABSENCE_NOTIFY_BCC", "jovin.lumapat@cohere.ph,andrew.tacdoro@cohere.ph")
FAILURE_RECIPIENTS = _env_list("ABSENCE_FAILURE_RECIPIENTS", "andrew.tacdoro@cohere.ph,jovin.lumapat@cohere.ph")

WINDOW_DAYS = 7
# Same cutoff as /admin/absences: before 1 PM, yesterday's overnight shifts
# may not have finished yet.
TODAY_CUTOFF_HOUR = 13


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {LOG} {msg}", flush=True)


def safe_last_day():
    if datetime.now().hour < TODAY_CUTOFF_HOUR:
        return date.today() - timedelta(days=2)
    return date.today() - timedelta(days=1)


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def send_email(to_addrs, bcc_addrs, subject, body_html):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{SENDER_NAME} <{SMTP_USER}>"
    msg["To"] = ", ".join(to_addrs)
    msg.attach(MIMEText(body_html, "html"))

    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=30) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(SMTP_USER, to_addrs + bcc_addrs, msg.as_string())


def notify_failure(process_date, err_text, live):
    subject = f"Absence report FAILED to run — {date.today():%B %-d, %Y}"
    body = (
        f"<p>absence_notify.py failed while processing <b>{process_date}</b>, so that day's "
        f"absence alert emails were <b>not sent</b>.</p>"
        f"<pre>{html.escape(err_text)}</pre>"
        f"<p>Check /var/www/html/leavesystem/absence_notify.log, then re-run:<br>"
        f"<code>venv/bin/python absence_notify.py --live {process_date}</code></p>"
    )
    if not live:
        log(f"[DRY RUN] Would send failure alert to {', '.join(FAILURE_RECIPIENTS)}")
        return
    try:
        send_email(FAILURE_RECIPIENTS, [], subject, body)
    except Exception as e:
        log(f"Failure-alert email itself failed to send: {e}")


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------

def load_suspension_dates(cur, emp_ids, start, end):
    """{employee_id: {date, ...}} from central_db.absence_records code 'SUS'."""
    if not emp_ids:
        return {}
    ph = ",".join(["%s"] * len(emp_ids))
    cur.execute(
        f"""
        SELECT employee_id, absence_date
        FROM absence_records
        WHERE code = 'SUS'
          AND employee_id IN ({ph})
          AND absence_date BETWEEN %s AND %s
        """,
        list(emp_ids) + [start, end],
    )
    out = {}
    for row in cur.fetchall():
        d = row["absence_date"]
        if isinstance(d, datetime):
            d = d.date()
        out.setdefault(row["employee_id"], set()).add(d)
    return out


def get_recipients(cur, employee_id):
    """Returns (to_addrs, tl_email, som_email)."""
    cur.execute(
        """
        SELECT ge.approver AS som_email, m.login_email AS tl_email
        FROM gsheet_employees ge
        LEFT JOIN tl_view_map m ON m.tl_name = ge.tl
        WHERE ge.employee_id = %s
        LIMIT 1
        """,
        (employee_id,),
    )
    row = cur.fetchone() or {}
    tl_email = (row.get("tl_email") or "").strip() or None
    som_email = (row.get("som_email") or "").strip() or None

    seen, to_addrs = set(), []
    for addr in [tl_email, som_email] + OM_RECIPIENTS:
        if addr and "@" in addr and addr.lower() not in seen:
            seen.add(addr.lower())
            to_addrs.append(addr)
    return to_addrs, tl_email, som_email


# ---------------------------------------------------------------------------
# Email body
# ---------------------------------------------------------------------------

def leave_label(leaves):
    if not leaves:
        return "No Filed Leave"
    return ", ".join(f"{l['status'].title()} ({l['leave_type_name']})" for l in leaves)


def build_email(rec, window_days, window_start, process_date):
    name = html.escape(rec["schedule_name"] or "")
    emp_id = html.escape(rec["employee_id"])
    pretty = f"{process_date:%B %-d, %Y}"
    total = len(window_days)

    items = "".join(
        f"<li>{d:%B %-d, %Y (%a)} — {html.escape(label)}</li>" for d, label in window_days
    )
    repeated = total >= 2
    subject = (f"REPEATED ABSENCE ALERT: {rec['schedule_name']} ({pretty})" if repeated
               else f"Absence Alert: {rec['schedule_name']} ({pretty})")

    body = f"""
    <p>Hi Team,</p>

    <p>Please be informed that <strong>{name}</strong> (ID: <strong>{emp_id}</strong>) was absent on <strong>{pretty}</strong>.</p>

    <p>Scheduled Shift: <strong>{html.escape(rec['shift_time'] or '')}</strong><br>
    Total Absences: <strong>{total}</strong> (from <strong>{window_start:%B %-d, %Y}</strong> to <strong>{pretty}</strong>)<br>
    Leave Type: <strong>No Filed Leave in HRIS</strong><br>
    Status: <strong>No Filed Leave in HRIS</strong></p>

    <p><strong>Absence Date(s) in this window:</strong></p>
    <ul style="margin-top:4px;">{items}</ul>
    {'<p style="color: red;">This is a repeated absence. Please review and take necessary action.</p>' if repeated else ''}
    <br>
    <p>Regards,<br>
    Attendance Monitoring System</p>"""
    return subject, body


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(process_date, live):
    # Imported here so a broken app import is caught and reported by main().
    from app import get_absence_records
    from db_core import get_db_connection

    window_start = process_date - timedelta(days=WINDOW_DAYS - 1)
    records, leave_map, cws_map = get_absence_records(window_start, process_date)

    absent = [r for r in records if r["attendance_status"] == "Absent"]
    day_absent = [r for r in absent if r["absent_date"] == process_date]
    fts_count = sum(1 for r in records
                    if r["absent_date"] == process_date and r["attendance_status"] != "Absent")

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            sus = load_suspension_dates(cur, {r["employee_id"] for r in absent},
                                        window_start, process_date)

            emailed = skipped_leave = skipped_cws = skipped_sus = failed = 0
            for rec in day_absent:
                emp_id = rec["employee_id"]
                key = f"{emp_id}|{process_date}"
                who = f"{rec['schedule_name']} ({emp_id})"

                if process_date in sus.get(emp_id, set()):
                    skipped_sus += 1
                    log(f"Skip {who}: suspension on {process_date}")
                    continue
                if leave_map.get(key):
                    skipped_leave += 1
                    log(f"Skip {who}: leave filed ({leave_label(leave_map[key])})")
                    continue
                if cws_map.get(key):
                    skipped_cws += 1
                    log(f"Skip {who}: CWS filed ({', '.join(c['status'] for c in cws_map[key])})")
                    continue

                window_days = sorted(
                    (r["absent_date"], leave_label(leave_map.get(f"{emp_id}|{r['absent_date']}")))
                    for r in absent
                    if r["employee_id"] == emp_id
                    and r["absent_date"] not in sus.get(emp_id, set())
                )
                subject, body = build_email(rec, window_days, window_start, process_date)
                to_addrs, tl_email, som_email = get_recipients(cur, emp_id)
                if not tl_email:
                    log(f"Note {who}: no TL email found in tl_view_map")
                if not som_email:
                    log(f"Note {who}: no SOM (approver) email in gsheet_employees")

                if not live:
                    log(f"[DRY RUN] Would email '{subject}' To={to_addrs} BCC={BCC_RECIPIENTS}")
                    emailed += 1
                    continue
                try:
                    send_email(to_addrs, BCC_RECIPIENTS, subject, body)
                    emailed += 1
                    log(f"Sent '{subject}' To={to_addrs}")
                except Exception as e:
                    failed += 1
                    log(f"Email error for {who}: {e}")
    finally:
        conn.close()

    log(
        f"{process_date}: {len(day_absent)} absent, {fts_count} FTS (not emailed), "
        f"{emailed} {'emailed' if live else 'would be emailed'}, "
        f"skipped {skipped_leave} leave / {skipped_cws} CWS / {skipped_sus} SUS"
        + (f", {failed} send failures" if failed else "")
        + ("" if live else " [DRY RUN]")
    )


def main(argv):
    live = "--live" in argv
    args = [a for a in argv if not a.startswith("--")]
    try:
        process_date = date.fromisoformat(args[0]) if args else date.today() - timedelta(days=1)
    except ValueError:
        log(f"Bad date '{args[0]}', expected YYYY-MM-DD")
        return 2

    last_ok = safe_last_day()
    if process_date > last_ok:
        log(f"{process_date} isn't complete yet (latest safe day is {last_ok}); nothing done.")
        return 1

    try:
        run(process_date, live)
    except Exception:
        err = traceback.format_exc()
        log(f"FATAL: {err}")
        notify_failure(process_date, err, live)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
