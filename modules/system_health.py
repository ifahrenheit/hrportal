"""
Admin > System Health -- /admin/system-health (full admins only)

One page that answers "is everything behind the portal still working?":
cron jobs (did each one run, did it log an error), the nightly database backup,
disk space, services and the Keycloak containers, SSL certificates, database
sizes and the portal's own errors over the last 24 hours.

The checks live in helpers/system_health.py and are shared with the daily
alert email (scripts/system_health_check.py). They are read-only.

Results are cached in-process for CACHE_SECONDS because a full run shells out
to journalctl/systemctl and takes several seconds. ?refresh=1 forces a new run.

app.py runs as __main__, so this module must not `from app import ...`.
init_system_health() is called from app.py and hands over get_db.
"""
import threading
import time

from flask import Blueprint, render_template, request, session, redirect, url_for, flash

from helpers import system_health as sh

system_health_bp = Blueprint('system_health', __name__)

CACHE_SECONDS = 120

_h = {}
_cache = {'at': 0.0, 'report': None}
_lock = threading.Lock()


def init_system_health(**helpers):
    """helpers: get_db."""
    _h.update(helpers)


def can_view():
    return bool(session.get('is_admin'))


def _db_sizes():
    get_db = _h.get('get_db')
    if not get_db:
        return []
    try:
        conn = get_db()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT table_schema AS name,
                           SUM(data_length + index_length) AS bytes,
                           COUNT(*) AS tables
                    FROM information_schema.tables
                    WHERE table_schema NOT IN ('mysql', 'information_schema',
                                               'performance_schema', 'sys')
                    GROUP BY table_schema
                    ORDER BY bytes DESC
                """)
                return [{'name': r['name'], 'bytes': int(r['bytes'] or 0),
                         'tables': r['tables']} for r in cur.fetchall()]
        finally:
            conn.close()
    except Exception:
        return []


def get_report(force=False):
    with _lock:
        fresh = _cache['report'] and time.time() - _cache['at'] < CACHE_SECONDS
        if fresh and not force:
            return _cache['report']
        report = sh.collect_all()
        report['db_sizes'] = _db_sizes()
        report['problems'] = sh.problems_list(report)
        _cache.update(at=time.time(), report=report)
        return report


@system_health_bp.app_template_filter('human_bytes')
def human_bytes(n):
    if n is None:
        return '—'
    n = float(n)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or unit == 'TB':
            return f'{n:.0f} {unit}' if unit in ('B', 'KB') else f'{n:.1f} {unit}'
        n /= 1024


@system_health_bp.app_template_filter('ago')
def ago(dt, now=None):
    if not dt:
        return '—'
    from datetime import datetime
    secs = ((now or datetime.now()) - dt).total_seconds()
    if secs < 0:
        secs = -secs
        suffix = 'from now'
    else:
        suffix = 'ago'
    if secs < 90:
        return f'{int(secs)}s {suffix}'
    if secs < 5400:
        return f'{int(secs // 60)} min {suffix}'
    if secs < 172800:
        return f'{secs / 3600:.0f} h {suffix}'
    return f'{secs / 86400:.0f} days {suffix}'


@system_health_bp.route('/admin/system-health')
def page():
    if 'user' not in session:
        return redirect(url_for('login'))
    if not can_view():
        flash('Admin access required.', 'danger')
        return redirect(url_for('dashboard'))
    report = get_report(force=request.args.get('refresh') == '1')
    status_filter = request.args.get('jobs', 'issues')
    if status_filter not in ('issues', 'all'):
        status_filter = 'issues'
    jobs = report['jobs']
    if status_filter == 'issues':
        jobs = [j for j in jobs if j['status'] != 'ok']
    counts = {s: sum(1 for j in report['jobs'] if j['status'] == s)
              for s in ('ok', 'info', 'warn', 'error')}
    return render_template('admin/system_health.html', r=report, jobs=jobs,
                           job_counts=counts, jobs_filter=status_filter,
                           cache_seconds=CACHE_SECONDS)
