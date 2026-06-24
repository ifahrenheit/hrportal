"""
tardiness_notify.py
Standalone daily cron script — NOT a Flask blueprint, run directly via cron,
separate from the web app process.

Run once a day (after the previous day's attendance data is finalized,
similar timing to the existing 4am absence-alert cron). Recommended:

    30 5 * * * /var/www/html/leavesystem/venv/bin/python /var/www/html/leavesystem/tardiness_notify.py >> /var/www/html/leavesystem/tardiness_notify.log 2>&1

What it does, once per run:
  1. Determines the CURRENT (in-progress) payroll period.
  2. For each employee with a late record YESTERDAY (the most recently
     finalized day) that hasn't been processed yet, increments two running
     counters in tardiness_cycle_state:
       - count_since_reset   (resets to 0 after every 3rd late)
       - minutes_since_reset (resets to 0 after crossing 31+ minutes)
  3. If a counter crosses its threshold, sends a memo email and resets
     that counter (independently of the other counter).
  4. State is keyed by (personid, period_start), so a new payroll period
     starts both counters fresh automatically — no explicit cycle-end
     cleanup needed.

This intentionally processes only ONE day per run (yesterday). If the cron
hasn't run for several days, run it manually multiple times (advancing
PROCESS_DATE) or extend it to loop — kept simple for now since daily cron
is the expected operating mode.
"""

import os
import smtplib
import sys
from datetime import date, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from dotenv import load_dotenv

from db_core import get_db_connection
from payroll_period import get_current_payroll_period
from tardiness import get_tardiness_for_date

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SMTP_SERVER = os.environ.get("SMTP_SERVER")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")

# Distinct sender name for this notification type, reusing the same SMTP
# account/credentials as everything else, just a different display name.
TARDINESS_SENDER_NAME = os.environ.get("TARDINESS_SENDER_NAME", "Tardiness Email Alert")

# Comma-separated static recipient list, e.g.:
#   TARDINESS_NOTIFY_RECIPIENTS=wfm@cohere.ph,som@cohere.ph,jericho.garcia@cohere.ph
STATIC_RECIPIENTS = [
    e.strip() for e in os.environ.get("TARDINESS_NOTIFY_RECIPIENTS", "").split(",") if e.strip()
]

# BCC recipient(s) - included in actual delivery but never shown in the
# "To" header. Configurable via .env, falls back to a hardcoded default.
BCC_RECIPIENTS = [
    e.strip() for e in os.environ.get("TARDINESS_NOTIFY_BCC", "andrewvincentt@gmail.com").split(",") if e.strip()
]

COUNT_THRESHOLD = 3
MINUTES_THRESHOLD = 31

# The day being processed: "yesterday" relative to when this script runs.
PROCESS_DATE = date.today() - timedelta(days=1)


# ---------------------------------------------------------------------------
# Email sending
# ---------------------------------------------------------------------------

def send_email(to_addresses, subject, body_html):
    if not to_addresses:
        print(f"[tardiness_notify] No recipients for '{subject}', skipping send.")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{TARDINESS_SENDER_NAME} <{SMTP_USER}>"
    msg["To"] = ", ".join(to_addresses)
    # Intentionally NOT setting a "Bcc" header -- that would defeat the
    # purpose. BCC recipients are added only to the actual SMTP envelope
    # recipient list below, invisible to everyone in "To".
    msg.attach(MIMEText(body_html, "html"))

    envelope_recipients = list(to_addresses) + [b for b in BCC_RECIPIENTS if b not in to_addresses]

    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(SMTP_USER, envelope_recipients, msg.as_string())

    print(f"[tardiness_notify] Sent '{subject}' to {to_addresses} (bcc: {BCC_RECIPIENTS})")


# ---------------------------------------------------------------------------
# Recipient lookup (static list + the employee's direct Team Lead)
# ---------------------------------------------------------------------------

def get_team_lead_email(cur, companyid):
    """
    Looks up the employee's TL's email via:
        gsheet_employees.tl (first name, e.g. 'Florence')
        -> tl_view_map.tl_name -> tl_view_map.login_email
    Returns None if no match (employee has no TL on file, or TL not yet
    in tl_view_map).
    """
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
    recipients = list(STATIC_RECIPIENTS)
    tl_email = get_team_lead_email(cur, companyid)
    if tl_email and tl_email not in recipients:
        recipients.append(tl_email)
    return recipients, tl_email


# ---------------------------------------------------------------------------
# Email content
# ---------------------------------------------------------------------------

def build_count_email(fname, lname, companyid, period_start, period_end, total_count_in_cycle, tl_email):
    subject = f"Tardiness Memo: {lname}, {fname} - 3 Lates Recorded ({period_start} to {period_end})"
    body = f"""
    <p>This is an automated tardiness memo notice.</p>
    <p><strong>{lname}, {fname}</strong> ({companyid}) has reached <strong>3 late instances</strong>
    within the current payroll cycle ({period_start.strftime('%B %d')} - {period_end.strftime('%B %d, %Y')}).</p>
    <p>Total late instances so far this cycle: <strong>{total_count_in_cycle}</strong></p>
    <p>Team Lead on file: {tl_email or 'Not found in tl_view_map'}</p>
    <hr>
    <p style="color:#888;font-size:12px;">This is an automated message from the Tardiness Report system. This counter resets after every 3rd late instance, so this memo may repeat later in the same cycle if tardiness continues.</p>
    """
    return subject, body


