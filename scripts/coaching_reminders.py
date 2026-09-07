#!/usr/bin/env python3
"""
coaching_reminders.py — daily nag for incomplete coaching action plans.

Rule (per Binbin): first reminder 2 days after the session, then DAILY until
the agent completes. Sessions past a 3-day SLA get 'overdue' framing.

Recipients: agent (gsheet_employees.email), cc TL (tl_view_map.login_email for
the agent's tl name) + SOM (agent's approver email). Sent via the portal's
threaded send_email(to, subject, body) — a single comma-joined recipient string.

Reads MAIN_DB_* + SMTP_* from the portal .env. Dry-run by default; --send to
actually email. Mirrors fts_notify.py / overbreak_notify.py conventions.

    cd /var/www/html/leavesystem
    python3 scripts/coaching_reminders.py            # dry-run (lists who'd get mailed)
    python3 scripts/coaching_reminders.py --send     # actually send

Cron (daily 9 AM):
    0 9 * * * cd /var/www/html/leavesystem && /usr/bin/python3 scripts/coaching_reminders.py --send >> /var/log/coaching_reminders.log 2>&1
"""
import os, sys, smtplib
from datetime import datetime, date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
try:
    from dotenv import load_dotenv; load_dotenv()
except ImportError:
    pass
import pymysql

SEND = "--send" in sys.argv
SLA_DAYS = 3
FIRST_DAY = 2               # first reminder N days after session_date
REMIND_EVERY_HOURS = 20     # don't re-send within this window (daily cadence)
PENDING = ("pending", "for_followup", "pending_followup")

DB = dict(host=os.environ["MAIN_DB_HOST"], port=int(os.environ.get("MAIN_DB_PORT", 3306)),
          user=os.environ["MAIN_DB_USER"], password=os.environ["MAIN_DB_PASSWORD"],
          database=os.environ["MAIN_DB_NAME"], cursorclass=pymysql.cursors.DictCursor)

SMTP_SERVER = os.getenv("SMTP_SERVER"); SMTP_PORT = int(os.getenv("SMTP_PORT", 2525))
SMTP_USER = os.getenv("SMTP_USER"); SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
FROM_NAME = os.getenv("SMTP_FROM_NAME", "Cohere HR Portal")
BASE_URL = os.getenv("PORTAL_BASE_URL", "https://hrportal.cohere.ph")


def send_mail(to_list, subject, html):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{FROM_NAME} <{SMTP_USER}>"
    msg["To"] = ", ".join(to_list)
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=15) as smtp:
        smtp.login(SMTP_USER, SMTP_PASSWORD)
        smtp.sendmail(SMTP_USER, to_list, msg.as_string())


def html_body(r, overdue):
    link = f"{BASE_URL}/coaching/my/{r['id']}"
    banner = "#c05621" if overdue else "#2563eb"
    head = ("Your coaching action plan is overdue" if overdue
            else "Action needed: complete your coaching action plan")
    sla_note = (f"<p style='font-size:.85rem;color:#c05621;font-weight:600;'>"
                f"This session is past the {SLA_DAYS}-day completion window. "
                f"Please act today.</p>") if overdue else ""
    return f"""\
<div style="font-family:Segoe UI,Arial,sans-serif;max-width:560px;margin:0 auto;color:#1f2937;">
  <div style="background:{banner};color:#fff;padding:16px 20px;border-radius:10px 10px 0 0;">
    <h2 style="margin:0;font-size:1.15rem;">{head}</h2></div>
  <div style="border:1px solid #e5e7eb;border-top:none;border-radius:0 0 10px 10px;padding:20px;">
    <p>Hi {r.get('agent_name') or 'there'},</p>
    <p>{r.get('supervisor_name') or 'Your team lead'} conducted a coaching session with you
       on {r['session_date']} — topic: <strong>{r.get('topic') or 'Coaching session'}</strong>.</p>
    <p>Please log in, review the feedback, and complete your <strong>SMART Action Plan</strong>.</p>
    <p style="text-align:center;margin:24px 0;">
      <a href="{link}" style="background:{banner};color:#fff;text-decoration:none;
         padding:11px 22px;border-radius:8px;font-weight:600;display:inline-block;">
        Complete My Action Plan</a></p>
    <p style="font-size:.85rem;color:#6b7280;">A plan is <strong>SMART</strong> when it's
       Specific, Measurable, Achievable, Relevant, and Time-bound.</p>
    {sla_note}
    <hr style="border:none;border-top:1px solid #e5e7eb;margin:18px 0;">
    <p style="font-size:.75rem;color:#9ca3af;">Cohere HR Portal · automated reminder</p>
  </div></div>"""


