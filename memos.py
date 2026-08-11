"""
Employee Memos Blueprint
Read-only API for employee disciplinary/HR memos, displayed on the PIM profile page,
plus a standalone filterable list page for all memos.

The employee_memos table is populated by an existing sync process (Google Sheets
AppScript -> webhook.cohere.ph) that already writes into this same central_db.
That sync is untouched - this Blueprint only reads from the table.

Standalone Blueprint - does NOT import from app.py.
"""
from flask import Blueprint, jsonify, session, request, render_template, redirect
from functools import wraps
import logging
import math
import os
from datetime import datetime

import pymysql.cursors
from db_core import get_db_connection

logger = logging.getLogger(__name__)

memos_bp = Blueprint('memos', __name__)

PER_PAGE = 50
MEMO_SYNC_SECRET = os.environ.get('MEMO_SYNC_SECRET')


# ---------- Local auth decorators (Blueprint pattern - no app.py imports) ----------

def login_required(f):
    """For JSON API endpoints - returns 401 JSON if not authenticated."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user' not in session:
            return jsonify({'error': 'Not authenticated'}), 401
        return f(*args, **kwargs)
    return decorated


def require_secret_key(f):
    """For the inbound sync webhook - checks a shared secret instead of a session."""
    @wraps(f)
    def decorated(*args, **kwargs):
        provided = request.headers.get('X-Secret-Key') or request.args.get('key')
        if not MEMO_SYNC_SECRET or provided != MEMO_SYNC_SECRET:
            logger.warning('Memo sync webhook: rejected request with invalid/missing secret key')
            return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated


def page_login_required(f):
    """For HTML page endpoints - redirects to home (SSO) if not authenticated."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user' not in session:
            return redirect('/')
        return f(*args, **kwargs)
    return decorated


def can_view_memos():
    """can_memos permission check - full admins always pass."""
    if session.get('is_admin'):
        return True
    perms = session.get('permissions', {}) or {}
    return bool(perms.get('can_memos'))


def _parse_memo_date(raw):
    if not raw:
        return None
    if isinstance(raw, str):
        if 'T' in raw:
            raw = raw.split('T')[0]
            try:
                return datetime.strptime(raw, '%Y-%m-%d').date()
            except ValueError:
                pass
        for fmt in ('%m/%d/%Y', '%Y-%m-%d', '%d/%m/%Y'):
            try:
                return datetime.strptime(raw, fmt).date()
            except ValueError:
                continue
    return None


# ---------- Inbound sync webhook (new Apps Script -> here) ----------

@memos_bp.route('/webhook/memo-sync', methods=['POST'])
@require_secret_key
def memo_sync_webhook():
    try:
        payload = request.get_json()
        if not payload:
            return jsonify({'error': 'No data provided'}), 400

        memos = payload.get('memos', [])
        total = payload.get('total', len(memos))
        logger.info(f'Memo sync received - total: {total}')

        conn = get_db_connection()
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        cursor.execute("SELECT email, employee_id FROM gsheet_employees")
        email_to_id = {
            (row.get('email') or '').strip().lower(): row.get('employee_id')
            for row in cursor.fetchall()
        }

        cursor.execute("DELETE FROM employee_memos")
        deleted_count = cursor.rowcount
        logger.info(f'Cleared {deleted_count} existing memos')

        insert_sql = """
            INSERT INTO employee_memos
            (memo_date, employee_id, last_name, first_name, email, supervisor,
             category, violation, disciplinary_action, details)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """

        success_count = 0
        error_count = 0
        errors = []
        skipped_no_date = 0

        for memo in memos:
            try:
                parsed_date = _parse_memo_date(memo.get('Date of Memo'))
                if not parsed_date:
                    skipped_no_date += 1
                    continue

                email = (memo.get('Email') or '').strip()
                employee_id = email_to_id.get(email.lower()) if email else None

                cursor.execute(insert_sql, (
                    parsed_date,
                    employee_id,
                    memo.get('Last Name'),
                    memo.get('First Name'),
                    email,
                    memo.get('Supervisor'),
                    memo.get('Category'),
                    memo.get('Violation'),
                    memo.get('Disciplinary Action'),
                    memo.get('Details'),
                ))
                success_count += 1
            except Exception as e:
                error_count += 1
                errors.append(f"Row ({memo.get('Email', 'unknown')}): {e}")
                logger.error(f'Memo row insert failed: {e}')

        conn.commit()
        conn.close()

        logger.info(
            f'Memo sync completed - success: {success_count}, errors: {error_count}, '
            f'skipped (no valid date): {skipped_no_date}'
        )
        return jsonify({
            'status': 'success',
            'message': 'Memo sync completed',
            'success_count': success_count,
            'error_count': error_count,
            'skipped_no_date': skipped_no_date,
            'errors': errors[:10],
        }), 200

    except Exception as e:
        logger.error(f'Error processing memo webhook: {e}', exc_info=True)
        return jsonify({'status': 'error', 'message': str(e)}), 500


