"""
Mailman moderation-queue check (cron).

Reads every Mailman list's queue (held posts + membership requests) the same
way the portal's Pending requests page does, stores the counts in
central_db.mailing_list_pending (the portal's menu badge reads them), and
emails an alert listing what is NEW since the last alert.

    venv/bin/python scripts/mailing_list_pending_check.py            # dry run: print, store nothing
    venv/bin/python scripts/mailing_list_pending_check.py --store    # store counts (badge), no email
    venv/bin/python scripts/mailing_list_pending_check.py --send     # store counts + email new items
    venv/bin/python scripts/mailing_list_pending_check.py --test     # email a sample of the current queue; changes nothing

"New" = request ids above the last emailed id per list (Mailman ids only go
up). On the very first stored run the existing backlog is taken as the
starting point, so the first email does not list months of old items.

.env: MAILING_LIST_ALERT_EMAILS (comma-separated recipients; no email if
unset), PORTAL_BASE_URL, SMTP_* (existing), CPANEL_* (existing).

Cron (root):
    */30 * * * *  cd /var/www/html/leavesystem && venv/bin/python scripts/mailing_list_pending_check.py --store >> mailing_list_pending_check.log 2>&1
    0 8 * * *     cd /var/www/html/leavesystem && venv/bin/python scripts/mailing_list_pending_check.py --send  >> mailing_list_pending_check.log 2>&1
"""

import html
import os
import smtplib
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from db_core import get_db_connection  # noqa: E402
from modules import mailing_lists as ml  # noqa: E402


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] [mailing_list_pending] {msg}", flush=True)


def read_all():
    """[(address, items or None, error or None)] for every Mailman list."""
    lists, _ = ml.fetch_mailman_lists(refresh=True)

    def one(addr):
        try:
            items, _ = ml.fetch_pending(addr, refresh=True)
            return addr, items, None
        except Exception as e:  # one broken list must not stop the rest
            return addr, None, str(e)[:500]

    with ThreadPoolExecutor(ml.READ_WORKERS) as ex:
        return list(ex.map(one, sorted(lists)))


def suspicious(item):
    return bool(item.get("spam") and (item["spam"]["flag"] or item["spam"]["phish"]))


def load_state(cur):
    cur.execute("SELECT list_address, notified_max_id FROM mailing_list_pending")
    return {r["list_address"]: r["notified_max_id"] for r in cur.fetchall()}


def store(cur, addr, items, error, notified_max_id):
    if items is None:
        cur.execute("""
            INSERT INTO mailing_list_pending (list_address, error, checked_at)
            VALUES (%s, %s, NOW())
            ON DUPLICATE KEY UPDATE error = VALUES(error), checked_at = NOW()
        """, (addr, error))
        return
    posts = [i for i in items if i["kind"] == "post"]
    cur.execute("""
        INSERT INTO mailing_list_pending
            (list_address, posts, subs, suspicious, max_request_id, notified_max_id, error, checked_at)
        VALUES (%s, %s, %s, %s, %s, %s, NULL, NOW())
        ON DUPLICATE KEY UPDATE posts = VALUES(posts), subs = VALUES(subs), suspicious = VALUES(suspicious),
            max_request_id = VALUES(max_request_id), notified_max_id = VALUES(notified_max_id),
            error = NULL, checked_at = NOW()
    """, (addr, len(posts), len(items) - len(posts), sum(suspicious(i) for i in posts),
          max((i["id"] for i in items), default=0), notified_max_id))


