"""Endpoints moved from the old employee-sync service (UMMS-Core webapp,
webhook.cohere.ph) so the portal is the only app to run.

  POST /webhook/employee-sync   Apps Script -> gsheet_employees
                                (actions: check_sync_needed, batch_sync, single row)
  POST /webhook/schedule-sync   Apps Script -> employee_schedules
  POST /webhook/break-autofix   Apps Script -> break_logs.shift_date fix for overnight shifts
  GET  /api/biometric/pending   Blade Server (office PCs) polls new hires to enroll
  POST /api/biometric/complete  Blade Server marks them completed / failed
  GET  /health                  DB check

The /webhook/* routes need the X-Secret-Key header to match WEBHOOK_SECRET_KEY
in .env (the Apps Script sends it). The biometric routes were open in the old
service and stay that way so the office PCs keep working. Requests and
responses are the same as the old service, so callers only needed the
webhook.cohere.ph vhost pointed at this app.
"""
import hmac
import logging
import os
import re
from datetime import datetime, timedelta
from functools import wraps

from flask import Blueprint, jsonify, request

from db_core import get_db_connection

sheet_webhooks_bp = Blueprint('sheet_webhooks', __name__)
log = logging.getLogger('sheet_webhooks')
if not log.handlers:
    _fh = logging.FileHandler(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                           'sheet_webhooks.log'))
    _fh.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    log.addHandler(_fh)
    log.setLevel(logging.INFO)


