"""
overbreak_notify.py
Standalone cron script — NOT a Flask blueprint, run directly via cron,
separate from the web app process.

Recommended schedule (staggered after the sheet sync, which runs every
6 hours via Apps Script trigger): run every 6 hours, offset ~15 min after.
    15 */6 * * * /var/www/html/leavesystem/venv/bin/python /var/www/html/leavesystem/overbreak_notify.py >> /var/www/html/leavesystem/overbreak_notify.log 2>&1

What it does, once per run:
  1. Pulls all overbreak_records rows where validity='Valid' AND
     notify_processed=0, ordered by record_date/agent_name.
  2. For each row, resolves its payroll period_key (payroll_month +
     payroll_cycle + record_year -- year is included because payroll
     labels like "December-1st" repeat every year; without it, different
     years' Decembers would share the same running counters).
  3. Updates per-employee-per-period counters in overbreak_cycle_state:
       Rule 1: count_since_reset (resets to 0 after every 3rd valid
               instance; re-arms, can fire multiple times per period)
       Rule 2: pair_since_reset (resets after every 2nd instance,
               regardless of whether it fired; fires only if that pair's
               combined duration exceeds 3:30)
       Rule 3: evaluated per-instance immediately, no counter needed;
               fires if a single instance's break_duration exceeds 2:00
  4. On every trigger (all 3 rules): auto-files a real Incident Report
     into incident_reports (status='pending', filed by HR,
     submitted_by_name='System'), then sends a memo email per triggered
     rule (same TL-to/static-cc/bcc pattern as tardiness_notify.py) that
     references the filed IR number/link.
  5. Marks each processed row's notify_processed=1 so re-runs only pick
     up newly-synced rows.
"""
import os
import sys
import json
import uuid
import smtplib
from datetime import timedelta, datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv
from db_core import get_db_connection

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SMTP_SERVER = os.environ.get("SMTP_SERVER")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")

OVERBREAK_SENDER_NAME = os.environ.get("OVERBREAK_SENDER_NAME", "Overbreak Email Alert")

STATIC_RECIPIENTS = [
    e.strip() for e in os.environ.get("OVERBREAK_NOTIFY_RECIPIENTS", "").split(",") if e.strip()
]
BCC_RECIPIENTS = [
    e.strip() for e in os.environ.get("OVERBREAK_NOTIFY_BCC", "andrewvincentt@gmail.com").split(",") if e.strip()
]

IR_REPORT_BASE_URL = "https://hrportal.cohere.ph/incident-reports"

ALLOWED_MINUTES_PER_INSTANCE = 90  # 1:30
RULE1_COUNT_THRESHOLD = 3
RULE2_PAIR_TOTAL_THRESHOLD_MIN = 210   # 3.5 hrs
RULE2_ALLOWED_FOR_PAIR_MIN = 180       # 3.0 hrs (2 x 1:30)
RULE3_SINGLE_THRESHOLD_MIN = 120       # 2.0 hrs
RULE3_ALLOWED_MIN = 90                 # 1:30

DRY_RUN = "--dry-run" in sys.argv


# ---------------------------------------------------------------------------
# Email sending (mirrors tardiness_notify.py exactly)
# ---------------------------------------------------------------------------

def send_email(to_address, subject, body_html, cc_addresses=None):
    cc_addresses = cc_addresses or []

    if DRY_RUN:
        print(f"[overbreak_notify][DRY RUN] Would send '{subject}' -> To: {to_address}, Cc: {cc_addresses}")
        return

    if to_address:
        to_list = [to_address]
        cc_list = [c for c in cc_addresses if c != to_address]
    else:
        if not cc_addresses:
            print(f"[overbreak_notify] No recipients at all for '{subject}', skipping send.")
            return
        to_list = cc_addresses
        cc_list = []

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{OVERBREAK_SENDER_NAME} <{SMTP_USER}>"
    msg["To"] = ", ".join(to_list)
    if cc_list:
        msg["Cc"] = ", ".join(cc_list)
    msg.attach(MIMEText(body_html, "html"))

    envelope_recipients = list(to_list) + [c for c in cc_list if c not in to_list] \
        + [b for b in BCC_RECIPIENTS if b not in to_list and b not in cc_list]

    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(SMTP_USER, envelope_recipients, msg.as_string())

    print(f"[overbreak_notify] Sent '{subject}' - To: {to_list}, Cc: {cc_list}, Bcc: {BCC_RECIPIENTS}")


