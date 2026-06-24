# qa_updates.py
# Blueprint for the QA Updates bulletin feature.
# Bulletin model: Topic (title) + Description (procedure/update text) + Category
# No severity/status/module fields - this is a process-update bulletin, not a bug tracker.

import os
from datetime import datetime
from functools import wraps

from flask import (
    Blueprint, render_template, request, jsonify, session,
    redirect, url_for, flash, current_app
)
from werkzeug.utils import secure_filename
import pymysql
import pymysql.cursors

qa_updates_bp = Blueprint('qa_updates', __name__, url_prefix='/qa-updates')

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
UPLOAD_DIR = '/var/www/html/leavesystem/uploads/qa_updates'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'pdf', 'docx', 'xlsx', 'csv', 'txt', 'log'}

CATEGORY_CHOICES = ['CSR', 'Refunds', 'Policy', 'General', 'Other']


def get_db():
    """Reuse the app's existing db connector. Adjust import to match app.py."""
    from app import get_db_connection  # adjust if your helper has a different name
    return get_db_connection()


# ---------------------------------------------------------------------------
# Permission helpers
# ---------------------------------------------------------------------------
def qa_can_manage():
    """Template global + internal check: can this session create/edit/delete QA updates?"""
    if session.get('is_admin'):
        return True
    perms = session.get('permissions') or {}
    return bool(perms.get('can_qa_updates'))


