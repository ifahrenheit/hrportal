"""
Leave filing hold -- Admin > Settings > "Leave Filing Hold"

An on/off switch that stops employees in chosen groups from filing chosen leave
types (by default Vacation Leave, Carry-Over Leave and Sick Leave) from their
own File Leave page. The held types still show in the dropdown, greyed out and
marked "(on hold)", and a POST for one of them is refused server-side.

Settings (orangehrm2.leave4day_settings, one row per key):
  leave_hold_enabled   '1' / '0'
  leave_hold_groups    JSON list of central_db.gsheet_employees.group_name
  leave_hold_types     JSON list of ohrm_leave_type.id
  leave_hold_message   text shown to held employees

Scope:
  - Only the employee's own /file-leave. Admin > File Leave for Employee is not
    affected, so HR can still file approved exceptions.
  - Leave that was already filed (pending/approved/scheduled) is untouched.
  - An employee with no gsheet_employees row has no group and is never held.

app.py runs as __main__, so this module must not `from app import ...`.
init_leave_hold() is called from app.py and hands over the helpers.
"""
import json

from flask import Blueprint, request, session, redirect, url_for, flash

from csrf import validate_csrf

leave_hold_bp = Blueprint('leave_hold', __name__)

_h = {}

DEFAULT_TYPE_NAMES = ('Vacation Leave', 'Carry-Over Leave', 'Sick Leave')
DEFAULT_MESSAGE = ('Filing for this leave type is on hold for now. '
                   'Please contact HR if you need to file an urgent leave.')
KEYS = ('leave_hold_enabled', 'leave_hold_groups', 'leave_hold_types', 'leave_hold_message')


def init_leave_hold(**helpers):
    """helpers: get_db."""
    _h.update(helpers)


def _norm(s):
    return (s or '').strip().casefold()


def _loads_list(raw):
    try:
        val = json.loads(raw) if raw else []
        return val if isinstance(val, list) else []
    except (ValueError, TypeError):
        return []


def parse_config(rows, leave_types=()):
    """rows: {setting_key: setting_value}. leave_types: [{'id', 'name'}], used to
    fill in the default held types when leave_hold_types was never saved."""
    if 'leave_hold_types' in rows:
        type_ids = [int(t) for t in _loads_list(rows['leave_hold_types']) if str(t).isdigit()]
    else:
        type_ids = [t['id'] for t in leave_types if t['name'] in DEFAULT_TYPE_NAMES]
    return {
        'enabled':  rows.get('leave_hold_enabled') == '1',
        'groups':   [str(g) for g in _loads_list(rows.get('leave_hold_groups'))],
        'type_ids': type_ids,
        'message':  rows.get('leave_hold_message') or DEFAULT_MESSAGE,
    }


def held_type_ids_for_group(config, group_name):
    """The leave type ids held for someone in group_name (empty set if none)."""
    if not config['enabled'] or not group_name:
        return set()
    if _norm(group_name) not in {_norm(g) for g in config['groups']}:
        return set()
    return set(config['type_ids'])


def _read_rows(c):
    fmt = ','.join(['%s'] * len(KEYS))
    c.execute(f"SELECT setting_key, setting_value FROM leave4day_settings "
              f"WHERE setting_key IN ({fmt})", KEYS)
    return {r['setting_key']: r['setting_value'] for r in c.fetchall()}


def _leave_types(c):
    c.execute("SELECT id, name FROM ohrm_leave_type WHERE deleted = 0 ORDER BY id")
    return c.fetchall()


def get_config():
    db = _h['get_db']()
    try:
        with db.cursor() as c:
            return parse_config(_read_rows(c), _leave_types(c))
    finally:
        db.close()


def held_type_ids(emp_number):
    """Leave type ids this employee can't file right now. Never raises: if the
    lookup fails, filing is allowed (the hold is a convenience, not a lock)."""
    try:
        db = _h['get_db']()
        try:
            with db.cursor() as c:
                rows = _read_rows(c)
                if rows.get('leave_hold_enabled') != '1':
                    return set()
                config = parse_config(rows, _leave_types(c))
                c.execute("""SELECT g.group_name
                             FROM hs_hr_employee h
                             JOIN central_db.gsheet_employees g ON g.employee_id = h.employee_id
                             WHERE h.emp_number = %s LIMIT 1""", (emp_number,))
                row = c.fetchone()
                return held_type_ids_for_group(config, row['group_name'] if row else None)
        finally:
            db.close()
    except Exception:
        return set()


