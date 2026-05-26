# incident_reports.py
# Flask Blueprint — migrated from PHP incident_report module
# Place at: /var/www/html/leavesystem/incident_reports.py

from flask import Blueprint, render_template, request, jsonify, session, redirect, url_for
from datetime import datetime
from functools import wraps
import os, uuid, smtplib, logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

ir_bp = Blueprint('incident_reports', __name__)

# ─── Constants ────────────────────────────────────────────────────────────────
HR_EMAILS   = ['jed.sagardui@cohere.ph', 'honey.cortes@cohere.ph', 'anamarie.munez@cohere.ph']
SGA_EMAILS  = ['anamarie.munez@cohere.ph', 'honey.cortes@cohere.ph']
IR_EMAIL_TO = 'managers@cohere.ph'
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
    return {
        'email':       email,
        'employee_id': session['user']['employee_id'],
        'name':        session['user']['name'],
        'is_admin':    session.get('is_admin', False) or email in IR_ALL_ACCESS,
        'is_supervisor': session.get('is_supervisor', False),
        'is_hr':       email in HR_EMAILS,
        'is_sga':      email in SGA_EMAILS,
    }

# ─── Helpers ──────────────────────────────────────────────────────────────────
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
                INNER JOIN gsheet_employees g ON sm.agent_email = g.email
                WHERE g.employee_id = %s
                LIMIT 1
            """, (employee_id,))
            row = cur.fetchone()
            return row['supervisor_email'] if row else None
    except Exception:
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
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From']    = f"{SMTP_FROM_NAME} <{SMTP_FROM}>"
        msg['To']      = ', '.join(filter(None, set(to_list)))
        if thread_id:
            mid = f"<incident-{thread_id}@dashboard.cohere.ph>"
            msg['In-Reply-To'] = mid
            msg['References']  = mid
        msg.attach(MIMEText(html_body, 'html'))
        all_rcpt = list(filter(None, set(to_list) | set(bcc_list or [])))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(SMTP_FROM, all_rcpt, msg.as_string())
        return True
    except Exception as e:
        logging.error(f"IR email failed: {e}")
        return False

def _ir_email_body(title, subtitle, fields, action_url, action_label='📊 View Report', alert=None):
    """Generic branded email body builder."""
    field_html = ''
    for label, value in fields:
        field_html += f"""
        <div style="background:white;border-left:4px solid #ff6b35;padding:15px;margin:8px 0;border-radius:4px;">
            <div style="color:#0f2557;font-weight:bold;font-size:12px;text-transform:uppercase;">{label}</div>
            <div style="margin-top:5px;color:#333;">{value}</div>
        </div>"""

    alert_html = ''
    if alert:
        alert_html = f"""<div style="background:white;border-left:4px solid #dc3545;padding:15px;margin-bottom:20px;border-radius:4px;">
            <strong>⚠️ Action Required:</strong> {alert}</div>"""

    return f"""
    <html><body style="font-family:Arial,sans-serif;line-height:1.6;color:#333;">
    <div style="max-width:600px;margin:0 auto;">
        <div style="background:linear-gradient(135deg,#0f2557,#1e3a8a);color:white;padding:30px;text-align:center;border-radius:10px 10px 0 0;">
            <h2 style="margin:0;">{title}</h2>
            <p style="margin:8px 0 0;font-size:14px;">{subtitle}</p>
        </div>
        <div style="background:#f9f9f9;padding:25px;border:1px solid #ddd;">
            {alert_html}{field_html}
            <div style="background:#fff3cd;border:2px solid #ff6b35;border-radius:8px;padding:20px;margin:25px 0;text-align:center;">
                <a href="{action_url}" style="background:linear-gradient(135deg,#0f2557,#ff6b35);color:white;padding:14px 35px;text-decoration:none;border-radius:8px;font-weight:bold;">{action_label}</a>
            </div>
        </div>
        <div style="background:#333;color:white;padding:12px;text-align:center;border-radius:0 0 10px 10px;font-size:12px;">⚡ Incident Report System</div>
    </div></body></html>"""

def send_incident_email(report_number, incident_date, agent_eid, employee_name,
                        submitter_eid, summary, attachment_count):
    conn = get_db()
    try:
        recipients = [IR_EMAIL_TO]
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

        status_text = ('PENDING HR — WRITTEN EXPLANATION REQUIRED'
                       if status_action == 'pending_hr' else 'RESOLVED HR — COMPLETED')
        body = f"""
        <html><body style="font-family:Arial,sans-serif;">
        <div style="max-width:600px;margin:0 auto;">
            <div style="background:linear-gradient(135deg,#dc3545,#ff6b35);color:white;padding:25px;text-align:center;border-radius:10px 10px 0 0;">
                <h2 style="margin:0;">📋 HR Escalation</h2>
                <p style="margin:8px 0 0;">Report #{report_number}</p>
            </div>
            <div style="padding:25px;background:#f9f9f9;">
                <div style="background:{'#ffc107' if status_action=='pending_hr' else '#28a745'};color:{'#000' if status_action=='pending_hr' else 'white'};padding:10px 20px;border-radius:20px;display:inline-block;font-weight:bold;margin-bottom:20px;">{status_text}</div>
                <div style="background:white;padding:15px;margin:10px 0;border-radius:4px;">
                    <p><strong>Date:</strong> {inc['incident_date'].strftime('%B %d, %Y') if hasattr(inc['incident_date'], 'strftime') else str(inc['incident_date'])}</p>
                    <p><strong>Agent:</strong> {inc['employee_name']} ({inc['employee_id']})</p>
                    <p><strong>Group:</strong> {inc.get('group_name') or 'N/A'}</p>
                    <p><strong>Summary:</strong> {inc['summary']}</p>
                </div>
                <p><strong>Comment from {commenter_name}:</strong></p>
                <div style="background:#fff3cd;border-left:4px solid #ff6b35;padding:20px;margin:15px 0;">{comment.replace(chr(10), '<br>')}</div>
                <div style="text-align:center;margin-top:25px;">
                    <a href="https://hrportal.cohere.ph/incident-reports/{report_number}"
                       style="background:linear-gradient(135deg,#dc3545,#ff6b35);color:white;padding:14px 35px;text-decoration:none;border-radius:8px;font-weight:bold;">📊 View Full Report</a>
                </div>
            </div>
            <div style="background:#333;color:white;padding:12px;text-align:center;font-size:12px;">⚡ HR Escalation Notification</div>
        </div></body></html>"""

        _send(HR_EMAILS, f"Re: New Incident Report: {report_number} — HR ACTION REQUIRED",
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
        recipients = [IR_EMAIL_TO]
        sup = get_supervisor_email(agent_eid, conn)
        if sup: recipients.append(sup)
        recipients += get_group_emails(submitter_eid, conn)

        status_badge = (f"<div style='background:#28a745;color:white;display:inline-block;"
                        f"padding:5px 12px;border-radius:20px;font-weight:bold;font-size:12px;"
                        f"margin-left:8px;'>STATUS: {status_action.upper()} ✅</div>"
                        if status_action else '')

        body = f"""
        <html><body style="font-family:Arial,sans-serif;">
        <div style="max-width:600px;margin:0 auto;">
            <div style="background:linear-gradient(135deg,#0f2557,#1e3a8a);color:white;padding:25px;text-align:center;border-radius:10px 10px 0 0;">
                <h2 style="margin:0;">New Comment on Incident Report</h2>
                <p style="margin:8px 0 0;font-size:14px;">Report #{report_number}</p>
            </div>
            <div style="background:#f9f9f9;padding:25px;border:1px solid #ddd;">
                <div style="margin-bottom:15px;">
                    <span style="background:#ff6b35;color:white;padding:5px 12px;border-radius:20px;font-weight:bold;font-size:12px;">NEW COMMENT</span>
                    {status_badge}
                </div>
                <div style="background:white;padding:15px;border-left:4px solid #0f2557;border-radius:4px;margin-bottom:20px;">
                    <strong style="color:#0f2557;font-size:16px;">{commenter_name}</strong>
                    <div style="color:#999;font-size:13px;margin-top:4px;">{datetime.now().strftime('%b %d, %Y at %I:%M %p')}</div>
                </div>
                <div style="background:white;border-left:4px solid #ff6b35;padding:20px;margin:15px 0;">{comment.replace(chr(10),'<br>')}</div>
                <div style="text-align:center;margin-top:25px;padding:20px;background:#fff3cd;border-radius:8px;">
                    <a href="https://hrportal.cohere.ph/incident-reports/{report_number}"
                       style="background:linear-gradient(135deg,#0f2557,#ff6b35);color:white;padding:12px 28px;text-decoration:none;border-radius:8px;font-weight:bold;">View Report</a>
                </div>
            </div>
            <div style="background:#333;color:white;padding:12px;text-align:center;font-size:12px;">Incident Report System</div>
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
            elif u['is_hr'] or u['is_sga']:
                conds.append("ir.status IN ('pending_hr','resolved_hr')")

        if status_filter != 'all':
            conds.append("ir.status = %s"); params.append(status_filter)
        if search:
            conds.append("(ir.report_number LIKE %s OR ir.employee_name LIKE %s OR ir.summary LIKE %s)")
            params += [f'%{search}%', f'%{search}%', f'%{search}%']
        if start_date:
            conds.append("ir.incident_date >= %s"); params.append(start_date)
        if end_date:
            conds.append("ir.incident_date <= %s"); params.append(end_date)

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
                SUM(CASE WHEN ir.status='pending_hr'  THEN 1 ELSE 0 END) as pending_hr,
                SUM(CASE WHEN ir.status='resolved_hr' THEN 1 ELSE 0 END) as resolved_hr
                FROM incident_reports ir WHERE {' AND '.join(s_conds)}""", s_params)
            stats = cur.fetchone()

        return render_template('incident_dashboard.html',
            reports=reports, stats=stats, user=u,
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
        initial_status = 'pending_hr' if escalate_to_hr else 'pending'

        with conn.cursor() as cur:
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
                    agent_eid, 'pending_hr')
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
    u = ir_user()
    conn = get_db()
    try:
        report_number = request.form.get('report_number', '').strip()
        comment_text  = request.form.get('comment', '').strip()
        status_action = request.form.get('status_action', '').strip()

        if not report_number or not comment_text:
            return jsonify({'success': False, 'message': 'Comment cannot be empty'})

        valid = ['reviewed', 'resolved', 'pending_hr', 'resolved_hr']
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
                cur.execute("UPDATE incident_reports SET status=%s WHERE report_number=%s",
                            (status_action, report_number))
        conn.commit()

        try:
            send_comment_notification(report_number, u['name'], comment_text,
                                      report['employee_id'], report['submitted_by_id'],
                                      status_action)
            if status_action in ['pending_hr', 'resolved_hr']:
                send_hr_notification(report_number, u['name'], comment_text,
                                     report['employee_id'], status_action)
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

    data          = request.get_json() or {}
    report_number = data.get('report_number', '')
    new_status    = data.get('status', '')

    valid = ['pending', 'reviewed', 'resolved', 'pending_hr', 'resolved_hr']
    if not report_number or new_status not in valid:
        return jsonify({'success': False, 'message': 'Invalid input'})

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
