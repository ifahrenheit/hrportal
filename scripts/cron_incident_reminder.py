import os
#!/usr/bin/env python3
"""
cron_incident_reminder.py
Standalone script — run daily via cron to alert on stale incidents.

Cron example (8:30 AM daily):
  30 8 * * * /usr/bin/python3 /var/www/html/leavesystem/cron_incident_reminder.py >> /var/log/ir_reminder.log 2>&1
"""

import pymysql
import pymysql.cursors
import smtplib
import logging
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv
load_dotenv()

# ─── Config ───────────────────────────────────────────────────────────────────
DB = dict(host='localhost', user='root', password=os.environ.get('DB_PASSWORD'),
          database='central_db', cursorclass=pymysql.cursors.DictCursor, charset='utf8mb4')

SMTP_HOST = 'cohere.ph';  SMTP_PORT = 2525
SMTP_USER = 'send_email@cohere.ph';  SMTP_PASS = '***REMOVED***'
SMTP_FROM = 'wfm@cohere.ph';  SMTP_FROM_NAME = 'Incident Report System'
IR_EMAIL_TO  = 'managers@cohere.ph'
IR_EMAIL_BCC = 'andrewvincentt@gmail.com'

logging.basicConfig(format='%(asctime)s %(message)s', level=logging.INFO)

# ─── DB helpers ───────────────────────────────────────────────────────────────
def get_db():
    return pymysql.connect(**DB)

def get_supervisor_emails(conn, incident_list):
    emails = set()
    with conn.cursor() as cur:
        for inc in incident_list:
            cur.execute("""
                SELECT sm.supervisor_email
                FROM supervisor_mapping sm
                INNER JOIN gsheet_employees g ON sm.agent_email = g.email
                WHERE g.employee_id = %s LIMIT 1
            """, (inc['employee_id'],))
            row = cur.fetchone()
            if row: emails.add(row['supervisor_email'])
    return emails

def get_group_emails(conn, incident_list):
    emails = set()
    with conn.cursor() as cur:
        for inc in incident_list:
            cur.execute("""
                SELECT DISTINCT g2.email
                FROM gsheet_employees g1
                INNER JOIN gsheet_employees g2 ON g1.group_name = g2.group_name
                WHERE g1.employee_id = %s
                  AND g1.group_name IS NOT NULL
                  AND g1.group_name != '' AND g1.group_name != 'TL'
                  AND g2.status = 'Active' AND g2.email IS NOT NULL
            """, (inc['submitted_by_id'],))
            for row in cur.fetchall():
                emails.add(row['email'])
    return emails