def mark_balances(balances, emp_number):
    """Copy of balances with on_hold / hold_message set on held leave types."""
    held = held_type_ids(emp_number)
    if not held:
        return balances
    message = get_config()['message']
    return [dict(b, on_hold=True, hold_message=message) if b['leave_type_id'] in held else b
            for b in balances]


def settings_context():
    """Data for the Leave Filing Hold card on admin/settings.html."""
    db = _h['get_db']()
    try:
        with db.cursor() as c:
            types = _leave_types(c)
            config = parse_config(_read_rows(c), types)
            c.execute("""SELECT group_name, COUNT(*) AS n FROM central_db.gsheet_employees
                         WHERE status IN ('Active', 'Training')
                           AND group_name IS NOT NULL AND group_name <> ''
                         GROUP BY group_name ORDER BY group_name""")
            groups = [{'name': r['group_name'], 'count': r['n']} for r in c.fetchall()]
    finally:
        db.close()
    # Keep saved groups that no longer have active employees, so they can be unticked.
    known = {_norm(g['name']) for g in groups}
    groups += [{'name': g, 'count': 0} for g in config['groups'] if _norm(g) not in known]
    selected = {_norm(g) for g in config['groups']}
    for g in groups:
        g['held'] = _norm(g['name']) in selected
    held_people = sum(g['count'] for g in groups if g['held'])
    return {'config': config, 'groups': groups, 'leave_types': types,
            'held_people': held_people}


@leave_hold_bp.route('/admin/settings/leave-hold', methods=['POST'])
def save_leave_hold():
    if not session.get('user'):
        return redirect(url_for('login'))
    if not session.get('is_admin') and not session.get('permissions', {}).get('can_settings'):
        flash('You do not have permission to access this page.', 'danger')
        return redirect(url_for('dashboard'))
    if not validate_csrf():
        flash('Security check failed, please try again.', 'danger')
        return redirect(url_for('admin_settings'))

    enabled = request.form.get('enabled') == '1'
    groups = sorted({g.strip() for g in request.form.getlist('groups') if g.strip()})
    type_ids = sorted({int(t) for t in request.form.getlist('type_ids') if t.isdigit()})
    # leave4day_settings.setting_value is varchar(255)
    message = (request.form.get('message') or '').strip()[:255] or DEFAULT_MESSAGE
    values = {
        'leave_hold_enabled': '1' if enabled else '0',
        'leave_hold_groups':  json.dumps(groups, separators=(',', ':')),
        'leave_hold_types':   json.dumps(type_ids, separators=(',', ':')),
        'leave_hold_message': message,
    }
    if len(values['leave_hold_groups']) > 255:
        flash('Too many groups selected to save. Untick a few and try again.', 'danger')
        return redirect(url_for('admin_settings') + '#leave-hold')
    if enabled and (not groups or not type_ids):
        flash('Pick at least one group and one leave type before turning the hold on.', 'warning')
        return redirect(url_for('admin_settings') + '#leave-hold')
    db = _h['get_db']()
    try:
        with db.cursor() as c:
            for key, val in values.items():
                c.execute("SELECT id FROM leave4day_settings WHERE setting_key = %s", (key,))
                if c.fetchone():
                    c.execute("UPDATE leave4day_settings SET setting_value = %s, updated_by = %s "
                              "WHERE setting_key = %s", (val, session['user']['emp_number'], key))
                else:
                    c.execute("INSERT INTO leave4day_settings (setting_key, setting_value, updated_by) "
                              "VALUES (%s, %s, %s)", (key, val, session['user']['emp_number']))
        db.commit()
    except Exception as e:
        db.rollback()
        flash(f'Error saving leave filing hold: {e}', 'danger')
        return redirect(url_for('admin_settings'))
    finally:
        db.close()

    if enabled:
        flash(f'Leave filing hold is ON for {len(groups)} group(s), {len(type_ids)} leave type(s).',
              'success')
    else:
        flash('Leave filing hold is OFF. Everyone can file leave normally.', 'success')
    return redirect(url_for('admin_settings') + '#leave-hold')
