# incident_reports.py
# Flask Blueprint — migrated from PHP incident_report module
# Place at: /var/www/html/leavesystem/incident_reports.py

from flask import Blueprint, render_template, request, jsonify, session, redirect, url_for
from csrf import validate_csrf
from datetime import datetime
from functools import wraps
import os, uuid, smtplib, logging
from dotenv import load_dotenv
load_dotenv()
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

ir_bp = Blueprint('incident_reports', __name__)

# ─── Constants ────────────────────────────────────────────────────────────────
HR_EMAILS  = [e.strip() for e in os.getenv('IR_HR_EMAILS', '').split(',') if e.strip()]
SGA_EMAILS = [e.strip() for e in os.getenv('IR_SGA_EMAILS', '').split(',') if e.strip()]
IR_EMAIL_TO = ['managers@cohere.ph', 'jovin.lumapat@cohere.ph']
IR_EMAIL_BCC = 'andrewvincentt@gmail.com'

UPLOAD_DIR  = '/var/www/html/cohere_dashboard/incident_report/uploads'
ALLOWED_EXT = {'jpg', 'jpeg', 'png', 'gif'}

SMTP_HOST      = 'cohere.ph'
SMTP_PORT      = 2525
SMTP_USER      = 'send_email@cohere.ph'
SMTP_PASS      = '***REMOVED***'
SMTP_FROM      = 'wfm@cohere.ph'
SMTP_FROM_NAME = 'Incident Report System'

# ─── DB ───────────────────────────────────────────────────────────────────────
def get_db():
    """Reuse the portal's central_db connection (DictCursor)."""
    from app import get_central_db
    return get_central_db()

# ─── Auth ─────────────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user' not in session:
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated

def ir_user():
    """Return a flat dict of current user info from Flask session."""
    from app import IR_ALL_ACCESS
    email = session['user']['email']
    # Check if TL via supervisor_mapping
    is_tl_sup = False
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) as cnt FROM supervisor_mapping WHERE supervisor_email=%s",
                        (email,))
            is_tl_sup = (cur.fetchone()['cnt'] or 0) > 0
        conn.close()
    except Exception:
        pass
    return {
        'email':         email,
        'employee_id':   session['user']['employee_id'],
        'name':          session['user']['name'],
        'is_admin':      session.get('is_admin', False) or email in IR_ALL_ACCESS,
        'is_supervisor': session.get('is_supervisor', False) or is_tl_sup,
        'is_hr':         email in HR_EMAILS,
        'is_sga':        email in SGA_EMAILS,
    }

def generate_report_number():
    return 'IR-' + datetime.now().strftime('%Y%m%d') + '-' + str(uuid.uuid4())[:6].upper()

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXT

def save_upload(file, report_number, prefix=''):
    """Save uploaded file, return filename or None."""
    if not file or not file.filename or not allowed_file(file.filename):
        return None
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    ext = file.filename.rsplit('.', 1)[1].lower()
    filename = f"{report_number}_{prefix}_{int(datetime.now().timestamp())}.{ext}"
    file.save(os.path.join(UPLOAD_DIR, filename))
    return filename

def get_personid(employee_id, conn):
    """Look up userdata.personid for edit-history tracking (backward compat)."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT personid FROM userdata WHERE companyid = %s", (employee_id,))
            row = cur.fetchone()
            return row['personid'] if row else None
    except Exception:
        return None

def get_supervisor_email(employee_id, conn):
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT sm.supervisor_email
                FROM supervisor_mapping sm
                INNER JOIN gsheet_employees g ON sm.agent_email COLLATE utf8mb4_unicode_ci = g.email
                WHERE g.employee_id = %s
                LIMIT 1
            """, (employee_id,))
            row = cur.fetchone()
            return row['supervisor_email'] if row else None
    except Exception as e:
        print(f"[IR] get_supervisor_email failed for {employee_id}: {e}", flush=True)
        return None

def get_group_emails(employee_id, conn):
    """All active group members of the given employee (excluding TL group)."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT g2.email
                FROM gsheet_employees g1
                INNER JOIN gsheet_employees g2 ON g1.group_name = g2.group_name
                WHERE g1.employee_id = %s
                  AND g1.group_name IS NOT NULL
                  AND g1.group_name != ''
                  AND g1.group_name != 'TL'
                  AND g2.status = 'Active'
                  AND g2.email IS NOT NULL
            """, (employee_id,))
            return [r['email'] for r in cur.fetchall()]
    except Exception:
        return []

# ─── Email ────────────────────────────────────────────────────────────────────
def _send(to_list, subject, html_body, bcc_list=None, thread_id=None):
    """Send email using portal SMTP with IR-specific From name."""
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    all_rcpt = list(filter(None, set(to_list) | set(bcc_list or [])))
    print(f"[IR EMAIL] to={all_rcpt} subj={subject[:50]}", flush=True)
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From']    = f"{os.getenv('IR_FROM_NAME','Incident Report System')} <{os.getenv('SMTP_USER')}>"
        msg['To']      = ', '.join(filter(None, set(to_list)))
        if thread_id:
            mid = f"<incident-{thread_id}@dashboard.cohere.ph>"
            msg['In-Reply-To'] = mid
            msg['References']  = mid
        msg.attach(MIMEText(html_body, 'html'))
        with smtplib.SMTP(os.getenv('SMTP_SERVER'), int(os.getenv('SMTP_PORT', 2525))) as s:
            s.login(os.getenv('SMTP_USER'), os.getenv('SMTP_PASSWORD'))
            s.sendmail(os.getenv('SMTP_USER'), all_rcpt, msg.as_string())
        print(f"[IR EMAIL OK] sent to {all_rcpt}", flush=True)
        return True
    except Exception as e:
        print(f"[IR EMAIL ERROR] {e}", flush=True)
        return False

def _ir_email_body(title, subtitle, fields, action_url, action_label='📊 View Report', alert=None):
    """Generic branded email body matching portal theme."""
    field_html = ''
    for label, value in fields:
        field_html += f"""<tr><td style="padding:10px 0;border-bottom:1px solid #e2e8f4;vertical-align:top;">
            <div style="font-size:11px;font-weight:700;color:#6b7a99;text-transform:uppercase;letter-spacing:.5px;margin-bottom:3px;">{label}</div>
            <div style="font-size:14px;color:#1a2440;">{value}</div></td></tr>"""
    alert_html = f"""<div style="background:#fef3c7;border-left:3px solid #f59e0b;border-radius:6px;
        padding:12px 14px;margin-bottom:18px;font-size:13px;color:#92400e;">
        <strong>⚠️ Action Required:</strong> {alert}</div>""" if alert else ''
    return f"""<html><body style="font-family:'Segoe UI',Arial,sans-serif;background:#f0f2f8;margin:0;padding:20px;">
    <div style="max-width:580px;margin:0 auto;">
      <div style="background:#0f2744;border-radius:12px 12px 0 0;padding:28px 32px;text-align:center;">
        <div style="font-size:12px;font-weight:600;color:#93c5fd;letter-spacing:1px;text-transform:uppercase;margin-bottom:6px;">Incident Report System</div>
        <h1 style="margin:0;font-size:22px;font-weight:700;color:#fff;">{title}</h1>
        <div style="margin-top:8px;font-size:13px;color:#94a3b8;">{subtitle}</div>
      </div>
      <div style="background:#fff;padding:28px 32px;border:1px solid #e2e8f4;">
        {alert_html}
        <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">{field_html}</table>
        <div style="text-align:center;margin-top:28px;">
          <a href="{action_url}" style="display:inline-block;background:#1e5fd4;color:#fff;padding:13px 32px;
             text-decoration:none;border-radius:8px;font-weight:700;font-size:14px;">{action_label}</a>
        </div>
      </div>
      <div style="background:#f0f2f8;border-radius:0 0 12px 12px;padding:14px 32px;text-align:center;
                  font-size:11px;color:#6b7a99;border:1px solid #e2e8f4;border-top:none;">
        Cohere HR Portal · Incident Report System
      </div>
    </div></body></html>"""