def build_minutes_email(fname, lname, companyid, period_start, period_end, total_minutes_in_cycle, tl_email):
    subject = f"Tardiness Memo: {lname}, {fname} - 31+ Minutes Accumulated ({period_start} to {period_end})"
    body = f"""
    <p>This is an automated tardiness memo notice.</p>
    <p><strong>{lname}, {fname}</strong> ({companyid}) has accumulated <strong>31 or more minutes</strong>
    of total tardiness within the current payroll cycle ({period_start.strftime('%B %d')} - {period_end.strftime('%B %d, %Y')}).</p>
    <p>Total minutes late so far this cycle: <strong>{total_minutes_in_cycle}</strong></p>
    <p>Team Lead on file: {tl_email or 'Not found in tl_view_map'}</p>
    <hr>
    <p style="color:#888;font-size:12px;">This is an automated message from the Tardiness Report system. This counter resets after crossing the 31-minute mark, so this memo may repeat later in the same cycle if tardiness continues.</p>
    """
    return subject, body


# ---------------------------------------------------------------------------
# Main processing
# ---------------------------------------------------------------------------

def get_or_create_state(cur, personid, period_start):
    cur.execute(
        "SELECT * FROM tardiness_cycle_state WHERE personid = %s AND period_start = %s",
        (personid, period_start),
    )
    row = cur.fetchone()
    if row:
        return row
    return {
        "personid": personid,
        "period_start": period_start,
        "count_since_reset": 0,
        "minutes_since_reset": 0,
        "total_count_in_cycle": 0,
        "total_minutes_in_cycle": 0,
        "last_processed_date": None,
        "count_triggers_sent": 0,
        "minutes_triggers_sent": 0,
    }


def save_state(cur, state):
    cur.execute(
        """
        INSERT INTO tardiness_cycle_state
            (personid, period_start, count_since_reset, minutes_since_reset,
             total_count_in_cycle, total_minutes_in_cycle, last_processed_date,
             count_triggers_sent, minutes_triggers_sent)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            count_since_reset=%s, minutes_since_reset=%s,
            total_count_in_cycle=%s, total_minutes_in_cycle=%s,
            last_processed_date=%s, count_triggers_sent=%s, minutes_triggers_sent=%s
        """,
        (
            state["personid"], state["period_start"],
            state["count_since_reset"], state["minutes_since_reset"],
            state["total_count_in_cycle"], state["total_minutes_in_cycle"],
            state["last_processed_date"], state["count_triggers_sent"], state["minutes_triggers_sent"],

            state["count_since_reset"], state["minutes_since_reset"],
            state["total_count_in_cycle"], state["total_minutes_in_cycle"],
            state["last_processed_date"], state["count_triggers_sent"], state["minutes_triggers_sent"],
        ),
    )


def process_day(process_date: date):
    period_start, period_end = get_current_payroll_period(process_date)

    # If process_date falls before period_start, it belongs to the PREVIOUS
    # cycle (e.g. cron ran on the 1st of the month for the 31st's data,
    # which is still within the prior 23rd-7th period). Recompute using
    # the period containing process_date itself, not "today".
    if process_date < period_start:
        period_start, period_end = get_current_payroll_period(process_date)

    conn = get_db_connection()
    cur = conn.cursor()

    day_records = get_tardiness_for_date(process_date)
    late_records = [r for r in day_records if r["status"] == "LATE"]

    print(f"[tardiness_notify] Processing {process_date.isoformat()} "
          f"(period {period_start} to {period_end}): {len(late_records)} late record(s)")

    for r in late_records:
        personid = r["personid"]
        companyid = r["companyid"]
        fname, lname = r["fname"], r["lname"]
        minutes_late = r["minutes_late"] or 0

        state = get_or_create_state(cur, personid, period_start)

        # Idempotency guard: don't double-process the same day if the cron
        # somehow runs twice (manual re-run, systemd retry, etc.)
        if state["last_processed_date"] == process_date:
            print(f"[tardiness_notify] {lname}, {fname} already processed for {process_date}, skipping.")
            continue

        state["count_since_reset"] += 1
        state["minutes_since_reset"] += minutes_late
        state["total_count_in_cycle"] += 1
        state["total_minutes_in_cycle"] += minutes_late
        state["last_processed_date"] = process_date

        recipients, tl_email = build_recipient_list(cur, companyid)

        if state["count_since_reset"] >= COUNT_THRESHOLD:
            subject, body = build_count_email(
                fname, lname, companyid, period_start, period_end,
                state["total_count_in_cycle"], tl_email,
            )
            send_email(recipients, subject, body)
            state["count_since_reset"] = 0
            state["count_triggers_sent"] += 1

        if state["minutes_since_reset"] >= MINUTES_THRESHOLD:
            subject, body = build_minutes_email(
                fname, lname, companyid, period_start, period_end,
                state["total_minutes_in_cycle"], tl_email,
            )
            send_email(recipients, subject, body)
            state["minutes_since_reset"] = 0
            state["minutes_triggers_sent"] += 1

        save_state(cur, state)

    conn.commit()
    cur.close()
    conn.close()


