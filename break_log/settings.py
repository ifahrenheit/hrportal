"""Admin -> Break Log Access: tick per account + group who may log breaks,
who may monitor, and whose breaks feed the portal Overbreak Review.

Rules live in central_db.break_log_group_access; every change is written to
break_log_access_log. Admins and sub-admins with can_settings.
"""

from datetime import date, datetime

from flask import render_template, request, session, redirect, url_for, flash, abort

from csrf import validate_csrf
from break_log import access as bl_access
from break_log.routes import break_log_bp

SHEET_LOOKBACK_DAYS = 60


def _can_manage():
    return bool(session.get('user')) and (
        session.get('is_admin') or session.get('permissions', {}).get('can_settings'))


def _load(c):
    """Rows for the page: every pair in the table or the employee master."""
    c.execute("SELECT * FROM break_log_group_access")
    saved = {bl_access.pair_key(r['account'], r['group_name']): r for r in c.fetchall()}

    c.execute("""SELECT TRIM(COALESCE(account, '')) AS account,
                        TRIM(COALESCE(group_name, '')) AS group_name,
                        SUM(status IN ('Active', 'Training')) AS active
                 FROM gsheet_employees WHERE status <> 'Separated'
                 GROUP BY 1, 2""")
    counts = {bl_access.pair_key(r['account'], r['group_name']): int(r['active'] or 0)
              for r in c.fetchall()}

    # Groups whose overbreaks already arrive from the overbreak sheet
    c.execute("""SELECT DISTINCT TRIM(COALESCE(g.account, '')) AS account,
                                 TRIM(COALESCE(g.group_name, '')) AS group_name
                 FROM overbreak_records o
                 JOIN gsheet_employees g ON g.employee_id = o.employee_id
                 WHERE o.record_date >= CURDATE() - INTERVAL %s DAY""",
              (SHEET_LOOKBACK_DAYS,))
    sheet = {bl_access.pair_key(r['account'], r['group_name']) for r in c.fetchall()}

    rows = []
    for key in sorted(set(saved) | set(counts)):
        r = saved.get(key)
        rows.append({
            'account': key[0], 'group_name': key[1],
            'employees': counts.get(key, 0),
            'is_new': r is None,
            'from_sheet': key in sheet,
            **{f: bool(r and r[f]) for f in bl_access.FIELDS},
            'overbreak_review_from': r['overbreak_review_from'] if r else None,
        })
    return rows, saved


@break_log_bp.route('/admin/break-log-access')
def break_log_access():
    if 'user' not in session:
        return redirect(url_for('login'))
    if not _can_manage():
        abort(403)
    from app import get_central_db
    db = get_central_db()
    try:
        with db.cursor() as c:
            rows, _ = _load(c)
            c.execute("SELECT * FROM break_log_access_log ORDER BY created_at DESC, id DESC LIMIT 200")
            log = c.fetchall()
    finally:
        db.close()
    labels = dict(bl_access.FIELD_LABELS, overbreak_review_from='Overbreak Review from')
    return render_template('break_log/access_settings.html', rows=rows, log=log,
                           labels=labels, fields=bl_access.FIELDS,
                           lookback=SHEET_LOOKBACK_DAYS, today=date.today().isoformat())


def _parse_date(raw):
    try:
        return datetime.strptime((raw or '').strip(), '%Y-%m-%d').date()
    except ValueError:
        return None


@break_log_bp.route('/admin/break-log-access', methods=['POST'])
def break_log_access_save():
    if not _can_manage():
        abort(403)
    if not validate_csrf():
        flash('Security check failed, please try again.', 'danger')
        return redirect(url_for('break_log.break_log_access'))

    user = session['user']
    actor_id = str(user.get('employee_id') or user.get('email') or 'unknown')
    actor_name = user.get('name')

    from app import get_central_db
    db = get_central_db()
    changes = 0
    try:
        with db.cursor() as c:
            rows, saved = _load(c)
            known = {(r['account'], r['group_name']) for r in rows}
            from_sheet = {(r['account'], r['group_name']) for r in rows if r['from_sheet']}
            n = request.form.get('n', 0, type=int)
            for i in range(n):
                key = bl_access.pair_key(request.form.get(f'acc_{i}'), request.form.get(f'grp_{i}'))
                if key not in known:
                    continue  # only pairs shown on the page
                old = saved.get(key)
                new = {f: f'{f}_{i}' in request.form for f in bl_access.FIELDS}
                # Start date only matters while Overbreak Review is on. Sheet groups
                # get one (default today) so ticking the box doesn't pull months of
                # old, already-handled overbreaks into the review queue.
                ob_from = _parse_date(request.form.get(f'obfrom_{i}')) if new['overbreak_review'] else None
                if new['overbreak_review'] and ob_from is None and key in from_sheet:
                    ob_from = date.today()
                diffs = []
                for f in bl_access.FIELDS:
                    before = None if old is None else bool(old[f])
                    if before is None and not new[f]:
                        continue
                    if before != new[f]:
                        diffs.append((f, before, new[f], None, None))
                old_from = old['overbreak_review_from'] if old is not None else None
                if old_from != ob_from and (old is not None or ob_from):
                    diffs.append(('overbreak_review_from', None if old_from is None else True,
                                  ob_from is not None,
                                  old_from.isoformat() if old_from else None,
                                  ob_from.isoformat() if ob_from else None))
                if old is not None and not diffs:
                    continue
                # New pairs are saved even when all off, so they stop showing as "new".
                c.execute("""INSERT INTO break_log_group_access
                        (account, group_name, can_log, can_monitor, overbreak_review,
                         overbreak_review_from, updated_by_id, updated_by_name)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    ON DUPLICATE KEY UPDATE can_log=VALUES(can_log), can_monitor=VALUES(can_monitor),
                        overbreak_review=VALUES(overbreak_review),
                        overbreak_review_from=VALUES(overbreak_review_from),
                        updated_by_id=VALUES(updated_by_id), updated_by_name=VALUES(updated_by_name)""",
                    (key[0], key[1], new['can_log'], new['can_monitor'], new['overbreak_review'],
                     ob_from, actor_id, actor_name))
                for f, before, after, old_text, new_text in diffs:
                    c.execute("""INSERT INTO break_log_access_log
                            (account, group_name, field, old_value, new_value, old_text, new_text,
                             actor_id, actor_name)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (key[0], key[1], f, before, after, old_text, new_text, actor_id, actor_name))
                    changes += 1
        db.commit()
    except Exception as e:
        db.rollback()
        flash(f'Could not save: {e}', 'danger')
        return redirect(url_for('break_log.break_log_access'))
    finally:
        db.close()

    bl_access.invalidate()
    flash(f'Saved. {changes} change(s) applied.' if changes else 'No changes.',
          'success' if changes else 'info')
    return redirect(url_for('break_log.break_log_access'))