# ─── Email ────────────────────────────────────────────────────────────────────
def send_reminder(incidents):
    rows_html = ''
    for inc in incidents:
        hours = inc['hours_since_activity']
        days  = int(hours // 24)
        color = '#ffc107' if inc['status'] == 'pending' else '#0f2557'
        tcolor = '#000'   if inc['status'] == 'pending' else '#fff'
        summary_short = (inc['summary'][:100] + '…') if len(inc['summary']) > 100 else inc['summary']
        dt = inc['incident_date']
        dt_str = dt.strftime('%b %d, %Y') if hasattr(dt, 'strftime') else str(dt)
        rows_html += f"""
        <tr style="border-bottom:1px solid #e0e0e0;">
          <td style="padding:10px;">
            <a href="https://hrportal.cohere.ph/incident-reports/{inc['report_number']}"
               style="color:#0f2557;font-weight:700;text-decoration:none;">{inc['report_number']}</a>
          </td>
          <td style="padding:10px;">{dt_str}</td>
          <td style="padding:10px;">{inc['employee_name']}</td>
          <td style="padding:10px;">
            <span style="background:{color};color:{tcolor};padding:3px 9px;border-radius:12px;
                         font-size:11px;font-weight:700;">{inc['status'].upper()}</span>
          </td>
          <td style="padding:10px;color:#dc3545;font-weight:700;">{days}d ago</td>
          <td style="padding:10px;max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">{summary_short}</td>
        </tr>"""

    body = f"""
    <html><body style="font-family:Arial,sans-serif;color:#333;">
    <div style="max-width:780px;margin:0 auto;">
      <div style="background:linear-gradient(135deg,#dc3545,#ff6b35);color:white;padding:28px;text-align:center;border-radius:10px 10px 0 0;">
        <h2 style="margin:0;">⏰ Stale Incident Report Reminder</h2>
        <p style="margin:8px 0 0;font-size:16px;">{len(incidents)} incident(s) need attention</p>
      </div>
      <div style="background:#f9f9f9;padding:25px;border:1px solid #ddd;">
        <div style="background:#fff3cd;border-left:4px solid #ff6b35;padding:14px;border-radius:4px;margin-bottom:20px;">
          <strong>⚠️ Action Required:</strong> The following incidents have been in
          <strong>pending</strong> or <strong>reviewed</strong> status for more than 72 hours
          without any updates.
        </div>
        <table style="width:100%;border-collapse:collapse;background:white;box-shadow:0 2px 5px rgba(0,0,0,.1);">
          <thead>
            <tr>
              {''.join(f'<th style="background:linear-gradient(135deg,#0f2557,#1e3a8a);color:white;padding:11px;text-align:left;font-size:12px;text-transform:uppercase;">{h}</th>'
                       for h in ['IR Number','Incident Date','Agent','Status','Last Activity','Summary'])}
            </tr>
          </thead>
          <tbody>{rows_html}</tbody>
        </table>
        <div style="text-align:center;margin-top:28px;">
          <a href="https://hrportal.cohere.ph/incident-reports/"
             style="background:linear-gradient(135deg,#0f2557,#ff6b35);color:white;padding:12px 28px;
                    text-decoration:none;border-radius:8px;font-weight:bold;">📊 View All Incidents</a>
        </div>
      </div>
      <div style="background:#333;color:white;padding:12px;text-align:center;font-size:12px;border-radius:0 0 10px 10px;">
        ⚡ Automated Reminder from Incident Report System
      </div>
    </div></body></html>"""

    conn = get_db()
    try:
        recipients = {IR_EMAIL_TO}
        recipients |= get_supervisor_emails(conn, incidents)
        recipients |= get_group_emails(conn, incidents)
    finally:
        conn.close()

    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = f"Incident Report Reminder: {len(incidents)} Stale Incident(s) Require Attention"
        msg['From']    = f"{SMTP_FROM_NAME} <{SMTP_FROM}>"
        msg['To']      = ', '.join(recipients)
        msg.attach(MIMEText(body, 'html'))
        all_rcpt = list(recipients) + [IR_EMAIL_BCC]
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls(); s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(SMTP_FROM, all_rcpt, msg.as_string())
        return True
    except Exception as e:
        logging.error(f"Reminder email failed: {e}")
        return False

# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT ir.id, ir.report_number, ir.incident_date, ir.employee_name,
                       ir.employee_id, ir.summary, ir.status, ir.updated_at,
                       ir.submitted_by_id, ir.last_reminder_sent,
                       COALESCE(MAX(ic.created_at), ir.updated_at) as last_activity,
                       TIMESTAMPDIFF(HOUR,
                           COALESCE(MAX(ic.created_at), ir.updated_at), NOW()
                       ) as hours_since_activity
                FROM incident_reports ir
                LEFT JOIN incident_comments ic ON ir.id = ic.report_id
                WHERE ir.status IN ('pending', 'reviewed')
                GROUP BY ir.id
                HAVING hours_since_activity >= 72
                   AND (ir.last_reminder_sent IS NULL
                        OR TIMESTAMPDIFF(HOUR, ir.last_reminder_sent, NOW()) >= 24)
                ORDER BY hours_since_activity DESC
            """)
            stale = cur.fetchall()

        if not stale:
            logging.info("No stale incidents found.")
            return

        logging.info(f"Found {len(stale)} stale incident(s).")
        if send_reminder(stale):
            ids = ','.join(str(i['id']) for i in stale)
            with conn.cursor() as cur:
                cur.execute(f"UPDATE incident_reports SET last_reminder_sent=NOW() WHERE id IN ({ids})")
            conn.commit()
            logging.info("Reminder sent and timestamps updated.")
        else:
            logging.error("Failed to send reminder email.")
    except Exception as e:
        logging.error(f"Cron error: {e}")
    finally:
        conn.close()

if __name__ == '__main__':
    main()