def require_qa_manage(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get('user'):
            return redirect(url_for('login'))
        if not qa_can_manage():
            if request.path.startswith('/qa-updates/api') or request.is_json:
                return jsonify({'error': 'Forbidden'}), 403
            flash('You do not have permission to manage QA Updates.', 'danger')
            return redirect(url_for('qa_updates.bulletin'))
        return f(*args, **kwargs)
    return wrapper


def require_login(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get('user'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return wrapper


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def log_history(cursor, qa_update_id, field_changed, old_value, new_value, changed_by):
    cursor.execute(
        """INSERT INTO qa_update_history
           (qa_update_id, field_changed, old_value, new_value, changed_by, changed_at)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        (qa_update_id, field_changed, str(old_value) if old_value is not None else None,
         str(new_value) if new_value is not None else None, changed_by, datetime.now())
    )


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------
@qa_updates_bp.route('/')
@require_login
def bulletin():
    """Main Tab bulletin view - everyone can view."""
    return render_template('qa_updates_bulletin.html', category_choices=CATEGORY_CHOICES)


@qa_updates_bp.route('/admin')
@require_qa_manage
def admin():
    """Admin management view."""
    return render_template('qa_updates_admin.html', category_choices=CATEGORY_CHOICES)


# ---------------------------------------------------------------------------
# API: list / read
# ---------------------------------------------------------------------------
@qa_updates_bp.route('/api/list')
@require_login
def api_list():
    """
    Returns JSON list of QA updates with optional filters.
    Query params: category, q (search)
    """
    category = request.args.get('category', '').strip()
    q = request.args.get('q', '').strip()

    conn = get_db()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            where = ["is_deleted = 0", "is_active = 1"]
            params = []

            if category:
                where.append("category = %s")
                params.append(category)
            if q:
                where.append("(title LIKE %s OR description LIKE %s)")
                params.extend([f"%{q}%", f"%{q}%"])

            where_clause = " AND ".join(where)

            cur.execute(
                f"""SELECT u.*,
                       (SELECT COUNT(*) FROM qa_update_acknowledgments a WHERE a.qa_update_id = u.id) AS ack_count
                    FROM qa_updates u
                    WHERE {where_clause}
                    ORDER BY u.is_pinned DESC, u.created_at DESC""",
                params
            )
            updates = cur.fetchall()

            current_emp = session.get('user', {}).get('employee_id')
            if updates and current_emp:
                ids = [u['id'] for u in updates]
                cur.execute(
                    f"""SELECT qa_update_id FROM qa_update_acknowledgments
                        WHERE employee_id = %s AND qa_update_id IN ({','.join(['%s']*len(ids))})""",
                    [current_emp] + ids
                )
                acked_ids = {r['qa_update_id'] for r in cur.fetchall()}
                for u in updates:
                    u['acknowledged_by_me'] = u['id'] in acked_ids

            for u in updates:
                cur.execute(
                    "SELECT id, filename, original_filename FROM qa_update_attachments WHERE qa_update_id = %s",
                    (u['id'],)
                )
                u['attachments'] = cur.fetchall()
                for dt_field in ('created_at', 'updated_at'):
                    if u.get(dt_field):
                        u[dt_field] = u[dt_field].isoformat()
                if u.get('implementation_date'):
                    u['implementation_date'] = u['implementation_date'].isoformat()

            return jsonify({'updates': updates})
    finally:
        conn.close()


@qa_updates_bp.route('/api/list-deactivated')
@require_qa_manage
def api_list_deactivated():
    """Admin-only: all deactivated (is_active = 0) updates, for the Deactivated tab."""
    conn = get_db()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                """SELECT * FROM qa_updates
                   WHERE is_deleted = 0 AND is_active = 0
                   ORDER BY deactivated_at DESC"""
            )
            updates = cur.fetchall()
            for u in updates:
                for dt_field in ('created_at', 'updated_at', 'deactivated_at'):
                    if u.get(dt_field):
                        u[dt_field] = u[dt_field].isoformat()
                if u.get('implementation_date'):
                    u['implementation_date'] = u['implementation_date'].isoformat()
            return jsonify({'updates': updates})
    finally:
        conn.close()


@qa_updates_bp.route('/api/<int:update_id>/toggle-active', methods=['POST'])
@require_qa_manage
def api_toggle_active(update_id):
    """
    Deactivate (hide from bulletin) or reactivate an update.
    Deactivation timestamp + actor are recorded for audit/log purposes.
    """
    make_active = request.form.get('is_active') == '1'
    changed_by = session.get('user', {}).get('name')
    now = datetime.now()

    conn = get_db()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("SELECT id, title, is_active FROM qa_updates WHERE id = %s AND is_deleted = 0", (update_id,))
            existing = cur.fetchone()
            if not existing:
                return jsonify({'error': 'Not found'}), 404

            if make_active:
                cur.execute(
                    """UPDATE qa_updates SET is_active = 1, deactivated_at = NULL,
                       deactivated_by = NULL, updated_at = %s WHERE id = %s""",
                    (now, update_id)
                )
                log_history(cur, update_id, 'is_active', existing.get('is_active'), 1, changed_by)
            else:
                cur.execute(
                    """UPDATE qa_updates SET is_active = 0, deactivated_at = %s,
                       deactivated_by = %s, updated_at = %s WHERE id = %s""",
                    (now, changed_by, now, update_id)
                )
                log_history(cur, update_id, 'is_active', existing.get('is_active'), 0, changed_by)

            conn.commit()
        return jsonify({'success': True, 'is_active': make_active})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@qa_updates_bp.route('/api/<int:update_id>/history')
@require_login
def api_history(update_id):
    conn = get_db()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT * FROM qa_update_history WHERE qa_update_id = %s ORDER BY changed_at DESC",
                (update_id,)
            )
            history = cur.fetchall()
            for h in history:
                if h.get('changed_at'):
                    h['changed_at'] = h['changed_at'].isoformat()
            return jsonify({'history': history})
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# API: create / update / delete (admin only)
# ---------------------------------------------------------------------------
@qa_updates_bp.route('/api/create', methods=['POST'])
@require_qa_manage
def api_create():
    title = request.form.get('title', '').strip()
    description = request.form.get('description', '').strip()
    category = request.form.get('category', '').strip()
    implementation_date = request.form.get('implementation_date', '').strip() or None
    is_pinned = 1 if request.form.get('is_pinned') == '1' else 0

    if not title or not description:
        return jsonify({'error': 'Topic and description are required.'}), 400

    user = session.get('user', {})
    author_employee_id = user.get('employee_id')
    author_name = user.get('name')
    now = datetime.now()

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO qa_updates
                   (title, description, category, implementation_date, author_employee_id,
                    author_name, is_pinned, created_at, updated_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (title, description, category, implementation_date, author_employee_id,
                 author_name, is_pinned, now, now)
            )
            new_id = cur.lastrowid

            files = request.files.getlist('attachments')
            os.makedirs(UPLOAD_DIR, exist_ok=True)
            for f in files:
                if f and f.filename and allowed_file(f.filename):
                    safe_name = secure_filename(f.filename)
                    stored_name = f"{new_id}_{int(now.timestamp())}_{safe_name}"
                    f.save(os.path.join(UPLOAD_DIR, stored_name))
                    cur.execute(
                        """INSERT INTO qa_update_attachments
                           (qa_update_id, filename, original_filename, uploaded_at)
                           VALUES (%s,%s,%s,%s)""",
                        (new_id, stored_name, safe_name, now)
                    )

            log_history(cur, new_id, 'created', None, title, author_name)
            conn.commit()
        return jsonify({'success': True, 'id': new_id})
    except Exception as e:
        conn.rollback()
        current_app.logger.exception('QA update create failed')
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@qa_updates_bp.route('/api/<int:update_id>/update', methods=['POST'])
@require_qa_manage
def api_update(update_id):
    """Partial update - used for full edits and the quick pin toggle."""
    conn = get_db()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("SELECT * FROM qa_updates WHERE id = %s AND is_deleted = 0", (update_id,))
            existing = cur.fetchone()
            if not existing:
                return jsonify({'error': 'Not found'}), 404

            changed_by = session.get('user', {}).get('name')
            now = datetime.now()
            updates = {}

            for field in ('title', 'description', 'category'):
                if field in request.form:
                    new_val = request.form.get(field, '').strip()
                    old_val = existing.get(field)
                    if new_val != old_val:
                        updates[field] = new_val
                        log_history(cur, update_id, field, old_val, new_val, changed_by)

            if 'implementation_date' in request.form:
                new_val = request.form.get('implementation_date', '').strip() or None
                old_val = existing.get('implementation_date')
                old_val_str = old_val.isoformat() if old_val else None
                if new_val != old_val_str:
                    updates['implementation_date'] = new_val
                    log_history(cur, update_id, 'implementation_date', old_val_str, new_val, changed_by)

            if 'is_pinned' in request.form:
                new_pin = 1 if request.form.get('is_pinned') == '1' else 0
                if new_pin != existing.get('is_pinned'):
                    updates['is_pinned'] = new_pin
                    log_history(cur, update_id, 'is_pinned', existing.get('is_pinned'), new_pin, changed_by)

            if not updates:
                return jsonify({'success': True, 'message': 'No changes.'})

            updates['updated_at'] = now

            set_clause = ", ".join(f"{k} = %s" for k in updates)
            params = list(updates.values()) + [update_id]
            cur.execute(f"UPDATE qa_updates SET {set_clause} WHERE id = %s", params)

            files = request.files.getlist('attachments')
            if files:
                os.makedirs(UPLOAD_DIR, exist_ok=True)
                for f in files:
                    if f and f.filename and allowed_file(f.filename):
                        safe_name = secure_filename(f.filename)
                        stored_name = f"{update_id}_{int(now.timestamp())}_{safe_name}"
                        f.save(os.path.join(UPLOAD_DIR, stored_name))
                        cur.execute(
                            """INSERT INTO qa_update_attachments
                               (qa_update_id, filename, original_filename, uploaded_at)
                               VALUES (%s,%s,%s,%s)""",
                            (update_id, stored_name, safe_name, now)
                        )

            conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        conn.rollback()
        current_app.logger.exception('QA update edit failed')
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@qa_updates_bp.route('/api/<int:update_id>/delete', methods=['POST'])
@require_qa_manage
def api_delete(update_id):
    """Soft delete."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT title FROM qa_updates WHERE id = %s", (update_id,))
            row = cur.fetchone()
            if not row:
                return jsonify({'error': 'Not found'}), 404
            cur.execute("UPDATE qa_updates SET is_deleted = 1, updated_at = %s WHERE id = %s",
                        (datetime.now(), update_id))
            log_history(cur, update_id, 'is_deleted', 0, 1, session.get('user', {}).get('name'))
            conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@qa_updates_bp.route('/api/attachment/<int:attachment_id>/delete', methods=['POST'])
@require_qa_manage
def api_delete_attachment(attachment_id):
    conn = get_db()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("SELECT * FROM qa_update_attachments WHERE id = %s", (attachment_id,))
            att = cur.fetchone()
            if not att:
                return jsonify({'error': 'Not found'}), 404
            file_path = os.path.join(UPLOAD_DIR, att['filename'])
            if os.path.exists(file_path):
                os.remove(file_path)
            cur.execute("DELETE FROM qa_update_attachments WHERE id = %s", (attachment_id,))
            conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# API: acknowledgment (any logged-in user)
# ---------------------------------------------------------------------------
@qa_updates_bp.route('/api/unacknowledged-count')
@require_login
def api_unacknowledged_count():
    """
    Count of active QA updates the current user has NOT yet acknowledged.
    Used for the sidebar nav badge and the bulletin page header count.
    """
    employee_id = session.get('user', {}).get('employee_id')
    if not employee_id:
        return jsonify({'count': 0})

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT COUNT(*) AS cnt FROM qa_updates u
                   WHERE u.is_deleted = 0 AND u.is_active = 1
                   AND NOT EXISTS (
                       SELECT 1 FROM qa_update_acknowledgments a
                       WHERE a.qa_update_id = u.id
                       AND a.employee_id COLLATE utf8mb4_unicode_ci = %s COLLATE utf8mb4_unicode_ci
                   )""",
                (employee_id,)
            )
            row = cur.fetchone()
            count = row['cnt'] if isinstance(row, dict) else row[0]
        return jsonify({'count': count})
    finally:
        conn.close()


@qa_updates_bp.route('/api/<int:update_id>/acknowledge', methods=['POST'])
@require_login
def api_acknowledge(update_id):
    user = session.get('user', {})
    employee_id = user.get('employee_id')
    email = user.get('email')
    name = user.get('name')

    if not employee_id:
        return jsonify({'error': 'No employee_id on session.'}), 400

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM qa_updates WHERE id = %s AND is_deleted = 0", (update_id,))
            if not cur.fetchone():
                return jsonify({'error': 'Not found'}), 404
            try:
                cur.execute(
                    """INSERT INTO qa_update_acknowledgments
                       (qa_update_id, employee_id, email, name, acknowledged_at)
                       VALUES (%s,%s,%s,%s,%s)""",
                    (update_id, employee_id, email, name, datetime.now())
                )
                conn.commit()
            except pymysql.err.IntegrityError:
                conn.rollback()  # already acknowledged - idempotent

            cur.execute("SELECT COUNT(*) AS cnt FROM qa_update_acknowledgments WHERE qa_update_id = %s", (update_id,))
            row = cur.fetchone()
            count = row['cnt'] if isinstance(row, dict) else row[0]
        return jsonify({'success': True, 'ack_count': count})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@qa_updates_bp.route('/api/<int:update_id>/acknowledgments')
@require_qa_manage
def api_acknowledgment_list(update_id):
    """Admin-only: see who has acknowledged."""
    conn = get_db()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                """SELECT employee_id, email, name, acknowledged_at
                   FROM qa_update_acknowledgments
                   WHERE qa_update_id = %s ORDER BY acknowledged_at DESC""",
                (update_id,)
            )
            rows = cur.fetchall()
            for r in rows:
                if r.get('acknowledged_at'):
                    r['acknowledged_at'] = r['acknowledged_at'].isoformat()
        return jsonify({'acknowledgments': rows})
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# API: Data / Reports
# ---------------------------------------------------------------------------
@qa_updates_bp.route('/api/report/updates-list')
@require_qa_manage
def api_report_updates_list():
    """Lightweight list of updates for the report dropdown."""
    conn = get_db()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                """SELECT id, title, category, implementation_date, created_at
                   FROM qa_updates WHERE is_deleted = 0
                   ORDER BY created_at DESC"""
            )
            rows = cur.fetchall()
            for r in rows:
                if r.get('created_at'):
                    r['created_at'] = r['created_at'].isoformat()
                if r.get('implementation_date'):
                    r['implementation_date'] = r['implementation_date'].isoformat()
            return jsonify({'updates': rows})
    finally:
        conn.close()


@qa_updates_bp.route('/api/report/teams-list')
@require_qa_manage
def api_report_teams_list():
    """Canonical Team Leads from tl_view_map (not the free-text gsheet_employees.tl field)."""
    conn = get_db()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                """SELECT DISTINCT tl_name, group_name FROM tl_view_map
                   ORDER BY tl_name"""
            )
            rows = cur.fetchall()
            return jsonify({'teams': rows})
    finally:
        conn.close()


@qa_updates_bp.route('/api/report/overview')
@require_qa_manage
def api_report_overview():
    """
    The main micromanagement view: a TL x Update matrix.
    Rows = canonical TLs from tl_view_map, Columns = QA updates (most recent first,
    capped to a reasonable count), Cells = acked/total for that team on that update.
    """
    conn = get_db()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("SELECT DISTINCT tl_name FROM tl_view_map ORDER BY tl_name")
            tl_names = [r['tl_name'] for r in cur.fetchall()]

            cur.execute(
                """SELECT id, title, category, implementation_date, created_at
                   FROM qa_updates WHERE is_deleted = 0
                   ORDER BY created_at DESC LIMIT 15"""
            )
            updates = cur.fetchall()
            for u in updates:
                if u.get('created_at'):
                    u['created_at'] = u['created_at'].isoformat()
                if u.get('implementation_date'):
                    u['implementation_date'] = u['implementation_date'].isoformat()

            if not tl_names or not updates:
                return jsonify({'tls': [], 'updates': updates, 'matrix': {}})

            # Active members per TL (case-insensitive, trimmed match against
            # the free-text gsheet_employees.tl field)
            members_by_tl = {}
            for tl in tl_names:
                cur.execute(
                    """SELECT employee_id, schedule_name AS name FROM gsheet_employees
                       WHERE status = 'Active' AND TRIM(LOWER(tl)) = TRIM(LOWER(%s))""",
                    (tl,)
                )
                members_by_tl[tl] = cur.fetchall()

            matrix = {}
            for tl in tl_names:
                members = members_by_tl[tl]
                member_ids = [m['employee_id'] for m in members]
                matrix[tl] = {}
                for u in updates:
                    if not member_ids:
                        matrix[tl][u['id']] = {'acked': 0, 'total': 0, 'not_acked': []}
                        continue
                    cur.execute(
                        f"""SELECT employee_id FROM qa_update_acknowledgments
                            WHERE qa_update_id = %s
                            AND employee_id COLLATE utf8mb4_unicode_ci IN ({','.join(['%s']*len(member_ids))})""",
                        [u['id']] + member_ids
                    )
                    acked_ids = {r['employee_id'] for r in cur.fetchall()}
                    not_acked = [m['name'] for m in members if m['employee_id'] not in acked_ids]
                    matrix[tl][u['id']] = {
                        'acked': len(acked_ids),
                        'total': len(members),
                        'not_acked': not_acked,
                    }

            return jsonify({'tls': tl_names, 'updates': updates, 'matrix': matrix})
    finally:
        conn.close()


@qa_updates_bp.route('/api/report/by-update/<int:update_id>')
@require_qa_manage
def api_report_by_update(update_id):
    """
    For one QA update: every active employee, grouped by TL,
    with their acknowledgment status for that specific update.
    """
    conn = get_db()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("SELECT id, title FROM qa_updates WHERE id = %s", (update_id,))
            update_row = cur.fetchone()
            if not update_row:
                return jsonify({'error': 'Not found'}), 404

            # Scope to Operations TLs only (tl_view_map), matching the Overview
            # and Per Team views - not the full company roster.
            cur.execute("SELECT DISTINCT tl_name FROM tl_view_map")
            ops_tl_lookup = {r['tl_name'].strip().lower(): r['tl_name'] for r in cur.fetchall()}

            cur.execute(
                """SELECT e.employee_id, e.schedule_name AS name, e.tl, e.group_name,
                          a.acknowledged_at
                   FROM gsheet_employees e
                   LEFT JOIN qa_update_acknowledgments a
                       ON a.employee_id COLLATE utf8mb4_unicode_ci = e.employee_id COLLATE utf8mb4_unicode_ci
                       AND a.qa_update_id = %s
                   WHERE e.status = 'Active'
                       AND TRIM(LOWER(e.tl)) IN ({placeholders})
                   ORDER BY e.tl, e.schedule_name""".format(
                       placeholders=','.join(['%s'] * len(ops_tl_lookup)) if ops_tl_lookup else "''"
                   ),
                [update_id] + list(ops_tl_lookup.keys())
            )
            rows = cur.fetchall()

            teams = {}
            for r in rows:
                raw_tl = (r.get('tl') or '').strip()
                tl = ops_tl_lookup.get(raw_tl.lower(), raw_tl)
                teams.setdefault(tl, {'tl': tl, 'employees': [], 'acked_count': 0, 'total_count': 0})
                acked = r.get('acknowledged_at') is not None
                teams[tl]['employees'].append({
                    'employee_id': r['employee_id'],
                    'name': r['name'],
                    'group_name': r['group_name'],
                    'acknowledged': acked,
                    'acknowledged_at': r['acknowledged_at'].isoformat() if acked else None,
                })
                teams[tl]['total_count'] += 1
                if acked:
                    teams[tl]['acked_count'] += 1

            team_list = sorted(teams.values(), key=lambda t: t['tl'])
            overall_total = sum(t['total_count'] for t in team_list)
            overall_acked = sum(t['acked_count'] for t in team_list)

            return jsonify({
                'update': update_row,
                'teams': team_list,
                'overall_total': overall_total,
                'overall_acked': overall_acked,
            })
    finally:
        conn.close()


@qa_updates_bp.route('/api/report/by-team/<string:tl_name>')
@require_qa_manage
def api_report_by_team(tl_name):
    """
    For one Team Lead: every QA update, with this team's completion
    (acked / total active members under that TL) for each.
    """
    conn = get_db()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                """SELECT employee_id, schedule_name AS name FROM gsheet_employees
                   WHERE status = 'Active' AND TRIM(LOWER(tl)) = TRIM(LOWER(%s))
                   ORDER BY schedule_name""",
                (tl_name,)
            )
            members = cur.fetchall()
            if not members:
                return jsonify({'tl': tl_name, 'members': [], 'updates': []})

            member_ids = [m['employee_id'] for m in members]

            cur.execute(
                """SELECT id, title, category, implementation_date, created_at
                   FROM qa_updates WHERE is_deleted = 0
                   ORDER BY created_at DESC"""
            )
            updates = cur.fetchall()

            results = []
            for u in updates:
                cur.execute(
                    f"""SELECT employee_id FROM qa_update_acknowledgments
                        WHERE qa_update_id = %s
                        AND employee_id COLLATE utf8mb4_unicode_ci IN ({','.join(['%s']*len(member_ids))})""",
                    [u['id']] + member_ids
                )
                acked_ids = {r['employee_id'] for r in cur.fetchall()}
                not_acked = [m['name'] for m in members if m['employee_id'] not in acked_ids]

                results.append({
                    'id': u['id'],
                    'title': u['title'],
                    'category': u['category'],
                    'implementation_date': u['implementation_date'].isoformat() if u.get('implementation_date') else None,
                    'created_at': u['created_at'].isoformat() if u.get('created_at') else None,
                    'acked_count': len(acked_ids),
                    'total_count': len(members),
                    'not_acked_names': not_acked,
                })

            return jsonify({'tl': tl_name, 'members': members, 'updates': results})
    finally:
        conn.close()



@require_login
def serve_attachment(filename):
    from flask import send_from_directory
    safe_filename = secure_filename(filename)
    return send_from_directory(UPLOAD_DIR, safe_filename)