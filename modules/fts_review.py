# fts_review.py
# Flask Blueprint -- Path B FTS standing report (payroll-period, categorized, in-place review)
# Place at: /var/www/html/leavesystem/fts_review.py
from flask import Blueprint, render_template, request, jsonify, session, redirect
from csrf import validate_csrf
from datetime import datetime, date, timedelta
from functools import wraps

fts_review_bp = Blueprint('fts_review', __name__)

def get_db():
    from app import get_central_db
    return get_central_db()

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user' not in session:
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated

def can_review():
    return bool(session.get('is_admin') or session.get('is_sub_admin'))

# ── Payroll period helpers (match app.py convention) ──────────────────────────
def prev_month(d):
    if d.month == 1:
        return d.replace(year=d.year-1, month=12, day=1)
    return d.replace(month=d.month-1, day=1)

def next_month(d):
    if d.month == 12:
        return d.replace(year=d.year+1, month=1, day=1)
    return d.replace(month=d.month+1, day=1)

def period_bounds(anchor):
    """Return (from, to, label) of the payroll period CONTAINING `anchor`."""
    if anchor.day >= 23:
        f = anchor.replace(day=23); t = next_month(anchor).replace(day=7)
    elif anchor.day >= 8:
        f = anchor.replace(day=8);  t = anchor.replace(day=22)
    else:  # 1..7 -> belongs to the 23->7 period that started last month
        p = prev_month(anchor)
        f = p.replace(day=23); t = anchor.replace(day=7)
    return f, t

def shift_period(f, back=1):
    """Move `back` periods earlier (or later if negative) from period starting at f."""
    cur = f
    for _ in range(abs(back)):
        if back > 0:
            cur = period_bounds(cur - timedelta(days=1))[0]
        else:
            # next period: day after this period's end
            _, t = period_bounds(cur)
            cur = period_bounds(t + timedelta(days=1))[0]
    return period_bounds(cur)

def period_label(f, t):
    return f"{f.strftime('%b %d')} – {t.strftime('%b %d, %Y')}"

def build_period_options(default_from, n_back=8):
    """List of recent periods (most recent first) for the dropdown."""
    opts = []
    f, t = period_bounds(date.today())   # current (in progress)
    opts.append({'from': f.strftime('%Y-%m-%d'), 'to': t.strftime('%Y-%m-%d'),
                 'label': period_label(f, t) + '  (current — in progress)',
                 'current': True})
    # walk backwards
    pf, pt = f, t
    for i in range(n_back):
        pf, pt = shift_period(pf, back=1)
        opts.append({'from': pf.strftime('%Y-%m-%d'), 'to': pt.strftime('%Y-%m-%d'),
                     'label': period_label(pf, pt), 'current': False})
    return opts

CATEGORY_TABS = [
    ('fts',                'FTS',      'Failure to swipe (present, one punch missing)'),
    ('absent_unexplained', 'Absent',   'Scheduled, no punches, no leave on file (AWOL or pending leave)'),
    ('on_leave',           'On Leave', 'Approved/scheduled leave on this date'),
    ('cws',                'CWS',      'Approved schedule change (shift moved) - not a failure to swipe'),
    ('review_schedule',    'Schedule', 'Schedule entry could not be parsed'),
]

@fts_review_bp.route('/')
@login_required
def review_page():
    if not can_review():
        return render_template('fts_review.html', tabs=[], denied=True,
                               period={}, period_options=[])

    # default = last COMPLETED period (one behind current)
    cur_from, _ = period_bounds(date.today())
    def_from, def_to = shift_period(cur_from, back=1)
    date_from = request.args.get('date_from', def_from.strftime('%Y-%m-%d'))
    date_to   = request.args.get('date_to',   def_to.strftime('%Y-%m-%d'))

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, employee_id, name, tl, email, schedule_date, shift_time,
                       flag_type, category, candidate_punch, candidate_note, ot_context, fts_filed,
                       review_status, reviewed_by, reviewed_at
                FROM fts_pathb_incidents
                WHERE schedule_date BETWEEN %s AND %s
                ORDER BY tl, schedule_date, name
            """, (date_from, date_to))
            rows = cur.fetchall()
    finally:
        conn.close()

    by_cat = {key: {} for key, _, _ in CATEGORY_TABS}
    by_cat['other'] = {}
    for r in rows:
        cat = r['category'] if r['category'] in by_cat else 'other'
        tl = r['tl'] or 'Unassigned'
        by_cat[cat].setdefault(tl, []).append(r)

    tabs = []
    for key, label, desc in CATEGORY_TABS:
        groups = [{'tl': k, 'items': v} for k, v in sorted(by_cat[key].items())]
        tabs.append({'key': key, 'label': label, 'desc': desc, 'groups': groups,
                     'count': sum(len(g['items']) for g in groups)})
    if by_cat['other']:
        groups = [{'tl': k, 'items': v} for k, v in sorted(by_cat['other'].items())]
        tabs.append({'key': 'other', 'label': 'Other', 'desc': 'Uncategorized',
                     'groups': groups, 'count': sum(len(g['items']) for g in groups)})

    period = {'from': date_from, 'to': date_to,
              'label': period_label(datetime.strptime(date_from, '%Y-%m-%d'),
                                    datetime.strptime(date_to, '%Y-%m-%d'))}
    return render_template('fts_review.html', tabs=tabs, denied=False,
                           period=period, period_options=build_period_options(date_from))

@fts_review_bp.route('/api/<int:incident_id>/<action>', methods=['POST'])
@login_required
def review_action(incident_id, action):
    if not can_review():
        return jsonify(ok=False, error='not authorized'), 403
    if not validate_csrf():
        return jsonify(ok=False, error='Security check failed, please try again.'), 403
    if action not in ('confirm', 'dismiss', 'reset'):
        return jsonify(ok=False, error='bad action'), 400
    if action == 'reset':
        new_status, reviewer, when = 'pending', None, None
    else:
        new_status = 'confirmed' if action == 'confirm' else 'dismissed'
        reviewer = session['user'].get('name') or session['user'].get('email')
        when = datetime.now()
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""UPDATE fts_pathb_incidents
                           SET review_status=%s, reviewed_by=%s, reviewed_at=%s
                           WHERE id=%s""", (new_status, reviewer, when, incident_id))
        conn.commit()
    finally:
        conn.close()
    stamp = when.strftime('%b %d, %H:%M') if when else None
    return jsonify(ok=True, id=incident_id, status=new_status,
                   reviewed_by=reviewer, reviewed_at=stamp)