def send_incident_email(report_number, incident_date, agent_eid, employee_name,
                        submitter_eid, summary, attachment_count):
    conn = get_db()
    try:
        recipients = list(IR_EMAIL_TO)
        sup = get_supervisor_email(agent_eid, conn)
        if sup: recipients.append(sup)
        recipients += get_group_emails(submitter_eid, conn)

        body = _ir_email_body(
            title='New Incident Report Submitted',
            subtitle=f'Report #{report_number}',
            fields=[
                ('Date of Incident', datetime.strptime(str(incident_date), '%Y-%m-%d').strftime('%B %d, %Y')),
                ('Agent Involved', employee_name),
                ('Summary', summary.replace('\n', '<br>')),
                ('Attachments', 'None' if not attachment_count else f'{attachment_count} file(s)'),
            ],
            action_url=f"https://hrportal.cohere.ph/incident-reports/{report_number}",
            alert='This incident requires your attention.'
        )
        _send(recipients, f"New Incident Report: {report_number}", body, bcc_list=[IR_EMAIL_BCC])
    finally:
        try:
            conn.close()
        except Exception:
            pass

def send_hr_notification(report_number, commenter_name, comment, agent_eid, status_action):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT ir.*, g.group_name FROM incident_reports ir
                LEFT JOIN gsheet_employees g ON ir.employee_id = g.employee_id
                WHERE ir.report_number = %s
            """, (report_number,))
            inc = cur.fetchone()
        if not inc:
            return

        if status_action == 'rwe_request':
            status_text = 'RWE REQUESTED — HR ACTION REQUIRED'
            bg_color = '#ff6b35' # Orange alert
        elif status_action == 'rwe_served':
            status_text = 'RWE SERVED — AWAITING AGENT EXPLANATION'
            bg_color = '#17a2b8' # Teal info banner
        else:
            status_text = 'PENDING HR — ATTENTION REQUIRED'
            bg_color = '#ffc107' # Yellow warning

        body = f"""<html><body style="font-family:'Segoe UI',Arial,sans-serif;background:#f0f2f8;margin:0;padding:20px;">
    <div style="max-width:580px;margin:0 auto;">
      <div style="background:#0f2744;border-radius:12px 12px 0 0;padding:24px 32px;text-align:center;">
        <div style="font-size:12px;font-weight:600;color:#93c5fd;letter-spacing:1px;text-transform:uppercase;margin-bottom:6px;">HR Escalation</div>
        <h1 style="margin:0;font-size:20px;font-weight:700;color:#fff;">📋 Incident Report Action</h1>
        <div style="margin-top:6px;font-size:13px;color:#94a3b8;">Report #{report_number}</div>
      </div>
      <div style="background:#fff;padding:24px 32px;border:1px solid #e2e8f4;">
        <div style="display:inline-block;background:{bg_color};color:white;padding:6px 16px;
                    border-radius:20px;font-weight:700;font-size:12px;margin-bottom:18px;">
          {status_text}
        </div>
        <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;margin-bottom:16px;">
          <tr><td style="padding:8px 0;border-bottom:1px solid #e2e8f4;">
            <div style="font-size:11px;font-weight:700;color:#6b7a99;text-transform:uppercase;">Date</div>
            <div style="font-size:14px;color:#1a2440;">{inc['incident_date'].strftime('%B %d, %Y') if hasattr(inc['incident_date'], 'strftime') else str(inc['incident_date'])}</div>
          </td></tr>
          <tr><td style="padding:8px 0;border-bottom:1px solid #e2e8f4;">
            <div style="font-size:11px;font-weight:700;color:#6b7a99;text-transform:uppercase;">Agent</div>
            <div style="font-size:14px;color:#1a2440;">{inc['employee_name']} ({inc['employee_id']})</div>
          </td></tr>
          <tr><td style="padding:8px 0;border-bottom:1px solid #e2e8f4;">
            <div style="font-size:11px;font-weight:700;color:#6b7a99;text-transform:uppercase;">Group</div>
            <div style="font-size:14px;color:#1a2440;">{inc.get('group_name') or 'N/A'}</div>
          </td></tr>
          <tr><td style="padding:8px 0;">
            <div style="font-size:11px;font-weight:700;color:#6b7a99;text-transform:uppercase;">Summary</div>
            <div style="font-size:14px;color:#1a2440;">{inc['summary']}</div>
          </td></tr>
        </table>
        <div style="font-size:12px;font-weight:700;color:#6b7a99;text-transform:uppercase;margin-bottom:6px;">
          Comment from {commenter_name}
        </div>
        <div style="background:#f8faff;border:1px solid #e2e8f4;border-radius:8px;padding:14px;
                    font-size:14px;color:#1a2440;line-height:1.7;margin-bottom:20px;">
          {comment.replace(chr(10), '<br>')}
        </div>
        <div style="text-align:center;">
          <a href="https://hrportal.cohere.ph/incident-reports/{report_number}"
             style="display:inline-block;background:#1e5fd4;color:#fff;padding:12px 28px;
                    text-decoration:none;border-radius:8px;font-weight:700;font-size:14px;">
            View Full Report
          </a>
        </div>
      </div>
      <div style="background:#f0f2f8;border-radius:0 0 12px 12px;padding:12px 32px;text-align:center;
                  font-size:11px;color:#6b7a99;border:1px solid #e2e8f4;border-top:none;">
        Cohere HR Portal · Incident Report System
      </div>
    </div></body></html>"""
        _send(HR_EMAILS, f"Re: New Incident Report: {report_number} — HR ACTION REQUIRED",
              body, thread_id=report_number)
    finally:
        try:
            conn.close()
        except Exception:
            pass

def send_rwe_served_to_tl(report_number, served_by, comment, agent_eid):
    """Notify the TL/supervisor that HR has served the RWE for their agent."""
    conn = get_db()
    try:
        # Get report details
        with conn.cursor() as cur:
            cur.execute("""SELECT ir.*, g.group_name FROM incident_reports ir
                           LEFT JOIN gsheet_employees g ON ir.employee_id = g.employee_id
                           WHERE ir.report_number = %s""", (report_number,))
            inc = cur.fetchone()
        if not inc:
            return

        # Get supervisor of the agent
        sup_email = get_supervisor_email(agent_eid, conn)
        if not sup_email:
            return

        recipients = list(IR_EMAIL_TO) + [sup_email]

        body = f"""
        <html><body style="font-family:Arial,sans-serif;">
        <div style="max-width:600px;margin:0 auto;">
            <div style="background:linear-gradient(135deg,#10b981,#059669);color:white;padding:25px;
                        text-align:center;border-radius:10px 10px 0 0;">
                <h2 style="margin:0;">✅ RWE Served</h2>
                <p style="margin:8px 0 0;">Report #{report_number}</p>
            </div>
            <div style="padding:25px;background:#f9f9f9;">
                <div style="background:#d1fae5;border:1px solid #6ee7b7;border-radius:8px;
                            padding:14px 16px;margin-bottom:20px;color:#065f46;font-weight:600;">
                    ✅ HR has marked the Written Explanation as served for this incident.
                </div>
                <div style="background:white;padding:15px;border-radius:6px;margin-bottom:15px;">
                    <p><strong>Agent:</strong> {inc['employee_name']} ({inc['employee_id']})</p>
                    <p><strong>Group:</strong> {inc.get('group_name') or 'N/A'}</p>
                    <p><strong>Incident Date:</strong> {inc['incident_date'].strftime('%B %d, %Y') if hasattr(inc.get('incident_date'), 'strftime') else str(inc.get('incident_date',''))}</p>
                    <p><strong>Summary:</strong> {inc['summary']}</p>
                </div>
                <p style="font-weight:600;color:#374151;">Note from {served_by}:</p>
                <div style="background:#f0fdf4;border-left:4px solid #10b981;padding:16px;
                            border-radius:4px;margin-bottom:20px;">
                    {comment.replace(chr(10),'<br>')}
                </div>
                <div style="text-align:center;">
                    <a href="https://hrportal.cohere.ph/incident-reports/{report_number}"
                       style="background:linear-gradient(135deg,#10b981,#059669);color:white;
                              padding:12px 28px;text-decoration:none;border-radius:8px;font-weight:700;">
                        📋 View Full Report
                    </a>
                </div>
            </div>
            <div style="background:#333;color:white;padding:12px;text-align:center;
                        font-size:12px;border-radius:0 0 10px 10px;">⚡ Incident Report System</div>
        </div></body></html>"""

        _send(recipients,
              f"RWE Served: {report_number} — {inc['employee_name']}",
              body, thread_id=report_number)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def send_comment_notification(report_number, commenter_name, comment,
                               agent_eid, submitter_eid, status_action):
    conn = get_db()
    try:
        recipients = list(IR_EMAIL_TO)
        sup = get_supervisor_email(agent_eid, conn)
        if sup: recipients.append(sup)
        recipients += get_group_emails(submitter_eid, conn)

        status_colors = {
            'rwe_request': ('#fee2e2','#991b1b','⚠️ RWE Requested'),
            'rwe_served':  ('#d1fae5','#065f46','✅ RWE Served'),
            'resolved':    ('#d1fae5','#065f46','✅ Resolved'),
            'reviewed':    ('#dbeafe','#1e40af','👁 Reviewed'),
        }
        status_badge = ''
        if status_action and status_action in status_colors:
            bg, fg, label = status_colors[status_action]
            status_badge = f"""<span style="background:{bg};color:{fg};padding:4px 12px;
                border-radius:20px;font-size:11px;font-weight:700;margin-left:8px;">{label}</span>"""

        body = f"""<html><body style="font-family:'Segoe UI',Arial,sans-serif;background:#f0f2f8;margin:0;padding:20px;">
    <div style="max-width:580px;margin:0 auto;">
      <div style="background:#0f2744;border-radius:12px 12px 0 0;padding:24px 32px;text-align:center;">
        <div style="font-size:12px;font-weight:600;color:#93c5fd;letter-spacing:1px;text-transform:uppercase;margin-bottom:6px;">Incident Report System</div>
        <h1 style="margin:0;font-size:20px;font-weight:700;color:#fff;">New Comment</h1>
        <div style="margin-top:6px;font-size:13px;color:#94a3b8;">Report #{report_number}</div>
      </div>
      <div style="background:#fff;padding:24px 32px;border:1px solid #e2e8f4;">
        <div style="margin-bottom:16px;">
          <span style="background:#1e5fd4;color:white;padding:4px 12px;border-radius:20px;font-size:11px;font-weight:700;">NEW COMMENT</span>
          {status_badge}
        </div>
        <div style="border-left:3px solid #1e5fd4;padding:12px 16px;margin-bottom:16px;background:#f8faff;border-radius:0 6px 6px 0;">
          <div style="font-weight:700;color:#1a2440;font-size:14px;">{commenter_name}</div>
          <div style="color:#6b7a99;font-size:12px;margin-top:2px;">{datetime.now().strftime('%b %d, %Y at %I:%M %p')}</div>
        </div>
        <div style="background:#f8faff;border:1px solid #e2e8f4;border-radius:8px;padding:16px;
                    font-size:14px;color:#1a2440;line-height:1.7;margin-bottom:20px;">
          {comment.replace(chr(10),'<br>')}
        </div>
        <div style="text-align:center;">
          <a href="https://hrportal.cohere.ph/incident-reports/{report_number}"
             style="display:inline-block;background:#1e5fd4;color:#fff;padding:12px 28px;
                    text-decoration:none;border-radius:8px;font-weight:700;font-size:14px;">
            View Report
          </a>
        </div>
      </div>
      <div style="background:#f0f2f8;border-radius:0 0 12px 12px;padding:12px 32px;text-align:center;
                  font-size:11px;color:#6b7a99;border:1px solid #e2e8f4;border-top:none;">
        Cohere HR Portal · Incident Report System
      </div>
    </div></body></html>"""

        _send(recipients, f"Re: New Incident Report: {report_number}", body,
              bcc_list=[IR_EMAIL_BCC], thread_id=report_number)
    finally:
        try:
            conn.close()
        except Exception:
            pass

