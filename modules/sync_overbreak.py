import os
from flask import Blueprint, request, jsonify
from db_core import get_db_connection

sync_overbreak_bp = Blueprint('sync_overbreak_bp', __name__)

OVERBREAK_SYNC_KEY = os.environ.get('OVERBREAK_SYNC_KEY')


@sync_overbreak_bp.route('/api/sync/overbreak', methods=['POST'])
def sync_overbreak():
    if not OVERBREAK_SYNC_KEY or request.headers.get('X-Sync-Key') != OVERBREAK_SYNC_KEY:
        return jsonify({'error': 'unauthorized'}), 401

    body = request.get_json(silent=True) or {}
    records = body.get('records', [])
    if not records:
        return jsonify({'status': 'no records'}), 200

    conn = get_db_connection()
    cur = conn.cursor()

    sql = """
        INSERT INTO overbreak_records
            (row_uid, record_date, agent_name, employee_id, break_duration,
             validity, submitted, payroll_month, payroll_cycle, tl,
             duplicate_count, record_month, record_year, batch, incident_report_info)
        VALUES (%(row_uid)s, %(date)s, %(agent_name)s, %(employee_id)s, %(break_duration)s,
                %(validity)s, %(submitted)s, %(payroll_month)s, %(payroll_cycle)s, %(tl)s,
                %(duplicate_count)s, %(record_month)s, %(record_year)s, %(batch)s, %(incident_report_info)s)
        ON DUPLICATE KEY UPDATE
            record_date = VALUES(record_date),
            agent_name = VALUES(agent_name),
            employee_id = VALUES(employee_id),
            break_duration = VALUES(break_duration),
            validity = VALUES(validity),
            submitted = VALUES(submitted),
            payroll_month = VALUES(payroll_month),
            payroll_cycle = VALUES(payroll_cycle),
            tl = VALUES(tl),
            duplicate_count = VALUES(duplicate_count),
            record_month = VALUES(record_month),
            record_year = VALUES(record_year),
            batch = VALUES(batch),
            incident_report_info = VALUES(incident_report_info)
    """

    synced = 0
    skipped = 0
    for r in records:
        if not r.get('row_uid') or not r.get('date') or not r.get('agent_name'):
            skipped += 1
            continue
        cur.execute(sql, {
            'row_uid': r.get('row_uid'),
            'date': r.get('date'),
            'agent_name': r.get('agent_name'),
            'employee_id': r.get('employee_id') or None,
            'break_duration': r.get('break_duration') or None,
            'validity': r.get('validity'),
            'submitted': r.get('submitted'),
            'payroll_month': r.get('payroll_month'),
            'payroll_cycle': r.get('payroll_cycle'),
            'tl': r.get('tl'),
            'duplicate_count': r.get('duplicate_count') or 0,
            'record_month': r.get('record_month'),
            'record_year': r.get('record_year') or None,
            'batch': r.get('batch'),
            'incident_report_info': r.get('incident_report_info'),
        })
        synced += 1

    conn.commit()
    cur.close()
    conn.close()

    return jsonify({'status': 'ok', 'synced': synced, 'skipped': skipped}), 200