# ---------------------------------------------------------------------------
# Recipient lookup (mirrors tardiness_notify.py: TL via gsheet_employees.tl
# -> tl_view_map.tl_name -> login_email)
# ---------------------------------------------------------------------------

def get_team_lead_email(cur, employee_id):
    cur.execute(
        """
        SELECT m.login_email
        FROM gsheet_employees ge
        JOIN tl_view_map m ON m.tl_name = ge.tl
        WHERE ge.employee_id = %s
        LIMIT 1
        """,
        (employee_id,),
    )
    row = cur.fetchone()
    return row["login_email"] if row else None


def build_recipient_list(cur, employee_id):
    tl_email = get_team_lead_email(cur, employee_id)
    return tl_email, list(STATIC_RECIPIENTS), tl_email


# ---------------------------------------------------------------------------
# Duration helpers
# ---------------------------------------------------------------------------

def duration_to_minutes(val):
    """Accepts a timedelta (from pymysql TIME column) or 'HH:MM:SS' string."""
    if val is None:
        return 0
    if isinstance(val, timedelta):
        return int(val.total_seconds() // 60)
    if isinstance(val, str) and val:
        parts = val.split(":")
        if len(parts) >= 2:
            h, m = int(parts[0]), int(parts[1])
            return h * 60 + m
    return 0


def minutes_to_hm(total_minutes):
    h, m = divmod(int(total_minutes), 60)
    if h and m:
        return f"{h}h {m}m"
    if h:
        return f"{h}h"
    return f"{m}m"


def format_date_us(d):
    """Formats a date object or ISO string as M/D/YYYY, matching the
    incident-report summary template style (e.g. 6/30/2026)."""
    if hasattr(d, "month"):
        return f"{d.month}/{d.day}/{d.year}"
    try:
        parsed = datetime.strptime(str(d), "%Y-%m-%d").date()
        return f"{parsed.month}/{parsed.day}/{parsed.year}"
    except Exception:
        return str(d)


# ---------------------------------------------------------------------------
# Incident Report auto-filing
# ---------------------------------------------------------------------------

def generate_ir_report_number():
    return "IR-" + datetime.now().strftime("%Y%m%d") + "-" + str(uuid.uuid4())[:6].upper()


def get_agent_full_name(cur, employee_id):
    cur.execute("SELECT schedule_name FROM gsheet_employees WHERE employee_id = %s LIMIT 1", (employee_id,))
    row = cur.fetchone()
    return row["schedule_name"] if row and row.get("schedule_name") else None


RULE_OPENING_TEXT = {
    "RULE1": "It was observed that the agent exceeded the allowable number of break deviations "
             "within one payroll cycle, which is not in compliance with established company "
             "break and time-management policies.",
    "RULE2": "It was observed that the agent had 2 overbreak instances totaling more than 3.5 "
             "hours within one payroll cycle, exceeding the allowable break time, which is not "
             "in compliance with established company break and time-management policies.",
    "RULE3": "It was observed that the agent had a single break instance exceeding 2 hours "
             "within one payroll cycle, exceeding the allowable break time, which is not in "
             "compliance with established company break and time-management policies.",
}

IR_MIDDLE_TEXT = (
    "Overbreak or excessive breaks is a result of taking too many breaks or beyond "
    "the allowable time. Deviation from the break schedule set by WFM or "
    "Supervisors/Team Leads/Managers is also unacceptable. An employee is "
    "considered in violation if there have been 3 instances of any of these "
    "individually or combined within a pay period."
)

IR_CLOSING_TEXT = (
    "Adherence to assigned break schedules is essential to ensure operational efficiency "
    "and consistently meet service level commitments.\n\n"
    "The incident has been documented accordingly and will be addressed in line with "
    "standard disciplinary and corrective procedures."
)


def build_ir_summary(rule_type, agent_name, payroll_month, payroll_cycle, table_rows):
    opening = RULE_OPENING_TEXT.get(rule_type, RULE_OPENING_TEXT["RULE1"])

    table_lines = ["Date Agent Name Daily Lunch & Break Duration Payroll Month Payroll Cycle"]
    for row in table_rows:
        table_lines.append(
            f"{row['date_display']} {agent_name} {row['duration_str']} Valid {payroll_month} {payroll_cycle}"
        )
    table_text = "\n".join(table_lines)

    return f"{opening}\n\n{IR_MIDDLE_TEXT}\n\n{table_text}\n\n\n{IR_CLOSING_TEXT}"


def file_incident_report(cur, employee_id, agent_name, incident_date, summary):
    """
    Creates a real incident_reports record, mirroring incident_reports.py's
    submit_report() escalate_to_hr branch, but system-filed
    (submitted_by_id='SYSTEM', submitted_by_name='System') since there's no
    logged-in user in this cron context. Status starts at 'rwe_request'
    (escalated straight to HR).
    """
    employee_name = get_agent_full_name(cur, employee_id) or agent_name
    report_number = generate_ir_report_number()

    if DRY_RUN:
        print(f"[overbreak_notify][DRY RUN] Would file IR '{report_number}' for {employee_name} ({employee_id})")
        return report_number

    cur.execute(
        """INSERT INTO incident_reports
            (report_number, incident_date, employee_id, employee_name,
             submitted_by_id, submitted_by_name, summary, status)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
        (report_number, incident_date, employee_id, employee_name,
         "SYSTEM", "System", summary, "pending"),
    )
    print(f"[overbreak_notify] Filed IR '{report_number}' for {employee_name} ({employee_id})")
    # Send IR notification email (same as manual filing)
    try:
        from ir_autofile import _send_ir_notification
        _send_ir_notification(cur, report_number, incident_date, employee_id,
                              employee_name, summary, "[overbreak_notify]")
    except Exception as e:
        print(f"[overbreak_notify] IR email failed: {e}")
    return report_number


# ---------------------------------------------------------------------------
# Email content
# ---------------------------------------------------------------------------

def _format_breakdown_html(breakdown):
    if not breakdown:
        return "<p style='color:#888;'>No breakdown available.</p>"
    items = ""
    for entry in breakdown:
        items += f"<li>{entry['date']} - <strong>{minutes_to_hm(entry['minutes'])}</strong></li>"
    return f"<ul style='margin:8px 0;padding-left:20px;'>{items}</ul>"


def _ir_notice_html(report_number):
    url = f"{IR_REPORT_BASE_URL}/{report_number}"
    return f"""
    <p style="background:#fff3cd;padding:10px 14px;border-radius:6px;border:1px solid #ffe69c;">
      <strong>⚠️ An Incident Report has been automatically filed and escalated to HR:</strong><br>
      <a href="{url}">{report_number}</a>
    </p>
    """


def build_rule1_email(agent_name, employee_id, period_key, total_count, tl_email, breakdown, report_number):
    subject = f"Overbreak Memo: {agent_name} - {total_count} Overbreaks Recorded ({period_key})"
    body = f"""
    <p>This is an automated overbreak memo notice.</p>
    <p>The agent has <strong>{total_count}</strong> overbreaks recorded within the current payroll cycle ({period_key}).</p>
    <p><strong>{agent_name}</strong> ({employee_id})</p>
    {_ir_notice_html(report_number)}
    <p>Breakdown of the instances triggering this memo:</p>
    {_format_breakdown_html(breakdown)}
    <p>Team Lead on file: {tl_email or 'Not found in tl_view_map'}</p>
    <hr>
    <p style="color:#888;font-size:12px;">This is an automated message from the Overbreak Report system. This counter resets after every 3rd instance, so this memo may repeat later in the same cycle if overbreaks continue.</p>
    """
    return subject, body


def build_rule2_email(agent_name, employee_id, period_key, pair_total_minutes, tl_email, breakdown, report_number):
    excess = pair_total_minutes - RULE2_ALLOWED_FOR_PAIR_MIN
    subject = f"Overbreak Memo: {agent_name} - 2 Instances Exceeding 3.5 Hrs ({period_key})"
    body = f"""
    <p>This is an automated overbreak memo notice.</p>
    <p><strong>{agent_name}</strong> ({employee_id}) has 2 overbreak instances totaling
    <strong>{minutes_to_hm(pair_total_minutes)}</strong> within the current payroll cycle ({period_key}),
    exceeding the 3.5-hour trigger.</p>
    <p>Exceeded the 3-hour allowed threshold (2 x 1:30) by <strong>{minutes_to_hm(excess)}</strong>.</p>
    {_ir_notice_html(report_number)}
    <p>Breakdown of the 2 instances:</p>
    {_format_breakdown_html(breakdown)}
    <p>Team Lead on file: {tl_email or 'Not found in tl_view_map'}</p>
    <hr>
    <p style="color:#888;font-size:12px;">This is an automated message from the Overbreak Report system.</p>
    """
    return subject, body


def build_rule3_email(agent_name, employee_id, period_key, single_minutes, record_date, tl_email, report_number):
    excess = single_minutes - RULE3_ALLOWED_MIN
    subject = f"Overbreak Memo: {agent_name} - Single Instance Exceeding 2 Hrs ({period_key})"
    body = f"""
    <p>This is an automated overbreak memo notice.</p>
    <p><strong>{agent_name}</strong> ({employee_id}) had a single overbreak instance on
    <strong>{record_date}</strong> lasting <strong>{minutes_to_hm(single_minutes)}</strong>,
    crossing the 2-hour trigger.</p>
    <p>Exceeded the 1.5-hour allowed threshold by <strong>{minutes_to_hm(excess)}</strong>.</p>
    {_ir_notice_html(report_number)}
    <p>Team Lead on file: {tl_email or 'Not found in tl_view_map'}</p>
    <hr>
    <p style="color:#888;font-size:12px;">This is an automated message from the Overbreak Report system.</p>
    """
    return subject, body


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def get_or_create_state(cur, employee_id, period_key):
    cur.execute(
        "SELECT * FROM overbreak_cycle_state WHERE employee_id = %s AND period_key = %s",
        (employee_id, period_key),
    )
    row = cur.fetchone()
    if row:
        row["rule1_breakdown"] = _deserialize(row.get("rule1_breakdown"))
        row["rule2_breakdown"] = _deserialize(row.get("rule2_breakdown"))
        return row
    return {
        "employee_id": employee_id, "period_key": period_key,
        "rule1_count_since_reset": 0, "rule1_total_in_period": 0, "rule1_triggers_sent": 0,
        "rule1_breakdown": [],
        "rule2_pair_since_reset": 0, "rule2_triggers_sent": 0, "rule2_breakdown": [],
        "rule3_triggers_sent": 0,
        "last_processed_row_uid": None,
    }


def _serialize(breakdown):
    return json.dumps(breakdown)


def _deserialize(raw):
    if not raw:
        return []
    try:
        return json.loads(raw)
    except Exception:
        return []


def save_state(cur, state):
    cur.execute(
        """
        INSERT INTO overbreak_cycle_state
            (employee_id, period_key, rule1_count_since_reset, rule1_total_in_period,
             rule1_triggers_sent, rule1_breakdown, rule2_pair_since_reset,
             rule2_triggers_sent, rule2_breakdown, rule3_triggers_sent, last_processed_row_uid)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
            rule1_count_since_reset=%s, rule1_total_in_period=%s, rule1_triggers_sent=%s,
            rule1_breakdown=%s, rule2_pair_since_reset=%s, rule2_triggers_sent=%s,
            rule2_breakdown=%s, rule3_triggers_sent=%s, last_processed_row_uid=%s
        """,
        (
            state["employee_id"], state["period_key"],
            state["rule1_count_since_reset"], state["rule1_total_in_period"], state["rule1_triggers_sent"],
            _serialize(state["rule1_breakdown"]), state["rule2_pair_since_reset"],
            state["rule2_triggers_sent"], _serialize(state["rule2_breakdown"]),
            state["rule3_triggers_sent"], state["last_processed_row_uid"],

            state["rule1_count_since_reset"], state["rule1_total_in_period"], state["rule1_triggers_sent"],
            _serialize(state["rule1_breakdown"]), state["rule2_pair_since_reset"],
            state["rule2_triggers_sent"], _serialize(state["rule2_breakdown"]),
            state["rule3_triggers_sent"], state["last_processed_row_uid"],
        ),
    )


# ---------------------------------------------------------------------------
# Main processing
# ---------------------------------------------------------------------------

def process_pending():
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT id, row_uid, record_date, agent_name, employee_id, break_duration,
               payroll_month, payroll_cycle, record_year
        FROM overbreak_records
        WHERE validity = 'Valid' AND notify_processed = 0 AND employee_id IS NOT NULL AND employee_id != ''
        ORDER BY record_date ASC, id ASC
        """
    )
    rows = cur.fetchall()
    print(f"[overbreak_notify] {len(rows)} unprocessed Valid row(s) found.")

    for r in rows:
        employee_id = r["employee_id"]
        agent_name = r["agent_name"]
        payroll_month = r["payroll_month"]
        payroll_cycle = r["payroll_cycle"]
        record_year = r["record_year"]
        period_key = (
            f"{payroll_month} {record_year}-{payroll_cycle}"
            if payroll_month and payroll_cycle and record_year else "Unknown"
        )
        minutes = duration_to_minutes(r["break_duration"])
        record_date_str = str(r["record_date"])
        duration_str = str(r["break_duration"]) if r["break_duration"] is not None else "0:00:00"
        date_display = format_date_us(r["record_date"])

        state = get_or_create_state(cur, employee_id, period_key)
        to_address, cc_addresses, tl_email = build_recipient_list(cur, employee_id)

        # -- Rule 1: 3+ instances, reset & re-arm --
        entry1 = {"date": record_date_str, "date_display": date_display, "minutes": minutes, "duration_str": duration_str}
        state["rule1_count_since_reset"] += 1
        state["rule1_total_in_period"] += 1
        state["rule1_breakdown"].append(entry1)

        if state["rule1_count_since_reset"] >= RULE1_COUNT_THRESHOLD:
            summary = build_ir_summary("RULE1", agent_name, payroll_month, payroll_cycle, state["rule1_breakdown"])
            report_number = file_incident_report(cur, employee_id, agent_name, r["record_date"], summary)

            subject, body = build_rule1_email(
                agent_name, employee_id, period_key,
                state["rule1_total_in_period"], tl_email, state["rule1_breakdown"], report_number,
            )
            send_email(to_address, subject, body, cc_addresses=cc_addresses)
            state["rule1_count_since_reset"] = 0
            state["rule1_triggers_sent"] += 1
            state["rule1_breakdown"] = []

        # -- Rule 2: pairs, fire if combined > 3.5hrs, reset every 2 regardless --
        entry2 = {"date": record_date_str, "date_display": date_display, "minutes": minutes, "duration_str": duration_str}
        state["rule2_pair_since_reset"] += 1
        state["rule2_breakdown"].append(entry2)

        if state["rule2_pair_since_reset"] >= 2:
            pair_total = sum(e["minutes"] for e in state["rule2_breakdown"])
            if pair_total > RULE2_PAIR_TOTAL_THRESHOLD_MIN:
                summary = build_ir_summary("RULE2", agent_name, payroll_month, payroll_cycle, state["rule2_breakdown"])
                report_number = file_incident_report(cur, employee_id, agent_name, r["record_date"], summary)

                subject, body = build_rule2_email(
                    agent_name, employee_id, period_key,
                    pair_total, tl_email, state["rule2_breakdown"], report_number,
                )
                send_email(to_address, subject, body, cc_addresses=cc_addresses)
                state["rule2_triggers_sent"] += 1
            state["rule2_pair_since_reset"] = 0
            state["rule2_breakdown"] = []

        # -- Rule 3: single instance > 2hrs, evaluated immediately --
        if minutes > RULE3_SINGLE_THRESHOLD_MIN:
            single_entry = [{"date": record_date_str, "date_display": date_display, "minutes": minutes, "duration_str": duration_str}]
            summary = build_ir_summary("RULE3", agent_name, payroll_month, payroll_cycle, single_entry)
            report_number = file_incident_report(cur, employee_id, agent_name, r["record_date"], summary)

            subject, body = build_rule3_email(
                agent_name, employee_id, period_key,
                minutes, record_date_str, tl_email, report_number,
            )
            send_email(to_address, subject, body, cc_addresses=cc_addresses)
            state["rule3_triggers_sent"] += 1

        state["last_processed_row_uid"] = r["row_uid"]
        save_state(cur, state)

        if not DRY_RUN:
            cur.execute("UPDATE overbreak_records SET notify_processed = 1 WHERE id = %s", (r["id"],))

    if DRY_RUN:
        print("[overbreak_notify][DRY RUN] Rolling back - no state, notify_processed, or IR changes saved.")
        conn.rollback()
        cur.close()
        conn.close()
        return

    conn.commit()
    cur.close()
    conn.close()
    print("[overbreak_notify] Done.")


if __name__ == "__main__":
    process_pending()