# ─── Routes ───────────────────────────────────────────────────────────────────

@ir_bp.route('/')
@login_required
def dashboard():
    u = ir_user()
    conn = get_db()
    try:
        status_filter = request.args.get('status', 'all')
        search        = request.args.get('search', '')
        start_date    = request.args.get('start_date', '')
        end_date      = request.args.get('end_date', '')
        sort_by       = request.args.get('sort', 'updated')

        conds, params = ["1=1"], []
        # Check if TL supervisor via supervisor_mapping
        _is_tl_sup = False
        if not u['is_admin'] and not u['is_supervisor']:
            with conn.cursor() as _cur:
                _cur.execute("SELECT COUNT(*) as cnt FROM supervisor_mapping WHERE supervisor_email = %s",
                             (u['email'],))
                _is_tl_sup = (_cur.fetchone()['cnt'] or 0) > 0

        if not u['is_admin'] and (u['is_supervisor'] or _is_tl_sup):
            conds.append("""ir.employee_id COLLATE utf8mb4_unicode_ci IN (
                SELECT g.employee_id FROM supervisor_mapping sm
                INNER JOIN gsheet_employees g ON sm.agent_email COLLATE utf8mb4_unicode_ci = g.email
                WHERE sm.supervisor_email COLLATE utf8mb4_unicode_ci = %s)""")
            params.append(u['email'])

        elif not u['is_admin'] and not u['is_supervisor'] and not _is_tl_sup:
            user_group = None
            if not u['is_sga']:
                with conn.cursor() as cur:
                    cur.execute("""SELECT group_name FROM gsheet_employees
                                   WHERE email=%s AND group_name IS NOT NULL
                                   AND group_name != 'TL' AND status='Active' LIMIT 1""",
                                (u['email'],))
                    row = cur.fetchone()
                    user_group = row['group_name'] if row else None

            if user_group and not u['is_hr'] and not u['is_sga']:
                conds.append("""ir.submitted_by_id IN (
                    SELECT employee_id FROM gsheet_employees
                    WHERE group_name=%s AND status='Active')""")
                params.append(user_group)
            elif u['is_hr']:
                conds.append("ir.status IN ('rwe_request','rwe_served')")
            # SGA sees all reports (no filter)

        if status_filter != 'all':
            conds.append("ir.status = %s"); params.append(status_filter)
        if search:
            conds.append("(ir.report_number LIKE %s OR ir.employee_name LIKE %s OR ir.summary LIKE %s)")
            params += [f'%{search}%', f'%{search}%', f'%{search}%']
        if start_date:
            conds.append("DATE(ir.rwe_requested_at) >= %s"); params.append(start_date)
        if end_date:
            conds.append("DATE(ir.rwe_requested_at) <= %s"); params.append(end_date)

        order = {'incident': 'ir.incident_date DESC, ir.created_at DESC',
                 'created':  'ir.created_at DESC',
                 'updated':  'ir.updated_at DESC, ir.created_at DESC'}.get(sort_by, 'ir.updated_at DESC')

        where = ' AND '.join(conds)
        with conn.cursor() as cur:
            cur.execute(f"SELECT ir.* FROM incident_reports ir WHERE {where} ORDER BY {order}", params)
            reports = cur.fetchall()

        # Stats (admin sees all, supervisor filtered)
        s_conds, s_params = ["1=1"], []
        if not u['is_admin'] and (u['is_supervisor'] or _is_tl_sup):
            s_conds.append("""ir.employee_id COLLATE utf8mb4_unicode_ci IN (
                SELECT g.employee_id FROM supervisor_mapping sm
                INNER JOIN gsheet_employees g ON sm.agent_email COLLATE utf8mb4_unicode_ci = g.email
                WHERE sm.supervisor_email COLLATE utf8mb4_unicode_ci = %s)""")
            s_params.append(u['email'])
        with conn.cursor() as cur:
            cur.execute(f"""SELECT COUNT(*) as total,
                SUM(CASE WHEN ir.status='pending'     THEN 1 ELSE 0 END) as pending,
                SUM(CASE WHEN ir.status='reviewed'    THEN 1 ELSE 0 END) as reviewed,
                SUM(CASE WHEN ir.status='resolved'    THEN 1 ELSE 0 END) as resolved,
                SUM(CASE WHEN ir.status='rwe_request'  THEN 1 ELSE 0 END) as rwe_request,
                SUM(CASE WHEN ir.status='rwe_for_signature' THEN 1 ELSE 0 END) as rwe_for_signature,
                SUM(CASE WHEN ir.status='rwe_for_service'   THEN 1 ELSE 0 END) as rwe_for_service,
                SUM(CASE WHEN ir.status='awaiting_explanation' THEN 1 ELSE 0 END) as awaiting_explanation,
                SUM(CASE WHEN ir.status='forwarded'         THEN 1 ELSE 0 END) as forwarded,
                SUM(CASE WHEN ir.status='for_memo'          THEN 1 ELSE 0 END) as for_memo,
                SUM(CASE WHEN ir.status='rwe_served'   THEN 1 ELSE 0 END) as rwe_served,
                SUM(CASE WHEN ir.status='waived'        THEN 1 ELSE 0 END) as waived
                FROM incident_reports ir WHERE {' AND '.join(s_conds)}""", s_params)
            stats = cur.fetchone()

            # --- INSERT THE NEW SLA BLOCK HERE ---
            cur.execute("""
                SELECT COUNT(*) as sla_breaches 
                FROM incident_reports 
                WHERE status IN ('rwe_request', 'rwe_served')
                  AND DATEDIFF(CURDATE(), created_at) > 5
                  AND MONTH(created_at) = MONTH(CURDATE())
                  AND YEAR(created_at) = YEAR(CURDATE())
            """)
            breach_data = cur.fetchone()
            
            # Safely merge it into your main stats object
            if stats:
                stats['sla_breaches'] = breach_data['sla_breaches'] if breach_data else 0
            # -------------------------------------
        rwe_reports = []
        if u['is_admin'] or u['is_hr'] or u['is_sga'] or u['is_supervisor']:
            # Full view for admin/HR/SGA; TL/SOM scoped to their own agents.
            full_view = u['is_admin'] or u['is_hr'] or u['is_sga']
            scope_sql = ""
            scope_params = []
            if not full_view:
                scope_sql = """
                    AND employee_id COLLATE utf8mb4_unicode_ci IN (
                        SELECT g.employee_id
                        FROM supervisor_mapping sm
                        INNER JOIN gsheet_employees g
                            ON sm.agent_email COLLATE utf8mb4_unicode_ci = g.email
                        WHERE sm.supervisor_email = %s
                    )
                """
                scope_params.append(u['email'])
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT incident_reports.*,
                        CASE status
                          WHEN 'rwe_request'       THEN DATEDIFF(NOW(), COALESCE(rwe_requested_at, updated_at))
                          WHEN 'rwe_for_signature' THEN DATEDIFF(NOW(), COALESCE(rwe_for_signature_at, updated_at))
                          WHEN 'rwe_for_service'   THEN DATEDIFF(NOW(), COALESCE(rwe_for_service_at, updated_at))
                          WHEN 'awaiting_explanation' THEN DATEDIFF(NOW(), COALESCE(awaiting_explanation_at, updated_at))
                          WHEN 'forwarded'         THEN DATEDIFF(NOW(), COALESCE(forwarded_at, updated_at))
                          WHEN 'for_memo'          THEN DATEDIFF(NOW(), COALESCE(for_memo_at, updated_at))
                        END as days_in_stage,
                        (SELECT tl_g.schedule_name
                           FROM supervisor_mapping sm2
                           INNER JOIN gsheet_employees ag
                               ON sm2.agent_email COLLATE utf8mb4_unicode_ci = ag.email
                           INNER JOIN gsheet_employees tl_g
                               ON sm2.supervisor_email COLLATE utf8mb4_unicode_ci = tl_g.email
                           WHERE ag.employee_id COLLATE utf8mb4_unicode_ci = incident_reports.employee_id COLLATE utf8mb4_unicode_ci
                           LIMIT 1) as responsible_tl
                    FROM incident_reports
                    WHERE status IN ('rwe_request','rwe_for_signature','rwe_for_service','awaiting_explanation','forwarded','for_memo')
                    {scope_sql}
                    ORDER BY FIELD(status,'rwe_request','rwe_for_signature','rwe_for_service','awaiting_explanation','forwarded','for_memo'),
                             updated_at ASC
                """, scope_params)
                rwe_reports = cur.fetchall()

        # RWE Report data (admin only)
        rwe_report_data = []
        if u['is_admin']:
            with conn.cursor() as cur:
                hr_emails = HR_EMAILS + SGA_EMAILS
                hr_emails_sql = ','.join([f"'{e}'" for e in set(hr_emails)])
                
                # Build date filter conditions
                rwe_conds = ["ir.rwe_requested_at IS NOT NULL"]
                rwe_params = []
                
                if start_date:
                    rwe_conds.append("DATE(ir.rwe_requested_at) >= %s")
                    rwe_params.append(start_date)
                if end_date:
                    rwe_conds.append("DATE(ir.rwe_requested_at) <= %s")
                    rwe_params.append(end_date)
                
                rwe_where = ' AND '.join(rwe_conds)
                
                cur.execute(f"""
                    SELECT ir.report_number, ir.employee_name, ir.submitted_by_name,
                           ir.status, ir.rwe_requested_at, ir.rwe_for_signature_at,
                           ir.rwe_for_service_at, ir.rwe_served_at,
                           ir.forwarded_at, ir.for_memo_at, ir.waived_at, ir.resolved_at,
                           MIN(ic.created_at) as first_hr_reply,
                           /* legacy = pre-workflow ticket OR clock-reset migration ticket */
                           CASE WHEN (ir.rwe_for_signature_at IS NULL
                                       AND ir.rwe_for_service_at IS NULL
                                       AND ir.status != 'rwe_request')
                                  OR (ir.sla_baseline_at IS NOT NULL
                                       AND ir.rwe_requested_at IS NOT NULL
                                       AND ABS(TIMESTAMPDIFF(MINUTE, ir.sla_baseline_at, ir.rwe_requested_at)) > 1)
                                  OR (ir.sla_baseline_at IS NOT NULL AND ir.rwe_requested_at IS NULL)
                                THEN 1 ELSE 0 END as legacy,
                           /* Stage 1: HR creates doc (requested -> for_signature) */
                           CASE WHEN ir.rwe_for_signature_at IS NOT NULL
                                THEN DATEDIFF(ir.rwe_for_signature_at, ir.rwe_requested_at)
                                WHEN ir.status = 'rwe_request'
                                THEN DATEDIFF(NOW(), ir.rwe_requested_at)
                           END as doc_days,
                           /* Stage 2: TL signs (for_signature -> for_service) */
                           CASE WHEN ir.rwe_for_service_at IS NOT NULL AND ir.rwe_for_signature_at IS NOT NULL
                                THEN DATEDIFF(ir.rwe_for_service_at, ir.rwe_for_signature_at)
                                WHEN ir.status = 'rwe_for_signature'
                                THEN DATEDIFF(NOW(), ir.rwe_for_signature_at)
                           END as sign_days,
                           /* Stage 3: HR serves (for_service -> served) */
                           CASE WHEN ir.rwe_served_at IS NOT NULL AND ir.rwe_for_service_at IS NOT NULL
                                THEN DATEDIFF(ir.rwe_served_at, ir.rwe_for_service_at)
                                WHEN ir.status = 'rwe_for_service'
                                THEN DATEDIFF(NOW(), ir.rwe_for_service_at)
                           END as serve_days,
                           /* Stage 4: SOM/TL decision (forwarded -> for_memo OR waived) */
                           CASE WHEN ir.for_memo_at IS NOT NULL AND ir.forwarded_at IS NOT NULL
                                THEN DATEDIFF(ir.for_memo_at, ir.forwarded_at)
                                WHEN ir.waived_at IS NOT NULL AND ir.forwarded_at IS NOT NULL
                                THEN DATEDIFF(ir.waived_at, ir.forwarded_at)
                                WHEN ir.status = 'forwarded'
                                THEN DATEDIFF(NOW(), ir.forwarded_at)
                           END as decision_days,
                           /* Stage 5: HR memo (for_memo -> resolved) */
                           CASE WHEN ir.resolved_at IS NOT NULL AND ir.for_memo_at IS NOT NULL
                                THEN DATEDIFF(ir.resolved_at, ir.for_memo_at)
                                WHEN ir.status = 'for_memo'
                                THEN DATEDIFF(NOW(), ir.for_memo_at)
                           END as memo_days,
                           /* Total: to resolved/waived if present, else served/first-reply, else live.
                              Counts from sla_baseline_at (fair reset) falling back to rwe_requested_at. */
                           GREATEST(0, DATEDIFF(
                               COALESCE(ir.resolved_at, ir.waived_at, NOW()),
                               COALESCE(ir.sla_baseline_at, ir.rwe_requested_at)
                           )) as days_open,
                           CASE
                             WHEN (ir.sla_baseline_at IS NOT NULL
                                   AND ir.rwe_requested_at IS NOT NULL
                                   AND ABS(TIMESTAMPDIFF(MINUTE, ir.sla_baseline_at, ir.rwe_requested_at)) > 1)
                                  THEN 'migrated'
                             WHEN ir.rwe_served_at IS NOT NULL THEN 'served'
                             WHEN ir.rwe_for_signature_at IS NULL
                                  AND ir.rwe_for_service_at IS NULL
                                  AND ir.status != 'rwe_request'
                                  AND MIN(ic.created_at) IS NOT NULL THEN 'served'
                             WHEN ir.status = 'rwe_request'
                                  AND DATEDIFF(NOW(), ir.rwe_requested_at) > 5 THEN 'overdue'
                             WHEN ir.status = 'rwe_for_signature'
                                  AND DATEDIFF(NOW(), ir.rwe_for_signature_at) > 5 THEN 'overdue'
                             WHEN ir.status = 'rwe_for_service'
                                  AND DATEDIFF(NOW(), ir.rwe_for_service_at) > 5 THEN 'overdue'
                             WHEN ir.status = 'rwe_request'
                                  AND DATEDIFF(NOW(), ir.rwe_requested_at) >= 4 THEN 'warning'
                             WHEN ir.status = 'rwe_for_signature'
                                  AND DATEDIFF(NOW(), ir.rwe_for_signature_at) >= 4 THEN 'warning'
                             WHEN ir.status = 'rwe_for_service'
                                  AND DATEDIFF(NOW(), ir.rwe_for_service_at) >= 4 THEN 'warning'
                             WHEN ir.status = 'forwarded'
                                  AND DATEDIFF(NOW(), ir.forwarded_at) > 5 THEN 'overdue'
                             WHEN ir.status = 'for_memo'
                                  AND DATEDIFF(NOW(), ir.for_memo_at) > 5 THEN 'overdue'
                             WHEN ir.status = 'forwarded'
                                  AND DATEDIFF(NOW(), ir.forwarded_at) >= 4 THEN 'warning'
                             WHEN ir.status = 'for_memo'
                                  AND DATEDIFF(NOW(), ir.for_memo_at) >= 4 THEN 'warning'
                             ELSE 'ok'
                           END as sla_status
                    FROM incident_reports ir
                    LEFT JOIN incident_comments ic ON ir.id = ic.report_id
                        AND ic.employee_id COLLATE utf8mb4_unicode_ci IN (
                            SELECT employee_id FROM gsheet_employees
                            WHERE email IN ({hr_emails_sql})
                        )
                        AND ic.created_at >= ir.rwe_requested_at
                    WHERE {rwe_where}
                    GROUP BY ir.id
                    ORDER BY ir.rwe_requested_at DESC
                """, rwe_params)
                rwe_report_data = cur.fetchall()

        # IR Timeline data (admin only)
        ir_timeline_data = []
        if u['is_admin']:
            tl_conds = ["1=1"]
            tl_params = []
            if status_filter != 'all':
                tl_conds.append("ir.status = %s")
                tl_params.append(status_filter)
            if search:
                tl_conds.append("(ir.report_number LIKE %s OR ir.employee_name LIKE %s)")
                tl_params += [f'%{search}%', f'%{search}%']
            if start_date:
                tl_conds.append("ir.incident_date >= %s")
                tl_params.append(start_date)
            if end_date:
                tl_conds.append("ir.incident_date <= %s")
                tl_params.append(end_date)
            tl_where = ' AND '.join(tl_conds)
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT
                        ir.report_number,
                        ir.employee_name,
                        ir.employee_id,
                        ir.submitted_by_name,
                        ir.created_at as filed_at,
                        ir.incident_date,
                        ir.status,
                        ir.rwe_requested_at,
                        ir.rwe_for_signature_at,
                        ir.rwe_for_service_at,
                        ir.rwe_served_at,
                        ir.forwarded_at,
                        ir.for_memo_at,
                        ir.waived_by,
                        ir.waived_at,
                        ir.memo_category,
                        ir.disciplinary_action,
                        GROUP_CONCAT(
                            CONCAT(
                                DATE_FORMAT(ic.created_at, '%%Y-%%m-%%d %%H:%%i'),
                                ' | ', ic.employee_name,
                                ': ', LEFT(ic.comment, 100)
                            )
                            ORDER BY ic.created_at ASC
                            SEPARATOR ' || '
                        ) as comment_timeline
                    FROM incident_reports ir
                    LEFT JOIN incident_comments ic ON ir.id = ic.report_id
                    WHERE {tl_where}
                    GROUP BY ir.id
                    ORDER BY ir.created_at DESC
                """, tl_params)
                ir_timeline_data = cur.fetchall()

        return render_template('incident_dashboard.html',
            reports=reports, stats=stats, user=u,
            rwe_reports=rwe_reports, rwe_report_data=rwe_report_data,
            ir_timeline_data=ir_timeline_data,
            status_filter=status_filter, search=search,
            start_date=start_date, end_date=end_date, sort_by=sort_by)
    finally:
        try:
            conn.close()
        except Exception:
            pass




