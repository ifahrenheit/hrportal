"""
PIM profile, extra tabs ("Employee 360") -- /pim/<employee_id>/tab/<name>

PIM (/pim/<employee_id>, app.py pim_profile) already shows the profile,
schedule, attendance summary, CSAT, leaves, file requests, leave balance,
punches and memos. These tabs add what was missing, each loaded only when
clicked (so PIM itself doesn't get slower):

  discipline  incident reports about the employee + undertime per payroll period
  coaching    coaching sessions where the employee is the agent
  documents   files in the Documents library tagged to the employee + CoE requests

Access: the same as PIM (admins, can_pim). On top of that each tab follows its
own feature's rules, so PIM never shows more than the feature itself would:
  - IRs:        modules.incident_reports.report_scope_conds (what the viewer's
                IR dashboard lists)
  - Coaching:   coaching.can_coaching / can_reports + coaching._scope
  - Documents:  modules.documents.docs_can_access + _visible_levels
  - CoE:        modules.coe_requests.coe_can_manage
A tab whose feature the viewer can't use isn't offered (pim360_tabs()).

app.py runs as __main__, so this module must not `from app import ...` at
import time. init_pim_360() is called from app.py and hands over get_central_db.
"""
import json
from datetime import date

from flask import Blueprint, abort, render_template, session, url_for

pim360_bp = Blueprint('pim360', __name__)

_h = {}

IR_STATUS = {
    'pending': ('Pending', 'warn'), 'reviewed': ('Reviewed', 'info'),
    'rwe_request': ('RWE requested', 'warn'), 'rwe_for_signature': ('RWE for signature', 'warn'),
    'rwe_for_service': ('RWE for service', 'warn'), 'rwe_served': ('RWE served', 'info'),
    'awaiting_explanation': ('Awaiting explanation', 'warn'), 'forwarded': ('Forwarded', 'info'),
    'for_memo': ('For memo', 'warn'), 'resolved': ('Resolved', 'ok'), 'waived': ('Waived', 'muted'),
}
COACHING_STATUS = {   # coaching.STATUSES / STATUS_LABELS_ALL
    'completed': ('Completed', 'ok'), 'cancelled': ('Cancelled', 'muted'), 'draft': ('Draft', 'muted'),
    'pending': ('Pending', 'warn'), 'for_followup': ('For follow-up', 'warn'),
    'pending_followup': ('Pending follow-up', 'warn'),
}


def init_pim_360(**helpers):
    """helpers: get_central_db."""
    _h.update(helpers)


def can_open_pim():
    return bool(session.get('is_admin') or (session.get('permissions') or {}).get('can_pim'))


def _can_coaching():
    import coaching
    return coaching.can_coaching() or coaching.can_reports()


def _can_documents():
    from modules.documents import docs_can_access
    return docs_can_access()


def _can_coe():
    from modules.coe_requests import coe_can_manage
    return coe_can_manage()


def pim360_tabs():
    """Tabs this viewer gets on a PIM profile (Jinja global)."""
    if not can_open_pim():
        return []
    tabs = [('discipline', 'Discipline', 'shield-alert')]
    if _can_coaching():
        tabs.append(('coaching', 'Coaching', 'messages-square'))
    if _can_documents() or _can_coe():
        tabs.append(('documents', 'Documents', 'folder-open'))
    return tabs


def _q(conn, sql, params):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


# ──────────────────────────────────────────────────────────────────
# Data
# ──────────────────────────────────────────────────────────────────
def incident_reports_for(conn, employee_id, scope_conds, scope_params):
    """IRs about this employee, limited to what the viewer's IR dashboard shows."""
    rows = _q(conn, f"""
        SELECT ir.report_number, ir.incident_date, ir.summary, ir.status, ir.memo_category,
               ir.disciplinary_action, ir.submitted_by_name, ir.created_at
        FROM incident_reports ir
        WHERE ir.employee_id = %s AND {' AND '.join(scope_conds)}
        ORDER BY ir.incident_date DESC, ir.created_at DESC
    """, [employee_id] + list(scope_params))
    for r in rows:
        label, tone = IR_STATUS.get(r['status'] or '', ((r['status'] or '—').replace('_', ' ').title(), 'muted'))
        r['status_label'], r['status_tone'] = label, tone
    return rows


def _breakdown_days(raw):
    try:
        items = json.loads(raw) if raw else []
        return [i.get('date') for i in items if isinstance(i, dict) and i.get('date')]
    except (ValueError, TypeError):
        return []