def require_secret_key(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        expected = os.environ.get('WEBHOOK_SECRET_KEY', '')
        provided = request.headers.get('X-Secret-Key', '')
        if not expected or not hmac.compare_digest(provided, expected):
            log.warning(f'Unauthorized webhook call to {request.path} from {request.remote_addr}')
            return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated


# ─── Employee sheet -> gsheet_employees ───────────────────────────────────────
BATCH_UPSERT = """
    INSERT INTO gsheet_employees (
        employee_id, schedule_name, email, birthdate,
        training_date, operational_date, exit_date, status, tl,
        group_name, account, batch, wfh, task_force, sme, approver,
        mailing_list
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON DUPLICATE KEY UPDATE
        schedule_name=VALUES(schedule_name), email=VALUES(email),
        birthdate=VALUES(birthdate), training_date=VALUES(training_date),
        operational_date=VALUES(operational_date), exit_date=VALUES(exit_date),
        status=VALUES(status), tl=VALUES(tl), group_name=VALUES(group_name),
        account=VALUES(account), batch=VALUES(batch),
        wfh=VALUES(wfh), task_force=VALUES(task_force), sme=VALUES(sme),
        approver=VALUES(approver),
        mailing_list=VALUES(mailing_list),
        last_synced_at=CURRENT_TIMESTAMP
"""

SINGLE_UPSERT = """
    INSERT INTO gsheet_employees (
        employee_id, schedule_name, email, phone, location, birthdate,
        training_date, operational_date, exit_date, status, tl,
        group_name, account, batch, wfh, task_force, sme, approver
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON DUPLICATE KEY UPDATE
        schedule_name=VALUES(schedule_name), email=VALUES(email),
        phone=VALUES(phone), location=VALUES(location),
        birthdate=VALUES(birthdate), training_date=VALUES(training_date),
        operational_date=VALUES(operational_date), exit_date=VALUES(exit_date),
        status=VALUES(status), tl=VALUES(tl), group_name=VALUES(group_name),
        account=VALUES(account), batch=VALUES(batch),
        wfh=VALUES(wfh), task_force=VALUES(task_force), sme=VALUES(sme),
        approver=VALUES(approver),
        last_synced_at=CURRENT_TIMESTAMP
"""

RESYNC_AFTER_SECONDS = 6 * 3600


def _batch_values(data):
    return (
        data.get('Employee ID'), data.get('Schedule Name'), data.get('Email'),
        data.get('birthdate'), data.get('training_date'), data.get('operational_date'),
        data.get('exit_date'), data.get('Status'), data.get('TL'), data.get('Group'),
        data.get('Account'), data.get('Batch'),
        1 if data.get('WFH') else 0,
        1 if data.get('task_force') else 0,
        1 if data.get('sme') else 0,
        data.get('Approver') or None,
        # Read by scripts/mailing_list_sheet_sync.py
        str(data.get('Mailing List') or '').strip()[:500] or None,
    )


def _single_values(data):
    return (
        data.get('Employee ID'), data.get('Schedule Name'), data.get('Email'),
        data.get('phone'), data.get('location'), data.get('birthdate'),
        data.get('training_date'), data.get('operational_date'), data.get('exit_date'),
        data.get('Status'), data.get('TL'), data.get('Group'),
        data.get('Account'), data.get('Batch'),
        1 if data.get('WFH') else 0,
        1 if data.get('task_force') else 0,
        1 if data.get('sme') else 0,
        data.get('Approver') or None,
    )


@sheet_webhooks_bp.route('/webhook/employee-sync', methods=['POST'])
@require_secret_key
def employee_sync():
    payload = request.get_json(silent=True)
    if not payload:
        return jsonify({'error': 'No data provided'}), 400
    action = payload.get('action')
    conn = None
    try:
        if action == 'check_sync_needed':
            employee_ids = payload.get('employee_ids', [])
            existing = {}
            if employee_ids:
                conn = get_db_connection()
                with conn.cursor() as cur:
                    marks = ','.join(['%s'] * len(employee_ids))
                    cur.execute(f"SELECT employee_id, last_synced_at FROM gsheet_employees "
                                f"WHERE employee_id IN ({marks})", employee_ids)
                    existing = {r['employee_id']: r['last_synced_at'] for r in cur.fetchall()}
            now = datetime.now()
            to_sync = [e for e in employee_ids
                       if existing.get(e) is None
                       or (now - existing[e]).total_seconds() > RESYNC_AFTER_SECONDS]
            log.info(f'Check sync: {len(to_sync)} of {len(employee_ids)} need sync')
            return jsonify({'status': 'success', 'employees_to_sync': to_sync}), 200

        if action == 'batch_sync':
            employees = payload.get('employees', [])
            if employees and isinstance(employees[0], dict) and 'Mailing List' not in employees[0]:
                log.warning(f'Batch sync: no "Mailing List" field; keys: {list(employees[0].keys())}')
            success = errors = 0
            conn = get_db_connection()
            with conn.cursor() as cur:
                for data in employees:
                    if not data or not isinstance(data, dict):
                        errors += 1
                        log.warning(f'Skipping invalid employee data: {data}')
                        continue
                    try:
                        cur.execute(BATCH_UPSERT, _batch_values(data))
                        success += 1
                    except Exception as e:
                        log.error(f'Error syncing {data.get("Employee ID", "unknown")}: {e}')
                        errors += 1
            conn.commit()
            log.info(f'Batch sync: {success} synced, {errors} errors')
            return jsonify({'status': 'success', 'synced': success, 'errors': errors}), 200

        data = payload.get('data')
        if not isinstance(data, dict) or not data.get('Employee ID'):
            return jsonify({'error': 'No employee data provided'}), 400
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(SINGLE_UPSERT, _single_values(data))
        conn.commit()
        log.info(f'Synced employee: {data.get("Employee ID")} - {data.get("Schedule Name")}')
        return jsonify({'status': 'success', 'message': 'Employee synced'}), 200
    except Exception as e:
        log.error(f'Error syncing employee: {e}', exc_info=True)
        if conn:
            conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        if conn:
            conn.close()


# ─── Schedule sheet -> employee_schedules ────────────────────────────────────
@sheet_webhooks_bp.route('/webhook/schedule-sync', methods=['POST'])
@require_secret_key
def schedule_sync():
    payload = request.get_json(silent=True)
    if not payload:
        return jsonify({'error': 'No data provided'}), 400
    schedules = payload.get('schedules', [])
    log.info(f'Received schedule sync request - Total schedules: {payload.get("total", len(schedules))}')
    conn = None
    try:
        conn = get_db_connection()
        success_count = error_count = 0
        errors = []
        with conn.cursor() as cur:
            # Replace only the employee+date pairs in this payload
            pairs = [(s.get('employee_id'), s.get('schedule_date')) for s in schedules
                     if s.get('employee_id') and s.get('schedule_date')]
            if pairs:
                cur.executemany("DELETE FROM employee_schedules WHERE employee_id = %s AND schedule_date = %s", pairs)
                log.info(f'Cleared {cur.rowcount} existing schedule entries')
            for s in schedules:
                try:
                    schedule_date = s.get('schedule_date')
                    if not schedule_date:
                        error_count += 1
                        errors.append(f"Missing schedule_date for {s.get('employee_id')}")
                        continue
                    cur.execute("""
                        INSERT INTO employee_schedules
                            (employee_id, schedule_date, shift_time, is_rest_day, month)
                        VALUES (%s, %s, %s, %s, %s)
                    """, (s['employee_id'], s['schedule_date'], s['shift_time'],
                          s['is_rest_day'], schedule_date[:7]))
                    success_count += 1
                except Exception as e:
                    error_count += 1
                    msg = (f"Schedule for {s.get('employee_id', 'unknown')} on "
                           f"{s.get('schedule_date', 'unknown')}: {e}")
                    errors.append(msg)
                    log.error(msg)
        conn.commit()
        log.info(f'Schedule sync completed - Success: {success_count}, Errors: {error_count}')
        return jsonify({'status': 'success', 'message': 'Schedule sync completed',
                        'success_count': success_count, 'error_count': error_count,
                        'errors': errors[:10]}), 200
    except Exception as e:
        log.error(f'Error processing schedule webhook: {e}', exc_info=True)
        if conn:
            conn.rollback()
        return jsonify({'status': 'error', 'message': str(e)}), 500
    finally:
        if conn:
            conn.close()


# ─── Break log shift_date fix ────────────────────────────────────────────────
def _shift_minutes(shift_time):
    """'5am-2pm' -> (300, 840); handles '5:30pm', '12mn' (midnight), '12nn'
    (noon). Returns None for anything that isn't a shift ('VL', 'LWOP')."""
    if not shift_time or '-' not in shift_time:
        return None
    out = []
    for part in shift_time.strip().split('-', 1):
        m = re.match(r'^(\d{1,2})(?::(\d{2}))?\s*(am|pm|mn|nn)$', part.strip(), re.I)
        if not m:
            return None
        h, mins, suf = int(m[1]), int(m[2] or 0), m[3].lower()
        if h > 12 or mins > 59:
            return None
        if suf == 'mn' or (suf == 'am' and h == 12):
            h = 0 if h == 12 else h
        elif suf == 'pm' and h != 12:
            h += 12
        out.append(h * 60 + mins)
    return out[0], out[1]


@sheet_webhooks_bp.route('/webhook/break-autofix', methods=['POST'])
@require_secret_key
def break_autofix():
    data = request.get_json(silent=True) or {}
    date_from = data.get('date_from', (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d'))
    if data.get('action', '') != 'auto_fix_breaks':
        return jsonify({'success': False, 'error': 'Invalid action'}), 400
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            # Candidates: breaks before 9 AM still dated to their calendar day.
            # Only move one back to the previous day when it belongs to an
            # overnight shift. If that day's own schedule is a daytime shift that
            # had already started (e.g. 5am-2pm, 1am-12nn), the break belongs to
            # that day and is left alone.
            cur.execute("""
                SELECT bl.id, bl.break_start, es.shift_time, es.is_rest_day
                FROM break_logs bl
                LEFT JOIN employee_schedules es
                  ON es.employee_id = bl.employee_id
                 AND es.schedule_date = DATE(bl.break_start)
                WHERE bl.account_type IN ('Numa', 'Arctic', 'GYG')
                  AND bl.break_start >= %s
                  AND HOUR(bl.break_start) < 9
                  AND bl.shift_date = DATE(bl.break_start)
            """, (date_from,))
            to_fix = []
            for row in cur.fetchall():
                window = None if row['is_rest_day'] else _shift_minutes(row['shift_time'])
                if window:
                    start_min, end_min = window
                    break_min = row['break_start'].hour * 60 + row['break_start'].minute
                    if start_min <= end_min and break_min >= start_min:
                        continue  # daytime shift already running -> correct as is
                to_fix.append(row['id'])
            fixed_count = 0
            if to_fix:
                cur.execute(
                    "UPDATE break_logs SET shift_date = DATE_SUB(shift_date, INTERVAL 1 DAY) "
                    f"WHERE id IN ({','.join(['%s'] * len(to_fix))}) AND shift_date = DATE(break_start)",
                    to_fix)
                fixed_count = cur.rowcount
        conn.commit()
        log.info(f'Break auto-fix: {fixed_count} breaks corrected (from {date_from})')
        return jsonify({'success': True, 'message': 'Auto-fix completed',
                        'fixed_count': fixed_count, 'date_from': date_from,
                        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')})
    except Exception as e:
        log.error(f'Break auto-fix error: {e}')
        if conn:
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn:
            conn.close()


# ─── Biometric enrollment queue (filled by the portal's new-hire flow) ───────
@sheet_webhooks_bp.route('/api/biometric/pending', methods=['GET'])
def biometric_pending():
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, employee_id, companyid, personid, username, fname, lname, gid
                FROM pending_biometric_sync
                WHERE status = 'pending'
                ORDER BY created_at ASC
                LIMIT 100
            """)
            records = cur.fetchall()
        return jsonify({'records': records}), 200
    except Exception as e:
        log.error(f'Error fetching pending biometric: {e}')
        return jsonify({'error': str(e)}), 500
    finally:
        if conn:
            conn.close()


@sheet_webhooks_bp.route('/api/biometric/complete', methods=['POST'])
def biometric_complete():
    data = request.get_json(silent=True) or {}
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            for record_id in data.get('completed_ids') or []:
                cur.execute("UPDATE pending_biometric_sync SET status = 'completed', processed_at = NOW() "
                            "WHERE id = %s", (record_id,))
            for failure in data.get('failed') or []:
                cur.execute("UPDATE pending_biometric_sync SET status = 'failed', processed_at = NOW(), "
                            "error_message = %s WHERE id = %s", (failure['error'], failure['id']))
        conn.commit()
        return jsonify({'success': True}), 200
    except Exception as e:
        log.error(f'Error marking biometric complete: {e}')
        if conn:
            conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        if conn:
            conn.close()


@sheet_webhooks_bp.route('/health', methods=['GET'])
def health():
    try:
        get_db_connection().close()
        return jsonify({'status': 'healthy', 'database': 'connected'}), 200
    except Exception:
        return jsonify({'status': 'unhealthy', 'database': 'disconnected'}), 503