@ir_bp.route('/new')
@login_required
def new_report():
    u = ir_user()
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT employee_id, schedule_name as full_name
                FROM gsheet_employees
                WHERE status = 'Active'
                ORDER BY schedule_name
            """)
            employees = cur.fetchall()
        return render_template('incident_form.html', employees=employees, user=u)
    finally:
        try:
            conn.close()
        except Exception:
            pass


@ir_bp.route('/submit', methods=['POST'])
@login_required
def submit_report():
    if not validate_csrf():
        return jsonify({'success': False, 'message': 'Security check failed, please try again.'})
    u = ir_user()
    incident_date    = request.form.get('incident_date', '').strip()
    agent_eid        = request.form.get('employee_id', '').strip()
    summary          = request.form.get('summary', '').strip()
    escalate_to_hr   = request.form.get('escalate_to_hr') == '1'

    if not incident_date or not agent_eid or not summary:
        return jsonify({'success': False, 'message': 'All required fields must be filled'})

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT schedule_name as full_name
                FROM gsheet_employees
                WHERE employee_id = %s LIMIT 1
            """, (agent_eid,))
            agent = cur.fetchone()
        if not agent:
            return jsonify({'success': False, 'message': 'Employee not found'})

        employee_name  = agent['full_name']
        report_number  = generate_report_number()
        initial_status = 'rwe_request' if escalate_to_hr else 'pending'
        with conn.cursor() as cur:
            if escalate_to_hr:
                cur.execute("""INSERT INTO incident_reports
                    (report_number, incident_date, employee_id, employee_name,
                     submitted_by_id, submitted_by_name, summary, status, rwe_requested_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NOW())""",
                    (report_number, incident_date, agent_eid, employee_name,
                     u['employee_id'], u['name'], summary, initial_status))
            else:
                cur.execute("""INSERT INTO incident_reports
                    (report_number, incident_date, employee_id, employee_name,
                     submitted_by_id, submitted_by_name, summary, status)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (report_number, incident_date, agent_eid, employee_name,
                     u['employee_id'], u['name'], summary, initial_status))
            report_id = cur.lastrowid
        conn.commit()

        # File uploads (up to 4)
        uploaded = 0
        for i, f in enumerate(request.files.getlist('attachments[]')[:4]):
            filename = save_upload(f, report_number, str(i + 1))
            if filename:
                fsize = os.path.getsize(os.path.join(UPLOAD_DIR, filename))
                with conn.cursor() as cur:
                    cur.execute("""INSERT INTO incident_attachments
                        (report_id, file_name, file_path, file_size, mime_type)
                        VALUES (%s,%s,%s,%s,%s)""",
                        (report_id, filename, os.path.join(UPLOAD_DIR, filename),
                         fsize, f.content_type))
                conn.commit()
                uploaded += 1

        # Emails (non-blocking)
        try:
            send_incident_email(report_number, incident_date, agent_eid,
                                employee_name, u['employee_id'], summary, uploaded)
            if escalate_to_hr:
                send_hr_notification(report_number, u['name'],
                    "This incident was escalated to HR upon submission and "
                    "requires written explanation from the agent.",
                    agent_eid, 'rwe_request')
        except Exception as e:
            logging.error(f"IR submit email error: {e}")

        return jsonify({'success': True, 'report_number': report_number})

    except Exception as e:
        logging.error(f"IR submit error: {e}")
        return jsonify({'success': False, 'message': str(e)})
    finally:
        try:
            conn.close()
        except Exception:
            pass


@ir_bp.route('/debug-ir')
@login_required
def debug_ir():
    u = ir_user()
    conn2 = get_db()
    with conn2.cursor() as cur2:
        cur2.execute("SELECT group_name FROM gsheet_employees WHERE email=%s LIMIT 1", (u["email"],))
        grp = cur2.fetchone()
    conn2.close()
    grp_name = grp["group_name"] if grp else "None"
    return f"<pre>email: {u['email']}\nis_admin: {u['is_admin']}\nis_supervisor: {u['is_supervisor']}\ngroup_name: {grp_name}</pre>"

@ir_bp.route('/<report_number>')
@login_required
def view_report(report_number):
    u = ir_user()
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM incident_reports WHERE report_number=%s", (report_number,))
            report = cur.fetchone()
        if not report:
            return "Report not found", 404

        with conn.cursor() as cur:
            cur.execute("SELECT * FROM incident_attachments WHERE report_id=%s ORDER BY uploaded_at",
                        (report['id'],))
            attachments = cur.fetchall()

        with conn.cursor() as cur:
            cur.execute("SELECT * FROM incident_comments WHERE report_id=%s ORDER BY created_at ASC",
                        (report['id'],))
            comments = cur.fetchall()

        comment_attachments = {}
        for c in comments:
            with conn.cursor() as cur:
                cur.execute("""SELECT * FROM incident_comment_attachments
                               WHERE comment_id=%s ORDER BY uploaded_at""", (c['id'],))
                comment_attachments[c['id']] = cur.fetchall()

        can_edit_report = u['is_admin'] or (report['submitted_by_id'] == u['employee_id'])

        return render_template('incident_view.html',
            report=report, attachments=attachments,
            comments=comments, comment_attachments=comment_attachments,
            can_edit_report=can_edit_report,
            user=u, HR_EMAILS=HR_EMAILS, SGA_EMAILS=SGA_EMAILS)
    finally:
        try:
            conn.close()
        except Exception:
            pass


@ir_bp.route('/edit-report', methods=['POST'])
@login_required
def edit_report():
    if not validate_csrf():
        return jsonify({'success': False, 'message': 'Security check failed, please try again.'})
    u = ir_user()
    conn = get_db()
    try:
        report_id = request.form.get('report_id', 0, type=int)
        with conn.cursor() as cur:
            cur.execute("SELECT submitted_by_id, summary, incident_date FROM incident_reports WHERE id=%s",
                        (report_id,))
            old = cur.fetchone()
        if not old:
            return jsonify({'success': False, 'message': 'Report not found'})

        can_edit = u['is_admin'] or (old['submitted_by_id'] == u['employee_id'])
        if not can_edit:
            return jsonify({'success': False, 'message': 'Not authorized'})

        updates, history = {}, []
        new_summary = request.form.get('summary')
        new_date    = request.form.get('incident_date')

        if new_summary and new_summary.strip() != old['summary']:
            updates['summary']       = new_summary.strip()
            history.append(('summary', old['summary'], new_summary.strip()))
        if new_date and str(new_date) != str(old['incident_date']):
            updates['incident_date'] = new_date
            history.append(('incident_date', str(old['incident_date']), new_date))

        if not updates:
            return jsonify({'success': False, 'message': 'No changes detected'})

        personid = get_personid(u['employee_id'], conn)
        set_parts = ', '.join([f"`{k}`=%s" for k in updates])
        set_parts += ", edited_at=NOW(), edited_by=%s, edit_count=edit_count+1"
        vals = list(updates.values()) + [personid, report_id]

        with conn.cursor() as cur:
            cur.execute(f"UPDATE incident_reports SET {set_parts} WHERE id=%s", vals)

        if personid:
            for field, old_val, new_val in history:
                with conn.cursor() as cur:
                    cur.execute("""INSERT INTO incident_report_edit_history
                        (report_id, edited_by, edited_at, field_name, old_value, new_value)
                        VALUES (%s,%s,NOW(),%s,%s,%s)""",
                        (report_id, personid, field, old_val, new_val))
        conn.commit()
        return jsonify({'success': True, 'message': 'Report updated successfully'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})
    finally:
        try:
            conn.close()
        except Exception:
            pass


@ir_bp.route('/edit-comment', methods=['POST'])
@login_required
def edit_comment():
    if not validate_csrf():
        return jsonify({'success': False, 'message': 'Security check failed, please try again.'})
    u = ir_user()
    conn = get_db()
    try:
        comment_id  = request.form.get('comment_id', 0, type=int)
        new_comment = request.form.get('comment', '').strip()
        if not new_comment:
            return jsonify({'success': False, 'message': 'Comment cannot be empty'})

        with conn.cursor() as cur:
            cur.execute("SELECT employee_id, comment FROM incident_comments WHERE id=%s", (comment_id,))
            old = cur.fetchone()
        if not old:
            return jsonify({'success': False, 'message': 'Comment not found'})

        can_edit = u['is_admin'] or (old['employee_id'] == u['employee_id'])
        if not can_edit:
            return jsonify({'success': False, 'message': 'Not authorized'})
        if old['comment'] == new_comment:
            return jsonify({'success': False, 'message': 'No changes detected'})

        personid = get_personid(u['employee_id'], conn)
        with conn.cursor() as cur:
            cur.execute("""UPDATE incident_comments
                SET comment=%s, edited_at=NOW(), edited_by=%s, edit_count=edit_count+1
                WHERE id=%s""", (new_comment, personid, comment_id))
        if personid:
            with conn.cursor() as cur:
                cur.execute("""INSERT INTO incident_comment_edit_history
                    (comment_id, edited_by, edited_at, old_content, new_content)
                    VALUES (%s,%s,NOW(),%s,%s)""",
                    (comment_id, personid, old['comment'], new_comment))
        conn.commit()
        return jsonify({'success': True, 'message': 'Comment updated successfully'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})
    finally:
        try:
            conn.close()
        except Exception:
            pass


@ir_bp.route('/add-comment', methods=['POST'])
@login_required
def add_comment():
    if not validate_csrf():
        return jsonify({'success': False, 'message': 'Security check failed, please try again.'})
    u = ir_user()
    conn = get_db()
    try:
        report_number = request.form.get('report_number', '').strip()
        comment_text  = request.form.get('comment', '').strip()
        status_action = request.form.get('status_action', '').strip()

        if not report_number or not comment_text:
            return jsonify({'success': False, 'message': 'Comment cannot be empty'})

        valid = ['reviewed', 'resolved', 'rwe_request', 'rwe_for_signature', 'rwe_for_service', 'rwe_served', 'awaiting_explanation', 'forwarded', 'for_memo', 'waived']
        if status_action and status_action not in valid:
            return jsonify({'success': False, 'message': 'Invalid status'})

        with conn.cursor() as cur:
            cur.execute("""SELECT id, employee_id, submitted_by_id
                           FROM incident_reports WHERE report_number=%s""", (report_number,))
            report = cur.fetchone()
        if not report:
            return jsonify({'success': False, 'message': 'Report not found'})

        with conn.cursor() as cur:
            cur.execute("""INSERT INTO incident_comments
                (report_id, employee_id, employee_name, comment)
                VALUES (%s,%s,%s,%s)""",
                (report['id'], u['employee_id'], u['name'], comment_text))
            comment_id = cur.lastrowid

        # Comment file uploads
        uploaded = 0
        for i, f in enumerate(request.files.getlist('comment_attachments[]')[:4]):
            filename = save_upload(f, report_number, f'c{comment_id}_{i+1}')
            if filename:
                fsize = os.path.getsize(os.path.join(UPLOAD_DIR, filename))
                with conn.cursor() as cur:
                    cur.execute("""INSERT INTO incident_comment_attachments
                        (comment_id, file_name, file_path, file_size, mime_type)
                        VALUES (%s,%s,%s,%s,%s)""",
                        (comment_id, filename, os.path.join(UPLOAD_DIR, filename),
                         fsize, f.content_type))
                uploaded += 1

        if status_action:
            with conn.cursor() as cur:
                if status_action == 'rwe_request':
                    cur.execute("""UPDATE incident_reports
                                   SET status=%s, rwe_requested_at=NOW()
                                   WHERE report_number=%s""",
                                (status_action, report_number))
                elif status_action == 'rwe_for_signature':
                    cur.execute("""UPDATE incident_reports
                                   SET status=%s, rwe_for_signature_at=NOW()
                                   WHERE report_number=%s""",
                                (status_action, report_number))
                elif status_action == 'rwe_for_service':
                    cur.execute("""UPDATE incident_reports
                                   SET status=%s, rwe_for_service_at=NOW()
                                   WHERE report_number=%s""",
                                (status_action, report_number))
                elif status_action == 'awaiting_explanation':
                    # HR served the RWE -> now waiting for the agent's written explanation.
                    # Stamp served + entered-awaiting-explanation (same moment).
                    cur.execute("""UPDATE incident_reports
                                   SET status=%s, rwe_served_at=NOW(), awaiting_explanation_at=NOW()
                                   WHERE report_number=%s""",
                                (status_action, report_number))
                elif status_action == 'forwarded':
                    # Agent's explanation received (or no response) -> forward to SOM/TL.
                    # Stamp forwarded; also stamp served/awaiting if this was a direct admin jump.
                    cur.execute("""UPDATE incident_reports
                                   SET status=%s,
                                       rwe_served_at=COALESCE(rwe_served_at, NOW()),
                                       awaiting_explanation_at=COALESCE(awaiting_explanation_at, NOW()),
                                       forwarded_at=NOW()
                                   WHERE report_number=%s""",
                                (status_action, report_number))
                elif status_action == 'for_memo':
                    # SOM/TL chose to proceed with disciplinary action -> back to HR for memo.
                    cur.execute("""UPDATE incident_reports
                                   SET status=%s, for_memo_at=NOW()
                                   WHERE report_number=%s""",
                                (status_action, report_number))
                elif status_action == 'rwe_served':
                    # Legacy/admin direct action -> stamp served only.
                    cur.execute("""UPDATE incident_reports
                                   SET status=%s, rwe_served_at=NOW()
                                   WHERE report_number=%s""",
                                (status_action, report_number))
                elif status_action == 'resolved':
                    memo_cat  = request.form.get('memo_category', '').strip()
                    disc_act  = request.form.get('disciplinary_action', '').strip()
                    cur.execute("""UPDATE incident_reports
                                   SET status=%s, memo_category=%s, disciplinary_action=%s, resolved_at=NOW()
                                   WHERE report_number=%s""",
                                (status_action, memo_cat or None, disc_act or None, report_number))
                elif status_action == 'waived':
                    waived_by_name = request.form.get('waived_by_name', '').strip() or u['name']
                    cur.execute("""UPDATE incident_reports
                                   SET status=%s, waived_by=%s, waived_at=NOW()
                                   WHERE report_number=%s""",
                                (status_action, waived_by_name, report_number))
                else:
                    cur.execute("UPDATE incident_reports SET status=%s WHERE report_number=%s",
                                (status_action, report_number))
        conn.commit()

        try:
            send_comment_notification(report_number, u['name'], comment_text,
                                      report['employee_id'], report['submitted_by_id'],
                                      status_action)
            
            # Workflow Modification: Reporter requests RWE -> Alerts HR
            if status_action == 'rwe_request':
                send_hr_notification(report_number, u['name'], 
                                     f"RWE Requested by reporter: {comment_text}",
                                     report['employee_id'], 'rwe_request')
            
            # Workflow Modification: HR serves RWE -> Notify TL/supervisor
            elif status_action == 'rwe_served':
                send_rwe_served_to_tl(report_number, u['name'], comment_text,
                                      report['employee_id'])
                                     
            # Fallback legacy compatibility
        except Exception as e:
            logging.error(f"Comment email error: {e}")

        return jsonify({'success': True,
                        'message': f"Comment posted{f' with {uploaded} attachment(s)' if uploaded else ''}"})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})
    finally:
        try:
            conn.close()
        except Exception:
            pass


@ir_bp.route('/edit-history')
@login_required
def edit_history():
    conn = get_db()
    try:
        h_type = request.args.get('type', '')
        h_id   = request.args.get('id', 0, type=int)

        if h_type == 'report':
            sql = """SELECT h.*, CONCAT(u.fname,' ',u.lname) as editor_name
                     FROM incident_report_edit_history h
                     LEFT JOIN userdata u ON h.edited_by = u.personid
                     WHERE h.report_id=%s ORDER BY h.edited_at DESC"""
        elif h_type == 'comment':
            sql = """SELECT h.*, CONCAT(u.fname,' ',u.lname) as editor_name
                     FROM incident_comment_edit_history h
                     LEFT JOIN userdata u ON h.edited_by = u.personid
                     WHERE h.comment_id=%s ORDER BY h.edited_at DESC"""
        else:
            return jsonify({'success': False, 'message': 'Invalid type'})

        with conn.cursor() as cur:
            cur.execute(sql, (h_id,))
            history = cur.fetchall()

        # Serialize datetime objects
        for row in history:
            for k, v in row.items():
                if hasattr(v, 'isoformat'):
                    row[k] = v.isoformat()

        return jsonify({'success': True, 'history': history})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})
    finally:
        try:
            conn.close()
        except Exception:
            pass


@ir_bp.route('/update-status', methods=['POST'])
@login_required
def update_status():
    u = ir_user()
    if not u['is_admin']:
        return jsonify({'success': False, 'message': 'Unauthorized'})
    if not validate_csrf():
        return jsonify({'success': False, 'message': 'Security check failed, please try again.'})

    data          = request.get_json() or {}
    report_number = data.get('report_number', '')
    new_status    = data.get('status', '')

    # Update this array to allow your two new workflow steps!
    valid = ['pending', 'reviewed', 'resolved', 'rwe_request', 'rwe_for_signature', 'rwe_for_service', 'rwe_served', 'awaiting_explanation', 'forwarded', 'for_memo', 'waived']
    if not report_number or new_status not in valid:
        return jsonify({'success': False, 'message': 'Invalid status'})

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE incident_reports SET status=%s WHERE report_number=%s",
                        (new_status, report_number))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})
    finally:
        try:
            conn.close()
        except Exception:
            pass

@ir_bp.route('/uploads/<filename>')
@login_required
def serve_upload(filename):
    from flask import send_from_directory
    return send_from_directory(
        '/var/www/html/cohere_dashboard/incident_report/uploads',
        filename
    )

@ir_bp.route('/timeline-csv')
@login_required
def timeline_csv():
    u = ir_user()
    if not u['is_admin']:
        return "Unauthorized", 403
    import csv, io
    from flask import Response
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    ir.report_number,
                    ir.employee_name,
                    ir.employee_id,
                    ir.submitted_by_name,
                    ir.created_at as filed_at,
                    ir.incident_date,
                    ir.status,
                    ir.rwe_requested_at,
                    ir.rwe_for_signature_at,
                    ir.rwe_for_service_at,
                    ir.rwe_served_at,
                    ir.waived_by,
                    ir.waived_at,
                    ir.memo_category,
                    ir.disciplinary_action,
                    ic.employee_name as commenter,
                    ic.comment,
                    ic.created_at as comment_date,
                    ROW_NUMBER() OVER (PARTITION BY ir.id ORDER BY ic.created_at ASC) as comment_num
                FROM incident_reports ir
                LEFT JOIN incident_comments ic ON ir.id = ic.report_id
                ORDER BY ir.created_at DESC, ic.created_at ASC
            """)
            rows = cur.fetchall()

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            'IR Number', 'Agent', 'Employee ID', 'Filed By',
            'Filed Date', 'Incident Date', 'Status',
            'RWE Requested', 'RWE For Signature', 'RWE For Service', 'RWE Served', 'Forwarded At', 'For Memo At', 'Waived By', 'Waived At',
            'Memo Category', 'Disciplinary Action',
            'Comment #', 'Comment Date', 'Commenter', 'Comment'
        ])
        for r in rows:
            writer.writerow([
                r['report_number'],
                r['employee_name'],
                r['employee_id'],
                r['submitted_by_name'] or '',
                r['filed_at'].strftime('%Y-%m-%d %H:%M') if r['filed_at'] else '',
                str(r['incident_date']) if r['incident_date'] else '',
                r['status'],
                r['rwe_requested_at'].strftime('%Y-%m-%d %H:%M') if r['rwe_requested_at'] else '',
                r['rwe_for_signature_at'].strftime('%Y-%m-%d %H:%M') if r.get('rwe_for_signature_at') else '',
                r['rwe_for_service_at'].strftime('%Y-%m-%d %H:%M') if r.get('rwe_for_service_at') else '',
                r['rwe_served_at'].strftime('%Y-%m-%d %H:%M') if r['rwe_served_at'] else '',
                r['forwarded_at'].strftime('%Y-%m-%d %H:%M') if r.get('forwarded_at') else '',
                r['for_memo_at'].strftime('%Y-%m-%d %H:%M') if r.get('for_memo_at') else '',
                r['waived_by'] or '',
                r['waived_at'].strftime('%Y-%m-%d %H:%M') if r['waived_at'] else '',
                r['memo_category'] or '',
                r['disciplinary_action'] or '',
                r['comment_num'] or '',
                r['comment_date'].strftime('%Y-%m-%d %H:%M') if r['comment_date'] else '',
                r['commenter'] or '',
                r['comment'] or '',
            ])

        output.seek(0)
        filename = f"IR_Timeline_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        return Response(
            output.getvalue(),
            mimetype='text/csv',
            headers={'Content-Disposition': f'attachment;filename={filename}'}
        )
    finally:
        try:
            conn.close()
        except Exception:
            pass
