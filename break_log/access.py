"""Break Log access rules per account + group (central_db.break_log_group_access).

Edited on Admin -> Break Log Access (break_log/settings.py). Rules are kept in
memory for RULES_TTL seconds; a save in this process calls invalidate() so the
change applies on the next request. The portal runs as a single process.

Lookups use the employee's account/group from gsheet_employees, with blank
values stored as ''. A pair with no row (a new group) has everything off.
"""

import time
import threading

FIELDS = ('can_log', 'can_monitor', 'overbreak_review')
FIELD_LABELS = {
    'can_log':          'Log breaks',
    'can_monitor':      'Monitor',
    'overbreak_review': 'Overbreak Review',
}
RULES_TTL = 60

_lock = threading.Lock()
_cache = {'at': 0.0, 'rules': {}}


def pair_key(account, group_name):
    return ((account or '').strip(), (group_name or '').strip())


def _load_rules():
    from app import get_central_db
    db = get_central_db()
    try:
        with db.cursor() as c:
            c.execute("SELECT account, group_name, can_log, can_monitor, overbreak_review "
                      "FROM break_log_group_access")
            return {pair_key(r['account'], r['group_name']):
                        {f: bool(r[f]) for f in FIELDS} for r in c.fetchall()}
    finally:
        db.close()


def rules():
    """{(account, group): {field: bool}} with a short in-memory cache. If the DB
    read fails, the last good copy is kept (an empty one denies everyone)."""
    now = time.time()
    if now - _cache['at'] < RULES_TTL:
        return _cache['rules']
    with _lock:
        if now - _cache['at'] < RULES_TTL:
            return _cache['rules']
        try:
            _cache['rules'] = _load_rules()
        except Exception:
            pass
        _cache['at'] = now
    return _cache['rules']


def invalidate():
    _cache['at'] = 0.0


def allowed(emp, field):
    """emp: a row with account and group_name (or None)."""
    if not emp:
        return False
    rule = rules().get(pair_key(emp.get('account'), emp.get('group_name')))
    return bool(rule and rule.get(field))


def pairs_with(field):
    """[(account, group)] where field is on, for SQL filters."""
    return sorted(k for k, v in rules().items() if v.get(field))
