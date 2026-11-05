"""
fts_reminders.py — FTS filing check + agent reminder emails for the Absence Report.

Two pieces:
  fts_filing_map(records)   used by admin_absences() to show, on every FTS IN /
                            FTS OUT row, whether the agent already filed an FTS
                            request for it (Filed / Rejected / Not filed) and
                            when they were last reminded.
  /admin/absences/fts-reminders/preview + /send
                            the "Email FTS reminders" button: one email per
                            agent listing their unfiled (or rejected) FTS in the
                            chosen date range. The preview is read-only; the send
                            re-computes everything server-side and only emails
                            the employee IDs the admin ticked.

Matching an FTS request to a report row (fts_requests, central_db):
  same employee, same type (IN/OUT), not deleted/cancelled, and
    IN  -> fts_date = shift date
    OUT -> fts_date = shift date, or the next day when the shift ends after
           midnight (e.g. 7pm-6am: the OUT is filed on the next calendar date).
  Best status wins: Approved > Pending > Rejected.

Sends are logged in central_db.fts_reminder_log (created on first use).
"""
import os
import re
import smtplib
from datetime import date, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape

from flask import Blueprint, jsonify, request, session

fts_reminders_bp = Blueprint('fts_reminders', __name__)

PORTAL_FTS_URL = 'https://hrportal.cohere.ph/file-requests/new?type=fts'
FTS_STATUSES = {'FTS IN': 'IN', 'FTS OUT': 'OUT'}
_RANK = {'Approved': 3, 'Pending': 2, 'Rejected': 1}
MAX_RANGE_DAYS = 62

_table_ready = False