def dry_run_range(date_from: date, date_to: date):
    """
    Simulates processing every day in [date_from, date_to] WITHOUT touching
    tardiness_cycle_state and WITHOUT sending any real email. State is kept
    purely in-memory for this run, then discarded. Use this to check who
    would have triggered in an already-completed payroll cycle, e.g.:

        python3 tardiness_notify.py --dry-run 2026-05-23 2026-06-07

    Still opens a real DB connection (read-only: get_tardiness_for_date,
    and the TL lookup in build_recipient_list), but never writes to
    tardiness_cycle_state and never calls smtplib.
    """
    if date_to < date_from:
        date_from, date_to = date_to, date_from

    conn = get_db_connection()
    cur = conn.cursor()

    # personid -> in-memory state dict, separate from the real DB table
    sim_state = {}
    triggered_events = []

    current = date_from
    while current <= date_to:
        day_records = get_tardiness_for_date(current)
        late_records = [r for r in day_records if r["status"] == "LATE"]

        if late_records:
            print(f"[DRY RUN] {current.isoformat()}: {len(late_records)} late record(s)")

        for r in late_records:
            personid = r["personid"]
            companyid = r["companyid"]
            fname, lname = r["fname"], r["lname"]
            minutes_late = r["minutes_late"] or 0

            if personid not in sim_state:
                sim_state[personid] = {
                    "fname": fname, "lname": lname, "companyid": companyid,
                    "count_since_reset": 0, "minutes_since_reset": 0,
                    "total_count_in_cycle": 0, "total_minutes_in_cycle": 0,
                    "count_triggers_sent": 0, "minutes_triggers_sent": 0,
                }
            s = sim_state[personid]
            s["count_since_reset"] += 1
            s["minutes_since_reset"] += minutes_late
            s["total_count_in_cycle"] += 1
            s["total_minutes_in_cycle"] += minutes_late

            if s["count_since_reset"] >= COUNT_THRESHOLD:
                triggered_events.append({
                    "date": current, "type": "COUNT", "fname": fname, "lname": lname,
                    "companyid": companyid, "total_count_in_cycle": s["total_count_in_cycle"],
                })
                s["count_since_reset"] = 0
                s["count_triggers_sent"] += 1

            if s["minutes_since_reset"] >= MINUTES_THRESHOLD:
                triggered_events.append({
                    "date": current, "type": "MINUTES", "fname": fname, "lname": lname,
                    "companyid": companyid, "total_minutes_in_cycle": s["total_minutes_in_cycle"],
                })
                s["minutes_since_reset"] = 0
                s["minutes_triggers_sent"] += 1

        current += timedelta(days=1)

    print()
    print(f"=== DRY RUN SUMMARY: {date_from.isoformat()} to {date_to.isoformat()} ===")
    if not triggered_events:
        print("No one would have triggered a notification in this range.")
    else:
        for ev in triggered_events:
            if ev["type"] == "COUNT":
                print(f"  [{ev['date']}] COUNT trigger: {ev['lname']}, {ev['fname']} ({ev['companyid']}) "
                      f"- {ev['total_count_in_cycle']} total lates in cycle so far")
            else:
                print(f"  [{ev['date']}] MINUTES trigger: {ev['lname']}, {ev['fname']} ({ev['companyid']}) "
                      f"- {ev['total_minutes_in_cycle']} total minutes in cycle so far")

    print()
    print("=== Final per-employee totals (in-memory, NOT saved) ===")
    for personid, s in sim_state.items():
        print(f"  {s['lname']}, {s['fname']} ({s['companyid']}): "
              f"{s['total_count_in_cycle']} late(s), {s['total_minutes_in_cycle']} min total, "
              f"{s['count_triggers_sent']} count-trigger(s), {s['minutes_triggers_sent']} minutes-trigger(s)")

    cur.close()
    conn.close()


if __name__ == "__main__":
    args = sys.argv[1:]

    if args and args[0] == "--dry-run":
        # python3 tardiness_notify.py --dry-run 2026-05-23 2026-06-07
        if len(args) != 3:
            print("Usage: python3 tardiness_notify.py --dry-run YYYY-MM-DD YYYY-MM-DD")
            sys.exit(1)
        date_from = date.fromisoformat(args[1])
        date_to = date.fromisoformat(args[2])
        dry_run_range(date_from, date_to)
    else:
        target = PROCESS_DATE
        if args:
            # Allow manual override: python3 tardiness_notify.py 2026-06-23
            target = date.fromisoformat(args[0])
        process_day(target)