def main():
    print(f"[{'SEND' if SEND else 'DRY-RUN'}] coaching reminders  {datetime.now():%Y-%m-%d %H:%M}")
    conn = pymysql.connect(**DB)
    sent = skipped = 0
    try:
        with conn.cursor() as cur:
            # Candidates: pending, at least FIRST_DAY days old, not reminded within the window.
            cur.execute(f"""
                SELECT cs.id, cs.topic, cs.session_date, cs.status,
                       cs.last_reminder_at, cs.reminder_count,
                       a.schedule_name AS agent_name, a.email AS agent_email,
                       a.tl AS agent_tl, a.approver AS som_email,
                       s.schedule_name AS supervisor_name
                FROM coaching_sessions cs
                LEFT JOIN gsheet_employees a
                  ON a.employee_id = cs.agent_id COLLATE utf8mb4_unicode_ci
                LEFT JOIN gsheet_employees s
                  ON s.employee_id = cs.supervisor_id COLLATE utf8mb4_unicode_ci
                WHERE cs.status IN %s
                  AND cs.session_date <= (CURDATE() - INTERVAL %s DAY)
                  AND (cs.last_reminder_at IS NULL
                       OR cs.last_reminder_at < (NOW() - INTERVAL %s HOUR))
                ORDER BY cs.session_date ASC
            """, (PENDING, FIRST_DAY, REMIND_EVERY_HOURS))
            rows = cur.fetchall()

            # TL email map
            cur.execute("SELECT tl_name, login_email FROM tl_view_map")
            tlmap = {x["tl_name"]: x["login_email"] for x in cur.fetchall()}

        print(f"  {len(rows)} session(s) due for a reminder\n")
        for r in rows:
            agent = (r.get("agent_email") or "").strip()
            if not agent:
                print(f"  SKIP #{r['id']} — no agent email"); skipped += 1; continue
            cc = []
            tl = tlmap.get(r.get("agent_tl"))
            if tl: cc.append(tl.strip())
            som = (r.get("som_email") or "").strip()
            if som and som not in cc: cc.append(som)
            overdue = (date.today() - r["session_date"]).days > SLA_DAYS if r["session_date"] else False
            tag = "OVERDUE" if overdue else "reminder"
            recips = [agent] + cc
            print(f"  #{r['id']} [{tag}] -> {agent}"
                  + (f" (cc {', '.join(cc)})" if cc else "")
                  + f"  [sent {r['reminder_count']}x]")
            if SEND:
                try:
                    subject = ("Overdue: complete your coaching action plan" if overdue
                               else "Action needed: complete your coaching action plan")
                    send_mail(recips, subject, html_body(r, overdue))
                    with conn.cursor() as cur:
                        cur.execute("""UPDATE coaching_sessions
                                       SET last_reminder_at=NOW(),
                                           reminder_count=reminder_count+1
                                       WHERE id=%s""", (r["id"],))
                    conn.commit()
                    sent += 1
                except Exception as e:
                    print(f"     ERROR sending #{r['id']}: {e}"); skipped += 1
        print(f"\n  {'sent' if SEND else 'would send'}: {len(rows)-skipped} · skipped: {skipped}")
    finally:
        conn.close()
    print("Done." + ("" if SEND else "  Re-run with --send to actually email."))


if __name__ == "__main__":
    main()
