#!/usr/bin/env python
"""
Daily system health alert -- emails a short list when something on the server
needs fixing. Same checks as Admin > System Health (helpers/system_health.py).

  venv/bin/python scripts/system_health_check.py           # dry run: print only
  venv/bin/python scripts/system_health_check.py --send    # email if anything is at 'error'
  venv/bin/python scripts/system_health_check.py --send --always   # email even when all is OK

Recipients: HEALTH_ALERT_TO in .env (comma-separated). Only 'error' items
trigger an email; warnings are listed in it too, so one email shows the whole
picture. No email on a clean day.
"""
import argparse
import html
import os
import smtplib
import sys
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))

from helpers import system_health as sh  # noqa: E402

PAGE_URL = 'https://hrportal.cohere.ph/admin/system-health'
COLORS = {'error': '#b91c1c', 'warn': '#b45309', 'info': '#475569'}
LABEL = {'error': 'Problem', 'warn': 'Warning', 'info': 'Note'}


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def build_email(problems, now):
    errors = sum(1 for p in problems if p[0] == 'error')
    warns = sum(1 for p in problems if p[0] == 'warn')
    if errors:
        subject = f"[HR portal] {errors} server problem{'s' if errors != 1 else ''} need attention"
    else:
        subject = "[HR portal] Server health: all OK"
    rows = ''.join(
        f"<tr><td style='padding:4px 8px;color:{COLORS.get(st, '#333')};font-weight:600'>{LABEL.get(st, st)}</td>"
        f"<td style='padding:4px 8px;font-weight:600'>{html.escape(area)}</td>"
        f"<td style='padding:4px 8px'>{html.escape(text)}</td></tr>"
        for st, area, text in problems)
    body = (
        f"<p>Server health check, {now:%b %d, %Y %H:%M}: "
        f"<b>{errors}</b> problem(s), <b>{warns}</b> warning(s).</p>"
        + (f"<table style='border-collapse:collapse;font-family:sans-serif;font-size:13px'>{rows}</table>"
           if problems else "<p>Everything is OK.</p>")
        + f"<p>Details: <a href='{PAGE_URL}'>{PAGE_URL}</a></p>"
    )
    return subject, body


def send(recipients, subject, body):
    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = f"HR Portal Health <{os.environ['SMTP_USER']}>"
    msg['To'] = ', '.join(recipients)
    msg.attach(MIMEText(body, 'html'))
    with smtplib.SMTP(os.environ['SMTP_SERVER'], int(os.environ.get('SMTP_PORT', '587')),
                      timeout=30) as smtp:
        smtp.starttls()
        smtp.login(os.environ['SMTP_USER'], os.environ['SMTP_PASSWORD'])
        smtp.sendmail(os.environ['SMTP_USER'], recipients, msg.as_string())


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    ap.add_argument('--send', action='store_true', help='actually send the email')
    ap.add_argument('--always', action='store_true', help='email even when nothing is wrong')
    args = ap.parse_args(argv)

    report = sh.collect_all()
    problems = [p for p in sh.problems_list(report) if p[0] in ('error', 'warn')]
    errors = [p for p in problems if p[0] == 'error']
    log(f"overall={report['overall']} errors={len(errors)} warnings={len(problems) - len(errors)}")
    for st, area, text in problems:
        log(f"  {st:5} {area}: {text}")

    if not errors and not args.always:
        log("Nothing at 'error' level, no email.")
        return 0

    recipients = [r.strip() for r in os.environ.get('HEALTH_ALERT_TO', '').split(',') if r.strip()]
    if not recipients:
        log("HEALTH_ALERT_TO is not set in .env, can't email.")
        return 1
    subject, body = build_email(problems, report['now'])
    if not args.send:
        log(f"[DRY RUN] Would email '{subject}' to {', '.join(recipients)}")
        return 0
    try:
        send(recipients, subject, body)
        log(f"Emailed '{subject}' to {', '.join(recipients)}")
    except Exception as e:  # noqa: BLE001 - log and exit non-zero for cron
        log(f"Email failed: {e}")
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