# ---------- Read API (used by PIM profile page) ----------

@memos_bp.route('/api/memos/<employee_id>')
@login_required
def get_employee_memos(employee_id):
    if not can_view_memos():
        return jsonify({'error': 'Forbidden'}), 403

    try:
        conn = get_db_connection()
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        cursor.execute("""
            SELECT memo_date, category, violation, disciplinary_action, details, supervisor
            FROM employee_memos
            WHERE employee_id = %s
            ORDER BY memo_date DESC
        """, [employee_id])

        memos = cursor.fetchall()
        for memo in memos:
            if memo.get('memo_date'):
                memo['memo_date'] = memo['memo_date'].strftime('%Y-%m-%d')

        conn.close()
        return jsonify(memos)

    except Exception as e:
        logger.error(f'Error fetching employee memos: {e}')
        return jsonify({'error': str(e)}), 500


# ---------- Standalone filterable list page ----------

@memos_bp.route('/memos')
@page_login_required
def memos_list():
    if not can_view_memos():
        return "Access denied - you don't have permission to view Employee Memos.", 403

    category = request.args.get('category', '').strip()
    supervisor = request.args.get('supervisor', '').strip()
    date_from = request.args.get('date_from', '').strip()
    date_to = request.args.get('date_to', '').strip()
    q = request.args.get('q', '').strip()
    page = max(int(request.args.get('page', 1) or 1), 1)

    where = []
    params = []

    if category:
        where.append("category = %s")
        params.append(category)
    if supervisor:
        where.append("supervisor = %s")
        params.append(supervisor)
    if date_from:
        where.append("memo_date >= %s")
        params.append(date_from)
    if date_to:
        where.append("memo_date <= %s")
        params.append(date_to)
    if q:
        where.append("""
            (last_name LIKE %s OR first_name LIKE %s OR email LIKE %s
             OR employee_id LIKE %s OR CONCAT(first_name, ' ', last_name) LIKE %s)
        """)
        like = f"%{q}%"
        params.extend([like, like, like, like, like])

    where_clause = f"WHERE {' AND '.join(where)}" if where else ""

    try:
        conn = get_db_connection()
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        # Total count for pagination
        cursor.execute(f"SELECT COUNT(*) AS total FROM employee_memos {where_clause}", params)
        total = cursor.fetchone()['total']
        total_pages = max(math.ceil(total / PER_PAGE), 1)
        page = min(page, total_pages)
        offset = (page - 1) * PER_PAGE

        cursor.execute(f"""
            SELECT memo_date, employee_id, first_name, last_name, email, supervisor,
                   category, violation, disciplinary_action, details
            FROM employee_memos
            {where_clause}
            ORDER BY memo_date DESC
            LIMIT %s OFFSET %s
        """, params + [PER_PAGE, offset])
        memos = cursor.fetchall()
        for m in memos:
            if m.get('memo_date'):
                m['memo_date'] = m['memo_date'].strftime('%Y-%m-%d')

        # Distinct values for filter dropdowns
        cursor.execute("SELECT DISTINCT category FROM employee_memos WHERE category IS NOT NULL AND category != '' ORDER BY category")
        categories = [r['category'] for r in cursor.fetchall()]

        cursor.execute("SELECT DISTINCT supervisor FROM employee_memos WHERE supervisor IS NOT NULL AND supervisor != '' ORDER BY supervisor")
        supervisors = [r['supervisor'] for r in cursor.fetchall()]

        conn.close()

        return render_template(
            'memos/index.html',
            memos=memos,
            categories=categories,
            supervisors=supervisors,
            total=total,
            page=page,
            total_pages=total_pages,
            filters={
                'category': category,
                'supervisor': supervisor,
                'date_from': date_from,
                'date_to': date_to,
                'q': q,
            }
        )

    except Exception as e:
        logger.error(f'Error loading memos list page: {e}', exc_info=True)
        return f"Error loading memos: {e}", 500