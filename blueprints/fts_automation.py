import os
import click
import smtplib
from email.message import EmailMessage
from datetime import datetime, timedelta
from flask import Blueprint

# Import centralized DB connection helper
from db_core import get_db_connection

# Define blueprint
fts_bp = Blueprint('fts', __name__)

STATIC_RECIPIENTS = [
    'andrewvincentt@gmail.com',
    'jericho@yourcompany.com'
]

def send_smtp_email(subject, body, recipients):
    """Sends email using standard library smtplib with credentials from .env"""
    smtp_server = os.environ.get('SMTP_SERVER', 'cohere.ph')
    smtp_port = int(os.environ.get('SMTP_PORT', 2525))
    smtp_user = os.environ.get('SMTP_USER', 'send_email@cohere.ph')
    smtp_password = os.environ.get('SMTP_PASSWORD', '')

    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = smtp_user
    msg['To'] = ", ".join(recipients)
    msg.set_content(body)

    with smtplib.SMTP(smtp_server, smtp_port) as server:
        try:
            server.starttls()
        except Exception:
            pass  # Continue if server does not require STARTTLS on this port
        if smtp_password:
            server.login(smtp_user, smtp_password)
        server.send_message(msg)

@fts_bp.cli.command('send-yesterday-summary')
def send_fts_yesterday_summary():
    """Daily job triggered at 4:00 PM for yesterday's completed FTS."""
    yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
    click.echo(f"[{datetime.now()}] Processing FTS summary for date: {yesterday}")

    query = """
        SELECT 
            fts.id,
            fts.employee_id,
            fts.fts_date,
            fts.reason,
            fts.status,
            e.employee_name,
            e.email AS employee_email,
            e.som_email,
            tl.tl_email
        FROM fts_requests fts
        JOIN gsheet_employees e 
            ON fts.employee_id = e.employee_id
        LEFT JOIN tl_view_map tl 
            ON e.tl_name = tl.tl_name
        WHERE DATE(fts.fts_date) = %s
          AND LOWER(fts.status) IN ('completed', 'approved')
    """

    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute(query, (yesterday,))
            fts_records = cursor.fetchall()

        if not fts_records:
            click.echo(f"No completed FTS records found for date: {yesterday}")
            return

        emails_sent = 0
        for record in fts_records:
            recipients = set(STATIC_RECIPIENTS)

            if record.get('tl_email'):
                recipients.add(record['tl_email'])
            if record.get('som_email'):
                recipients.add(record['som_email'])

            valid_recipients = [r.strip() for r in recipients if r and '@' in r]

            if not valid_recipients:
                click.echo(f"Skipping record ID {record['id']} - no valid email recipients found.")
                continue

            subject = f"[FTS Alert] Completed Schedule FTS - {record['employee_name']} ({yesterday})"
            body = f"""Hello,

This is an automated notification regarding a completed Failure to Swipe (FTS) schedule.

Details:
--------------------------------------------------
Employee Name : {record['employee_name']} ({record['employee_id']})
Schedule Date : {yesterday}
FTS Status    : {record['status'].capitalize()}
Reason        : {record.get('reason', 'N/A')}
--------------------------------------------------

This email was automatically routed to TL, SOM, WFM, and Management.
"""

            try:
                send_smtp_email(subject, body, valid_recipients)
                emails_sent += 1
                click.echo(f"Sent FTS alert for {record['employee_name']} to {valid_recipients}")
            except Exception as e:
                click.echo(f"Failed to send email for FTS ID {record['id']}: {str(e)}")

        click.echo(f"Finished processing. Total emails sent: {emails_sent}")

    except Exception as err:
        click.echo(f"Database error: {str(err)}")
    finally:
        if conn:
            conn.close()