def _ensure_log_table(cur):
    global _table_ready
    if _table_ready:
        return
    cur.execute("""
        CREATE TABLE IF NOT EXISTS fts_reminder_log (
          id          INT AUTO_INCREMENT PRIMARY KEY,
          employee_id VARCHAR(50)  NOT NULL,
          shift_date  DATE         NOT NULL,
          fts_type    ENUM('IN','OUT') NOT NULL,
          email       VARCHAR(255) NOT NULL,
          sent_by     VARCHAR(255) NOT NULL,
          sent_at     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
          KEY idx_emp_date (employee_id, shift_date)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    _table_ready = True


def _hour(token):
    """'7pm' -> 19.0, '12mn' -> 0.0, '6:30am' -> 6.5; None if unreadable."""
    t = token.strip().lower()
    m = re.fullmatch(r'(\d{1,2})(?::(\d{2}))?(am|pm|nn|mn)', t)
    if not m:
        return None
    h, mins, suf = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if suf == 'mn':
        h = 0
    elif suf == 'nn':
        h = 12
    elif suf == 'am':
        h = 0 if h == 12 else h
    else:
        h = h if h == 12 else h + 12
    return h + mins / 60


def crosses_midnight(shift_time):
    """True if the shift ends on the next calendar day (end <= start).
    None when the shift text can't be read."""
    parts = (shift_time or '').replace(' ', '').split('-')
    if len(parts) != 2:
        return None
    s, e = _hour(parts[0]), _hour(parts[1])
    if s is None or e is None:
        return None
    return e <= s


def _as_date(v):
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def _fmt_time(v):
    if v is None:
        return ''
    if isinstance(v, timedelta):
        secs = int(v.total_seconds()) % 86400
        v = datetime(2000, 1, 1) + timedelta(seconds=secs)
    return v.strftime('%I:%M %p').lstrip('0')


def _key(emp_id, d, ftype):
    return f"{emp_id}|{_as_date(d).isoformat()}|{ftype}"


def fts_filing_map(records):
    """{'<employee_id>|<YYYY-MM-DD>|IN|OUT': info} for every FTS row in records.
    info = {state: filed|rejected|not_filed, status, fts_date, fts_time, reminded_at, reminded_count}"""
    from app import get_central_db
    rows = [r for r in records if r.get('attendance_status') in FTS_STATUSES]
    if not rows:
        return {}
    emp_ids = sorted({r['employee_id'] for r in rows})
    dates = [_as_date(r['absent_date']) for r in rows]
    d_from, d_to = min(dates), max(dates) + timedelta(days=1)

    conn = get_central_db()
    try:
        with conn.cursor() as cur:
            ph = ','.join(['%s'] * len(emp_ids))
            cur.execute(f"""
                SELECT employeeID AS employee_id, fts_date, fts_time, fts_type, status
                FROM fts_requests
                WHERE employeeID IN ({ph})
                  AND fts_date BETWEEN %s AND %s
                  AND deleted_at IS NULL
                  AND (status IS NULL OR status NOT IN ('Deleted','Cancelled'))
            """, (*emp_ids, d_from, d_to))
            filed = {}
            for f in cur.fetchall():
                filed.setdefault((f['employee_id'], f['fts_type']), []).append(f)

            _ensure_log_table(cur)
            cur.execute(f"""
                SELECT employee_id, shift_date, fts_type, MAX(sent_at) AS last_sent, COUNT(*) AS n
                FROM fts_reminder_log
                WHERE employee_id IN ({ph}) AND shift_date BETWEEN %s AND %s
                GROUP BY employee_id, shift_date, fts_type
            """, (*emp_ids, d_from, d_to))
            reminded = {_key(x['employee_id'], x['shift_date'], x['fts_type']): x for x in cur.fetchall()}
        conn.commit()
    finally:
        conn.close()

    out = {}
    for r in rows:
        ftype = FTS_STATUSES[r['attendance_status']]
        d = _as_date(r['absent_date'])
        ok_dates = {d}
        if ftype == 'OUT':
            overnight = crosses_midnight(r.get('shift_time'))
            if overnight or overnight is None:
                ok_dates.add(d + timedelta(days=1))
        best = None
        for f in filed.get((r['employee_id'], ftype), []):
            fd = _as_date(f['fts_date'])
            if fd not in ok_dates:
                continue
            # an unknown-shift next-day match only counts for a morning time-out
            if fd != d and crosses_midnight(r.get('shift_time')) is None:
                t = f['fts_time']
                if isinstance(t, timedelta) and t.total_seconds() > 12 * 3600:
                    continue
            score = (_RANK.get(f['status'] or 'Pending', 2), fd == d)
            if best is None or score > best[0]:
                best = (score, f)
        k = _key(r['employee_id'], d, ftype)
        rem = reminded.get(k)
        info = {'state': 'not_filed', 'status': None, 'fts_date': None, 'fts_time': None,
                'reminded_at': rem['last_sent'] if rem else None,
                'reminded_count': rem['n'] if rem else 0}
        if best:
            f = best[1]
            st = f['status'] or 'Pending'
            info.update(state='rejected' if st == 'Rejected' else 'filed', status=st,
                        fts_date=_as_date(f['fts_date']).isoformat(), fts_time=_fmt_time(f['fts_time']))
        out[k] = info
    return out


# ── Reminder emails ────────────────────────────────────────────────────────────

def _allowed():
    return bool(session.get('is_admin') or session.get('permissions', {}).get('can_absences'))


def _csrf_ok():
    from csrf import validate_csrf
    return validate_csrf()


def _parse_range(data):
    try:
        d_from = date.fromisoformat(str(data.get('date_from', '')))
        d_to = date.fromisoformat(str(data.get('date_to', '')))
    except ValueError:
        return None, None, 'Pick a valid date range.'
    if d_to < d_from:
        d_from, d_to = d_to, d_from
    if (d_to - d_from).days > MAX_RANGE_DAYS:
        return None, None, f'Pick a range of {MAX_RANGE_DAYS} days or less.'
    return d_from, d_to, None


def _pending_reminders(d_from, d_to):
    """Agents with unfiled or rejected FTS in the range, grouped per agent."""
    from app import get_absence_records, get_central_db
    records, _, _ = get_absence_records(d_from, d_to)
    fmap = fts_filing_map(records)
    agents = {}
    for r in records:
        ftype = FTS_STATUSES.get(r.get('attendance_status'))
        if not ftype:
            continue
        info = fmap.get(_key(r['employee_id'], r['absent_date'], ftype))
        if not info or info['state'] == 'filed':
            continue
        a = agents.setdefault(r['employee_id'], {
            'employee_id': r['employee_id'], 'name': r.get('schedule_name') or r['employee_id'],
            'tl': r.get('tl') or '', 'group': r.get('group_name') or '', 'email': '', 'items': [],
            'last_reminded': None})
        a['items'].append({
            'date': _as_date(r['absent_date']).isoformat(), 'type': ftype,
            'shift': r.get('shift_time') or '', 'state': info['state'],
            'time_in': str(r['time_in']) if r.get('time_in') else '',
            'time_out': str(r['time_out']) if r.get('time_out') else '',
            'reminded_at': info['reminded_at'].strftime('%b %d %H:%M') if info['reminded_at'] else ''})
        if info['reminded_at'] and (a['last_reminded'] is None or info['reminded_at'] > a['last_reminded']):
            a['last_reminded'] = info['reminded_at']
    if agents:
        conn = get_central_db()
        try:
            with conn.cursor() as cur:
                ids = list(agents)
                cur.execute(f"SELECT employee_id, email FROM gsheet_employees WHERE employee_id IN "
                            f"({','.join(['%s'] * len(ids))})", ids)
                for row in cur.fetchall():
                    if row['employee_id'] in agents:
                        agents[row['employee_id']]['email'] = (row['email'] or '').strip()
        finally:
            conn.close()
    out = sorted(agents.values(), key=lambda a: (a['group'], a['tl'], a['name']))
    for a in out:
        a['items'].sort(key=lambda i: i['date'])
        lr = a.pop('last_reminded')
        a['last_reminded'] = lr.strftime('%b %d %H:%M') if lr else ''
        a['reminded_recently'] = bool(lr and datetime.now() - lr < timedelta(hours=24))
    return out


def _email_html(agent):
    first = escape((agent['name'] or '').split(' ')[0] or 'there')
    rows = []
    for i in agent['items']:
        missing = 'Time-in' if i['type'] == 'IN' else 'Time-out'
        note = ('Your FTS for this was <b>rejected</b>. Please check with your approver and file it again.'
                if i['state'] == 'rejected' else 'No FTS filed yet')
        d = date.fromisoformat(i['date']).strftime('%a, %b %d, %Y')
        rows.append(f"""<tr>
          <td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;">{d}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;">{escape(i['shift'])}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;">{missing}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;">{note}</td></tr>""")
    return f"""<div style="font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:#1f2937;max-width:640px;">
  <p>Hi {first},</p>
  <p>Our biometric records show a missing time-in or time-out on the shift(s) below,
     and we don't see a Failure to Swipe (FTS) request for them yet.</p>
  <table style="border-collapse:collapse;width:100%;font-size:13px;">
    <thead><tr style="background:#f3f4f6;text-align:left;">
      <th style="padding:8px 12px;">Shift date</th><th style="padding:8px 12px;">Shift</th>
      <th style="padding:8px 12px;">Missing</th><th style="padding:8px 12px;">Note</th></tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
  <p style="margin-top:16px;">Please file an FTS for each one in the HR Portal:
     <a href="{PORTAL_FTS_URL}">File a Request &rsaquo; FTS</a>.
     For a shift that ends after midnight, use the date your time-out actually fell on.</p>
  <p><b>If you have already filed it, please disregard this email.</b></p>
  <p style="color:#6b7280;font-size:12px;">Sent from the HR Portal Absence Report.
     Reply to this email if you have questions.</p>
</div>"""


def _send(to, subject, html, reply_to):
    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = f"HR Portal <{os.getenv('SMTP_USER')}>"
    msg['To'] = to
    if reply_to:
        msg['Reply-To'] = reply_to
    msg.attach(MIMEText(html, 'html'))
    with smtplib.SMTP(os.getenv('SMTP_SERVER'), int(os.getenv('SMTP_PORT', 2525)), timeout=15) as smtp:
        smtp.login(os.getenv('SMTP_USER'), os.getenv('SMTP_PASSWORD'))
        smtp.sendmail(os.getenv('SMTP_USER'), [to], msg.as_string())


@fts_reminders_bp.route('/admin/absences/fts-reminders/preview', methods=['POST'])
def preview():
    if 'user' not in session or not _allowed():
        return jsonify(error='Access denied.'), 403
    if not _csrf_ok():
        return jsonify(error='Security check failed. Reload the page and try again.'), 400
    d_from, d_to, err = _parse_range(request.get_json(silent=True) or {})
    if err:
        return jsonify(error=err), 400
    return jsonify(agents=_pending_reminders(d_from, d_to),
                   date_from=d_from.isoformat(), date_to=d_to.isoformat())


@fts_reminders_bp.route('/admin/absences/fts-reminders/send', methods=['POST'])
def send():
    from app import app, get_central_db
    if 'user' not in session or not _allowed():
        return jsonify(error='Access denied.'), 403
    if not _csrf_ok():
        return jsonify(error='Security check failed. Reload the page and try again.'), 400
    data = request.get_json(silent=True) or {}
    d_from, d_to, err = _parse_range(data)
    if err:
        return jsonify(error=err), 400
    wanted = {str(x) for x in (data.get('employee_ids') or [])}
    if not wanted:
        return jsonify(error='Tick at least one agent.'), 400

    sender = session['user'].get('email', '')
    results = []
    for a in _pending_reminders(d_from, d_to):      # re-computed: only still-unfiled items go out
        if a['employee_id'] not in wanted:
            continue
        if not a['email']:
            results.append({'employee_id': a['employee_id'], 'name': a['name'], 'ok': False,
                            'error': 'No email in the employee sheet'})
            continue
        n = len(a['items'])
        subject = f"Action needed: file your FTS ({n} missing time {'record' if n == 1 else 'records'})"
        try:
            _send(a['email'], subject, _email_html(a), sender)
        except Exception as e:
            app.logger.error(f"FTS reminder to {a['email']} failed: {e}")
            results.append({'employee_id': a['employee_id'], 'name': a['name'], 'ok': False,
                            'error': 'Email could not be sent'})
            continue
        conn = get_central_db()
        try:
            with conn.cursor() as cur:
                _ensure_log_table(cur)
                cur.executemany(
                    "INSERT INTO fts_reminder_log (employee_id, shift_date, fts_type, email, sent_by) "
                    "VALUES (%s,%s,%s,%s,%s)",
                    [(a['employee_id'], i['date'], i['type'], a['email'], sender) for i in a['items']])
            conn.commit()
        finally:
            conn.close()
        app.logger.info(f"FTS reminder sent to {a['email']} ({n} item(s)) by {sender}")
        results.append({'employee_id': a['employee_id'], 'name': a['name'], 'ok': True, 'email': a['email']})
    skipped = len(wanted) - len(results)
    return jsonify(results=results, sent=sum(1 for r in results if r['ok']),
                   failed=sum(1 for r in results if not r['ok']), already_filed=skipped)