def undertime_periods_for(conn, employee_id):
    """Undertime per payroll period, from undertime_notify.py's cycle state.
    Totals are complete; the day list is cleared whenever a memo is sent, so it
    only holds the days since the last memo."""
    from helpers.payroll_period import get_payroll_period_for_date
    rows = _q(conn, """
        SELECT period_start, total_count_in_cycle, total_minutes_in_cycle,
               count_triggers_sent, minutes_triggers_sent, count_breakdown, minutes_breakdown
        FROM undertime_cycle_state
        WHERE employee_id = %s AND total_count_in_cycle > 0
        ORDER BY period_start DESC
    """, [employee_id])
    out = []
    for r in rows:
        start = r['period_start']
        _, end = get_payroll_period_for_date(start)
        days = sorted(set(_breakdown_days(r['count_breakdown']) + _breakdown_days(r['minutes_breakdown'])))
        out.append({'start': start, 'end': end, 'count': r['total_count_in_cycle'] or 0,
                    'minutes': r['total_minutes_in_cycle'] or 0,
                    'memos': (r['count_triggers_sent'] or 0) + (r['minutes_triggers_sent'] or 0),
                    'recent_days': days})
    return out


def coaching_for(conn, employee_id, scope_sql, scope_params):
    rows = _q(conn, f"""
        SELECT cs.id, cs.session_date, cs.coaching_type, cs.topic, cs.status, cs.follow_up_date,
               cs.supervisor_id, COALESCE(g.schedule_name, cs.supervisor_id) AS coach
        FROM coaching_sessions cs
        LEFT JOIN gsheet_employees g
               ON g.employee_id COLLATE utf8mb4_unicode_ci = cs.supervisor_id COLLATE utf8mb4_unicode_ci
        WHERE cs.agent_id COLLATE utf8mb4_unicode_ci = %s {scope_sql}
        ORDER BY cs.session_date DESC, cs.id DESC
    """, [employee_id] + list(scope_params))
    for r in rows:
        label, tone = COACHING_STATUS.get(r['status'] or '', ((r['status'] or '—').replace('_', ' ').title(), 'muted'))
        r['status_label'], r['status_tone'] = label, tone
    return rows


def documents_for(conn, employee_id, levels):
    ph = ','.join(['%s'] * len(levels))
    return _q(conn, f"""
        SELECT id, title, category, doc_date, visibility, original_name, file_size, uploaded_at
        FROM hr_documents
        WHERE employee_id = %s AND deleted_at IS NULL AND visibility IN ({ph})
        ORDER BY COALESCE(doc_date, DATE(uploaded_at)) DESC, id DESC
    """, [employee_id] + list(levels))


def coe_for(conn, employee_id):
    return _q(conn, """
        SELECT id, purpose, purpose_other, addressee, status, requested_at, processed_by_name,
               released_at, released_via
        FROM coe_requests
        WHERE employee_id = %s AND (is_deleted = 0 OR is_deleted IS NULL)
        ORDER BY requested_at DESC
    """, [employee_id])


# ──────────────────────────────────────────────────────────────────
# Route
# ──────────────────────────────────────────────────────────────────
@pim360_bp.route('/pim/<employee_id>/tab/<name>')
def tab(employee_id, name):
    if 'user' not in session:
        abort(401)
    if not can_open_pim():
        abort(403)
    if name not in {t[0] for t in pim360_tabs()}:
        abort(403)
    conn = _h['get_central_db']()
    try:
        if not _q(conn, "SELECT 1 FROM gsheet_employees WHERE employee_id = %s", [employee_id]):
            abort(404)
        ctx = {'employee_id': employee_id, 'name': name, 'today': date.today()}
        if name == 'discipline':
            from modules.incident_reports import ir_user, report_scope_conds
            conds, params = report_scope_conds(ir_user(), conn)
            ctx['irs'] = incident_reports_for(conn, employee_id, conds, params)
            ctx['undertime'] = undertime_periods_for(conn, employee_id)
        elif name == 'coaching':
            import coaching
            scope_sql, scope_params = coaching._scope('cs')
            ctx['sessions'] = coaching_for(conn, employee_id, scope_sql, scope_params)
            ctx['coaching_all'] = coaching.can_reports()
        elif name == 'documents':
            ctx['show_docs'] = _can_documents()
            ctx['show_coe'] = _can_coe()
            if ctx['show_docs']:
                from modules.documents import _visible_levels
                ctx['hr_hidden'] = 'hr' not in _visible_levels()
                ctx['docs'] = documents_for(conn, employee_id, _visible_levels())
            if ctx['show_coe']:
                ctx['coes'] = coe_for(conn, employee_id)
    finally:
        conn.close()
    return render_template('pim/_tab_360.html', **ctx)