def build_email(new_by_list, totals):
    base = (os.environ.get("PORTAL_BASE_URL") or "https://hrportal.cohere.ph").rstrip("/")
    n_new = sum(len(v) for v in new_by_list.values())
    parts = [f"<p>{n_new} new item{'s' if n_new != 1 else ''} waiting for approval on "
             f"{len(new_by_list)} Mailman list{'s' if len(new_by_list) != 1 else ''}.</p>"]
    for addr, items in sorted(new_by_list.items()):
        link = f"{base}/admin/mailing-lists/mailman/{addr}/pending"
        posts_total, subs_total = totals[addr]
        parts.append(f"<h3 style='margin:18px 0 4px'>{html.escape(addr)}</h3>"
                     f"<div style='color:#666;font-size:13px'>{posts_total} held message(s) and {subs_total} "
                     f"membership request(s) in total · <a href='{html.escape(link)}'>Open Pending requests</a></div>")
        rows = []
        for i in items[:25]:
            flags = []
            if i.get("spam") and i["spam"]["phish"]:
                flags.append("<b style='color:#c93a39'>Phishing link</b>")
            if i.get("spam") and i["spam"]["flag"]:
                flags.append("<b style='color:#c93a39'>Spam</b>")
            what = (html.escape(i["subject"] or "(no subject)") if i["kind"] == "post"
                    else ("Asks to join" if i["kind"] == "subscribe" else "Asks to leave"))
            rows.append(f"<tr><td style='padding:3px 10px 3px 0'>{html.escape(i['sender'] or '?')}</td>"
                        f"<td style='padding:3px 10px 3px 0'>{what}</td>"
                        f"<td style='padding:3px 0'>{' '.join(flags)}</td></tr>")
        more = f"<div style='color:#666;font-size:13px'>…and {len(items) - 25} more</div>" if len(items) > 25 else ""
        parts.append(f"<table style='border-collapse:collapse;font-size:13px'>{''.join(rows)}</table>{more}")
    parts.append("<p style='color:#666;font-size:12px'>Accepting a message sends it to every member of the list. "
                 "Discard spam and phishing; never click links in held messages.</p>")
    subject = f"Mailing lists: {n_new} new item{'s' if n_new != 1 else ''} waiting for approval"
    return subject, "".join(parts)


def send(recipients, subject, body_html):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{os.environ.get('SMTP_FROM_NAME', 'HR Portal')} <{os.environ['SMTP_USER']}>"
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(body_html, "html", "utf-8"))
    with smtplib.SMTP(os.environ["SMTP_SERVER"], int(os.environ.get("SMTP_PORT", "587"))) as server:
        server.starttls()
        server.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        server.sendmail(os.environ["SMTP_USER"], recipients, msg.as_string())


def send_test():
    """Email the newest few queue items as a sample, to check the alert path."""
    recipients = [r.strip() for r in (os.environ.get("MAILING_LIST_ALERT_EMAILS") or "").split(",") if r.strip()]
    if not recipients:
        log("MAILING_LIST_ALERT_EMAILS is not set")
        return
    sample, totals = {}, {}
    for addr, items, error in read_all():
        if items:
            posts = sum(1 for i in items if i["kind"] == "post")
            totals[addr] = (posts, len(items) - posts)
            sample[addr] = sorted(items, key=lambda i: -i["id"])[:3]
    subject, body = build_email(sample, totals)
    body = ("<p style='background:#fff3cd;padding:8px'><b>Test email.</b> This is what the daily alert looks like; "
            "it shows the newest 3 items per list, not only new ones.</p>") + body
    send(recipients, "[TEST] " + subject, body)
    log(f"test email sent to {', '.join(recipients)}")


def main(argv):
    if "--test" in argv:
        return send_test()
    do_store = "--store" in argv or "--send" in argv
    do_send = "--send" in argv
    mode = "SEND" if do_send else "STORE" if do_store else "DRY RUN"
    recipients = [r.strip() for r in (os.environ.get("MAILING_LIST_ALERT_EMAILS") or "").split(",") if r.strip()]

    results = read_all()
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            state = load_state(cur)
            new_by_list, totals, next_ids = {}, {}, {}
            for addr, items, error in results:
                if items is None:
                    log(f"{addr}: could not read ({error})")
                    if do_store:
                        store(cur, addr, None, error, state.get(addr))
                    continue
                max_id = max((i["id"] for i in items), default=0)
                seen = state.get(addr)
                first_run = addr not in state or seen is None
                new = [] if first_run else [i for i in items if i["id"] > seen]
                posts = sum(1 for i in items if i["kind"] == "post")
                totals[addr] = (posts, len(items) - posts)
                log(f"{addr}: {posts} held, {len(items) - posts} requests, {len(new)} new"
                    + (" (first run: backlog taken as the starting point)" if first_run else ""))
                if new:
                    new_by_list[addr] = new
                # first run: start from the current backlog; --send: mark as emailed
                next_ids[addr] = max_id if (first_run or do_send) else seen
                if do_store:
                    store(cur, addr, items, None, next_ids[addr] if (first_run or not new or do_send) else seen)
            if new_by_list and do_send:
                if not recipients:
                    log("new items, but MAILING_LIST_ALERT_EMAILS is not set: no email sent")
                else:
                    subject, body = build_email(new_by_list, totals)
                    send(recipients, subject, body)
                    log(f"emailed {', '.join(recipients)}: {subject}")
            elif new_by_list:
                log(f"[{mode}] would email: {build_email(new_by_list, totals)[0]}")
            if do_store:
                conn.commit()
            else:
                conn.rollback()
    finally:
        conn.close()
    log(f"[{mode}] done")


if __name__ == "__main__":
    main(sys.argv[1:])
