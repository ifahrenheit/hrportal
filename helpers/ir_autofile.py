"""
ir_autofile.py
Shared helper for auto-filing system-generated Incident Reports from
notification cron scripts (tardiness_notify.py, overbreak_notify.py).

Mirrors incident_reports.py's submit_report() escalate_to_hr branch, but
system-filed (submitted_by_id='SYSTEM', submitted_by_name='System') since
there's no logged-in user in a cron context. Status starts at
'rwe_request' (escalated straight to HR).

Note: overbreak_notify.py has its own inline copy of this same logic
(written before this shared module existed) rather than importing from
here, to avoid touching a working script. New notify scripts should
import from here instead.
"""
import uuid, os, smtplib
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

def _send_ir_notification(cur, report_number, incident_date, employee_id,
                           employee_name, summary, log_prefix="[ir_autofile]"):
    """Send the same IR notification email as manual filing."""
    try:
        smtp_server = os.environ.get("SMTP_SERVER", "cohere.ph")
        smtp_port   = int(os.environ.get("SMTP_PORT", 2525))
        smtp_user   = os.environ.get("SMTP_USER", "send_email@cohere.ph")
        smtp_pass   = os.environ.get("SMTP_PASSWORD", "")
        from_name   = os.environ.get("IR_FROM_NAME", "Incident Report System")
        bcc         = os.environ.get("OVERBREAK_NOTIFY_BCC", "andrewvincentt@gmail.com")

        # Get supervisor email
        cur.execute("""
            SELECT sm.supervisor_email
            FROM supervisor_mapping sm
            INNER JOIN gsheet_employees g
                ON sm.agent_email COLLATE utf8mb4_unicode_ci = g.email
            WHERE g.employee_id = %s LIMIT 1
        """, (employee_id,))
        row = cur.fetchone()
        supervisor_email = row["supervisor_email"] if row else None


        recipients = list(filter(None, [supervisor_email]))

        if not recipients:
            print(f"{log_prefix} No supervisor found for {employee_id}, skipping IR email.")
            return

        if not isinstance(incident_date, str):
            date_str = incident_date.strftime("%B %d, %Y")
        else:
            date_str = incident_date

        url = f"https://hrportal.cohere.ph/incident-reports/{report_number}"
        body = f"""<html><body style="font-family:'Segoe UI',Arial,sans-serif;background:#f0f2f8;margin:0;padding:20px;">
    <div style="max-width:580px;margin:0 auto;">
      <div style="background:#0f2744;border-radius:12px 12px 0 0;padding:28px 32px;text-align:center;">
        <div style="font-size:12px;font-weight:600;color:#93c5fd;letter-spacing:1px;text-transform:uppercase;margin-bottom:6px;">Incident Report System</div>
        <h1 style="margin:0;font-size:22px;font-weight:700;color:#fff;">New Incident Report Filed</h1>
        <div style="margin-top:8px;font-size:13px;color:#94a3b8;">Report #{report_number}</div>
      </div>
      <div style="background:#fff;padding:28px 32px;border:1px solid #e2e8f4;">
        <div style="background:#fef3c7;border-left:3px solid #f59e0b;border-radius:6px;padding:12px 14px;margin-bottom:18px;font-size:13px;color:#92400e;">
          <strong>⚠️ Action Required:</strong> This incident was auto-filed by the system and requires your attention.
        </div>
        <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
          <tr><td style="padding:10px 0;border-bottom:1px solid #e2e8f4;">
            <div style="font-size:11px;font-weight:700;color:#6b7a99;text-transform:uppercase;">Date of Incident</div>
            <div style="font-size:14px;color:#1a2440;">{date_str}</div>
          </td></tr>
          <tr><td style="padding:10px 0;border-bottom:1px solid #e2e8f4;">
            <div style="font-size:11px;font-weight:700;color:#6b7a99;text-transform:uppercase;">Agent Involved</div>
            <div style="font-size:14px;color:#1a2440;">{employee_name}</div>
          </td></tr>
          <tr><td style="padding:10px 0;">
            <div style="font-size:11px;font-weight:700;color:#6b7a99;text-transform:uppercase;">Summary</div>
            <div style="font-size:14px;color:#1a2440;">{summary.replace(chr(10), '<br>')}</div>
          </td></tr>
        </table>
        <div style="text-align:center;margin-top:28px;">
          <a href="{url}" style="display:inline-block;background:#1e5fd4;color:#fff;padding:13px 32px;
             text-decoration:none;border-radius:8px;font-weight:700;font-size:14px;">View Report</a>
        </div>
      </div>
      <div style="background:#f0f2f8;border-radius:0 0 12px 12px;padding:14px 32px;
                  text-align:center;font-size:11px;color:#6b7a99;border:1px solid #e2e8f4;border-top:none;">
        Cohere HR Portal · Incident Report System · Auto-filed
      </div>
    </div></body></html>"""

        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"New Incident Report: {report_number}"
        msg["From"]    = f"{from_name} <{smtp_user}>"
        msg["To"]      = ", ".join(recipients)
        msg.attach(MIMEText(body, "html"))

        all_rcpt = list(set(recipients + [bcc]))
        with smtplib.SMTP(smtp_server, smtp_port) as s:
            s.login(smtp_user, smtp_pass)
            s.sendmail(smtp_user, all_rcpt, msg.as_string())
        print(f"{log_prefix} IR notification email sent for {report_number} to {recipients}")
    except Exception as e:
        print(f"{log_prefix} IR email failed for {report_number}: {e}")

IR_REPORT_BASE_URL = "https://hrportal.cohere.ph/incident-reports"


def generate_ir_report_number():
    return "IR-" + datetime.now().strftime("%Y%m%d") + "-" + str(uuid.uuid4())[:6].upper()


def get_agent_full_name(cur, employee_id, fallback=None):
    cur.execute("SELECT schedule_name FROM gsheet_employees WHERE employee_id = %s LIMIT 1", (employee_id,))
    row = cur.fetchone()
    return (row["schedule_name"] if row and row.get("schedule_name") else None) or fallback


def file_incident_report(cur, employee_id, agent_name, incident_date, summary, dry_run=False, log_prefix="[ir_autofile]",
                         submitted_by_id="SYSTEM", submitted_by_name="System"):
    employee_name = get_agent_full_name(cur, employee_id, fallback=agent_name)
    report_number = generate_ir_report_number()

    if dry_run:
        print(f"{log_prefix}[DRY RUN] Would file IR '{report_number}' for {employee_name} ({employee_id})")
        return report_number

    cur.execute(
        """INSERT INTO incident_reports
            (report_number, incident_date, employee_id, employee_name,
             submitted_by_id, submitted_by_name, summary, status)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
        (report_number, incident_date, employee_id, employee_name,
         submitted_by_id, submitted_by_name, summary, "pending"),
    )
    print(f"{log_prefix} Filed IR '{report_number}' for {employee_name} ({employee_id})")
    _send_ir_notification(cur, report_number, incident_date, employee_id,
                          employee_name, summary, log_prefix)
    return report_number


def ir_notice_html(report_number):
    url = f"{IR_REPORT_BASE_URL}/{report_number}"
    return f"""
    <p style="background:#fff3cd;padding:10px 14px;border-radius:6px;border:1px solid #ffe69c;">
      <strong>⚠️ An Incident Report has been automatically filed and escalated to HR:</strong><br>
      <a href="{url}">{report_number}</a>
    </p>
    """
