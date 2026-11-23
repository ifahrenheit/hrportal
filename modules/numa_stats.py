"""Numa Stats: monthly / weekly (later daily) CS performance for the Numa team.

Data: central_db.numa_stats, filled from the "NUMA Stat Dashboard" sheet by
helpers/numa_stats.py (hourly cron + the admin "Sync now" button).

Who can see it (whole-team view, same as the sheet's MoM/WoW tabs):
  - admins
  - Active/Training employees in group "Numa CS" or "Numa BO"
  - anyone who is the approver (gsheet_employees.approver) of such an employee
The check is cached in the session for ACCESS_TTL seconds, keyed by email so
Login As re-evaluates for the viewed-as user.

Agents shown: the same Numa CS/BO roster, matched to Numa usernames by email
(nina.delcastillo@cohere.ph -> nina_delcastillo). Agents with no stats for
that period type at all (new hires, BO for now) are left out.

    GET  /numa-stats/                   page
    GET  /numa-stats/api/data           ?type=month|week|day&period=YYYY-MM-DD
    GET  /numa-stats/api/trend          ?metric=…&year=YYYY  (month-by-month grid)
    POST /numa-stats/api/sync           admin only, CSRF
    GET  /numa-stats/api/teams          admin only: roster with team
    POST /numa-stats/api/teams          admin only, CSRF: {username, team}

Teams: agents are split into TEAMS for display. Assignments live in
central_db.numa_stats_teams; anyone without a row is DEFAULT_TEAM.
"""
import os
import time
from datetime import date, datetime, timedelta
from functools import wraps
from statistics import median

from flask import Blueprint, flash, jsonify, redirect, render_template, request, session, url_for

from csrf import validate_csrf
from db_core import get_db_connection
from helpers import numa_stats as ns

numa_stats_bp = Blueprint('numa_stats', __name__, url_prefix='/numa-stats')

NUMA_GROUPS = ('Numa CS', 'Numa BO')
ROSTER_STATUSES = ('Active', 'Training')
ACCESS_TTL = 600
PERIOD_TYPES = ('month', 'week', 'day')
TABLE_METRICS = ('conversations', 'whatsapp_csat_pct', 'email_csat', 'email_sla_pct',
                 'median_first_reply_sec', 'median_response_sec')
TREND_METRICS = ('conversations', 'whatsapp_csat_pct', 'email_csat', 'email_sla_pct')
# Team rows / tiles (user's rules, 2026-11-18):
#   conversations -> total of the agents
#   CSAT          -> average across the team's conversations, i.e. each agent's CSAT
#                    weighted by their conversation count
#   everything else -> median of the agents
SUM_METRICS = ('conversations',)
WEIGHTED_METRICS = ('whatsapp_csat_pct', 'email_csat')
SUMMARY_KINDS = {m: ('total' if m in SUM_METRICS else 'weighted' if m in WEIGHTED_METRICS else 'median')
                 for m in TABLE_METRICS}
TEAMS = ('GX Team', 'GX Night Shift')
DEFAULT_TEAM = 'GX Team'


# ─── access ─────────────────────────────────────────────────────────────────
def _email():
    return ((session.get('user') or {}).get('email') or '').strip().lower()


def _lookup_access(email):
    ph = ', '.join(['%s'] * len(NUMA_GROUPS))
    sp = ', '.join(['%s'] * len(ROSTER_STATUSES))
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute(f"""
                SELECT 1 FROM gsheet_employees
                 WHERE group_name IN ({ph}) AND status IN ({sp})
                   AND (LOWER(TRIM(email)) = %s OR LOWER(TRIM(approver)) = %s)
                 LIMIT 1""", (*NUMA_GROUPS, *ROSTER_STATUSES, email, email))
            return c.fetchone() is not None
    finally:
        conn.close()


def _sync_emails():
    """Non-admins allowed to press Sync now (.env NUMA_STATS_SYNC_EMAILS, comma-separated)."""
    raw = os.environ.get('NUMA_STATS_SYNC_EMAILS', '')
    return {e.strip().lower() for e in raw.split(',') if e.strip()}


def numa_stats_can_sync():
    if not session.get('user'):
        return False
    return bool(session.get('is_admin')) or _email() in _sync_emails()


def numa_stats_can_view():
    """Template global + route check."""
    if not session.get('user'):
        return False
    if session.get('is_admin'):
        return True
    email = _email()
    if not email:
        return False
    if email in _sync_emails():
        return True
    cached = session.get('_numa_stats_access')
    if cached and cached.get('email') == email and time.time() - cached.get('ts', 0) < ACCESS_TTL:
        return bool(cached.get('ok'))
    try:
        ok = _lookup_access(email)
    except Exception:
        return False
    session['_numa_stats_access'] = {'email': email, 'ok': ok, 'ts': time.time()}
    return ok


def _require_view(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get('user'):
            return redirect(url_for('login'))
        if not numa_stats_can_view():
            if request.path.startswith('/numa-stats/api'):
                return jsonify({'error': 'Forbidden'}), 403
            flash('Numa Stats is only available to the Numa team.', 'danger')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return wrapper


# ─── helpers ────────────────────────────────────────────────────────────────
def username_from_email(email):
    return (email or '').strip().lower().split('@')[0].replace('.', '_')


def _roster(conn):
    """{username: {'name', 'group', 'team'}} for current Numa CS/BO staff."""
    ph = ', '.join(['%s'] * len(NUMA_GROUPS))
    sp = ', '.join(['%s'] * len(ROSTER_STATUSES))
    with conn.cursor() as c:
        c.execute(f"""
            SELECT schedule_name, email, group_name FROM gsheet_employees
             WHERE group_name IN ({ph}) AND status IN ({sp})
               AND email IS NOT NULL AND email <> ''""", (*NUMA_GROUPS, *ROSTER_STATUSES))
        rows = c.fetchall()
        c.execute("SELECT username, team FROM numa_stats_teams")
        teams = {r['username']: r['team'] for r in c.fetchall()}
    out = {}
    for r in rows:
        u = username_from_email(r['email'])
        t = teams.get(u)
        out[u] = {'name': r['schedule_name'] or r['email'], 'group': r['group_name'],
                  'team': t if t in TEAMS else DEFAULT_TEAM}
    return out


def _team_arg():
    """'all' or one of TEAMS from ?team=; None when invalid."""
    t = request.args.get('team') or 'all'
    return t if (t == 'all' or t in TEAMS) else None


def _num(v):
    if v is None:
        return None
    return float(v) if not isinstance(v, int) else v


def period_label(kind, start):
    if kind == 'month':
        return start.strftime('%B %Y')
    if kind == 'week':
        end = start + timedelta(days=6)
        iso = start.isocalendar()
        return f"Week {iso[1]} · {start.strftime('%b')} {start.day} – {end.strftime('%b')} {end.day}, {end.year}"
    return start.strftime('%a, %b ') + str(start.day) + start.strftime(', %Y')


def team_value(rows, metric):
    """Team figure for a metric (see SUMMARY_KINDS). Blanks ignored; rows need a
    'conversations' value to count towards a weighted CSAT."""
    if metric in WEIGHTED_METRICS:
        pairs = [(r[metric], r.get('conversations')) for r in rows
                 if r.get(metric) is not None and r.get('conversations')]
        total = sum(w for _, w in pairs)
        return round(sum(v * w for v, w in pairs) / total, 2) if total else None
    vals = [r[metric] for r in rows if r.get(metric) is not None]
    if not vals:
        return None
    if metric in SUM_METRICS:
        return sum(vals)
    return round(median(vals), 2)


def summarise(rows, has_prev):
    """Team figures for a set of agent rows (current + previous period)."""
    prev_rows = [{m: r['prev_' + m] for m in TABLE_METRICS} for r in rows]
    return {
        'agents': len(rows),
        'summary': {m: team_value(rows, m) for m in TABLE_METRICS},
        'prev_summary': {m: team_value(prev_rows, m) for m in TABLE_METRICS} if has_prev else {},
        'median_conversations': (round(median(v), 2) if (v := [r['conversations'] for r in rows
                                                                 if r['conversations'] is not None]) else None),
        'total_conversations': sum(r['conversations'] or 0 for r in rows),
        'prev_total_conversations': (sum(r['prev_conversations'] or 0 for r in rows) if has_prev else None),
    }


def _last_sync(conn):
    with conn.cursor() as c:
        c.execute("""SELECT started_at, finished_at, ok, summary, triggered_by FROM numa_stats_sync_log
                      WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1""")
        r = c.fetchone()
        c.execute("SELECT MAX(finished_at) AS t FROM numa_stats_sync_log WHERE ok = 1")
        good = c.fetchone()
    return {
        'last_ok_at': good['t'].isoformat(sep=' ') if good and good['t'] else None,
        'last_run_ok': bool(r['ok']) if r else None,
        'last_run_at': r['finished_at'].isoformat(sep=' ') if r else None,
        'last_run_summary': (r['summary'] if r and not r['ok'] else None),
    }


# ─── routes ─────────────────────────────────────────────────────────────────
@numa_stats_bp.route('/')
@_require_view
def page():
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("SELECT DISTINCT period_type FROM numa_stats")
            types = {r['period_type'] for r in c.fetchall()}
    finally:
        conn.close()
    available = [t for t in PERIOD_TYPES if t in types] or ['month', 'week']
    return render_template('numa_stats/index.html',
                           available_types=available,
                           teams=TEAMS,
                           my_username=username_from_email(_email()),
                           is_admin=bool(session.get('is_admin')), can_sync=numa_stats_can_sync())


@numa_stats_bp.route('/api/data')
@_require_view
def api_data():
    kind = request.args.get('type', 'month')
    team_sel = _team_arg()
    if kind not in PERIOD_TYPES or team_sel is None:
        return jsonify({'error': 'bad type or team'}), 400
    conn = get_db_connection()
    try:
        roster = _roster(conn)
        if team_sel != 'all':
            roster = {u: v for u, v in roster.items() if v['team'] == team_sel}
        users = list(roster)
        if not users:
            return jsonify({'periods': [], 'rows': [], 'teams': list(TEAMS), **_last_sync(conn)})
        ph = ', '.join(['%s'] * len(users))
        with conn.cursor() as c:
            c.execute(f"""SELECT DISTINCT period_start FROM numa_stats
                           WHERE period_type = %s AND username IN ({ph})
                           ORDER BY period_start DESC""", (kind, *users))
            periods = [r['period_start'] for r in c.fetchall()]
        if not periods:
            return jsonify({'periods': [], 'rows': [], 'teams': list(TEAMS), **_last_sync(conn)})
        want = request.args.get('period')
        try:
            sel = datetime.strptime(want, '%Y-%m-%d').date() if want else periods[0]
        except ValueError:
            return jsonify({'error': 'bad period'}), 400
        if sel not in periods:
            sel = periods[0]
        prev = next((p for p in periods if p < sel), None)
        with conn.cursor() as c:
            c.execute(f"""SELECT username, period_start, {', '.join(TABLE_METRICS)} FROM numa_stats
                           WHERE period_type = %s AND period_start IN (%s, %s) AND username IN ({ph})""",
                      (kind, sel, prev or sel, *users))
            got = c.fetchall()
            # agents with any data for this period type (so a quiet period still lists them)
            c.execute(f"""SELECT DISTINCT username FROM numa_stats
                           WHERE period_type = %s AND username IN ({ph})""", (kind, *users))
            active_users = {r['username'] for r in c.fetchall()}
        cur = {r['username']: r for r in got if r['period_start'] == sel}
        old = {r['username']: r for r in got if prev and r['period_start'] == prev}
        rows = []
        for u in sorted(active_users, key=lambda x: roster[x]['name'].lower()):
            r = cur.get(u, {})
            p = old.get(u, {})
            row = {'username': u, 'name': roster[u]['name'], 'group': roster[u]['group'],
                   'team': roster[u]['team']}
            for m in TABLE_METRICS:
                row[m] = _num(r.get(m))
                row['prev_' + m] = _num(p.get(m))
            rows.append(row)
        groups = [{'team': t, **summarise([r for r in rows if r['team'] == t], bool(prev))}
                  for t in TEAMS if any(r['team'] == t for r in rows)]
        return jsonify({
            'team_filter': team_sel,
            'teams': list(TEAMS),
            'groups': groups,
            **summarise(rows, bool(prev)),
            'type': kind,
            'period': sel.isoformat(),
            'period_label': period_label(kind, sel),
            'prev_period_label': period_label(kind, prev) if prev else None,
            'periods': [{'value': p.isoformat(), 'label': period_label(kind, p)} for p in periods],
            'rows': rows,
            'summary_kinds': SUMMARY_KINDS,
            **_last_sync(conn),
        })
    finally:
        conn.close()


@numa_stats_bp.route('/api/trend')
@_require_view
def api_trend():
    metric = request.args.get('metric', 'conversations')
    if metric not in TREND_METRICS:
        return jsonify({'error': 'bad metric'}), 400
    try:
        year = int(request.args.get('year') or date.today().year)
    except ValueError:
        return jsonify({'error': 'bad year'}), 400
    team_sel = _team_arg()
    if team_sel is None:
        return jsonify({'error': 'bad team'}), 400
    conn = get_db_connection()
    try:
        roster = _roster(conn)
        if team_sel != 'all':
            roster = {u: v for u, v in roster.items() if v['team'] == team_sel}
        users = list(roster)
        if not users:
            return jsonify({'year': year, 'years': [], 'rows': [], 'summary': [None] * 12, 'groups': []})
        ph = ', '.join(['%s'] * len(users))
        with conn.cursor() as c:
            c.execute(f"""SELECT DISTINCT YEAR(period_start) AS y FROM numa_stats
                           WHERE period_type = 'month' AND username IN ({ph}) ORDER BY y DESC""", users)
            years = [r['y'] for r in c.fetchall()]
            c.execute(f"""SELECT username, MONTH(period_start) AS m, {metric} AS v, conversations AS w FROM numa_stats
                           WHERE period_type = 'month' AND YEAR(period_start) = %s
                             AND username IN ({ph})""", (year, *users))
            got = c.fetchall()
    finally:
        conn.close()
    grid, conv = {}, {}
    for r in got:
        grid.setdefault(r['username'], [None] * 12)[r['m'] - 1] = _num(r['v'])
        conv.setdefault(r['username'], [None] * 12)[r['m'] - 1] = r['w']
    rows = [{'username': u, 'name': roster[u]['name'], 'team': roster[u]['team'], 'values': grid[u]}
            for u in sorted(grid, key=lambda x: roster[x]['name'].lower())]
    by_month = lambda rs: [team_value([{metric: r['values'][i], 'conversations': conv[r['username']][i]}  # noqa: E731
                                       for r in rs], metric) for i in range(12)]
    groups = [{'team': t, 'summary': by_month([r for r in rows if r['team'] == t])}
              for t in TEAMS if any(r['team'] == t for r in rows)]
    return jsonify({'year': year, 'years': years, 'metric': metric, 'rows': rows,
                    'summary': by_month(rows), 'groups': groups,
                    'team_kind': SUMMARY_KINDS[metric]})


@numa_stats_bp.route('/api/sync', methods=['POST'])
def api_sync():
    if not numa_stats_can_sync():
        return jsonify({'error': 'Forbidden'}), 403
    if not validate_csrf():
        return jsonify({'error': 'Your session expired. Reload the page and try again.'}), 400
    conn = get_db_connection()
    try:
        ok, lines = ns.sync(conn, triggered_by=_email() or 'admin', live=True)
    finally:
        conn.close()
    return jsonify({'ok': ok, 'lines': lines}), (200 if ok else 502)


@numa_stats_bp.route('/api/teams', methods=['GET', 'POST'])
def api_teams():
    if not session.get('user') or not session.get('is_admin'):
        return jsonify({'error': 'Forbidden'}), 403
    conn = get_db_connection()
    try:
        if request.method == 'POST':
            if not validate_csrf():
                return jsonify({'error': 'Your session expired. Reload the page and try again.'}), 400
            body = request.get_json(silent=True) or {}
            user, team = (body.get('username') or '').strip().lower(), body.get('team')
            if team not in TEAMS or user not in _roster(conn):
                return jsonify({'error': 'Unknown agent or team'}), 400
            with conn.cursor() as c:
                c.execute("""INSERT INTO numa_stats_teams (username, team, updated_by, updated_at)
                             VALUES (%s, %s, %s, NOW())
                             ON DUPLICATE KEY UPDATE team = VALUES(team), updated_by = VALUES(updated_by),
                                                     updated_at = VALUES(updated_at)""",
                          (user, team, _email() or 'admin'))
            conn.commit()
            return jsonify({'ok': True, 'username': user, 'team': team})
        roster = _roster(conn)
        return jsonify({'teams': list(TEAMS), 'agents': [
            {'username': u, 'name': v['name'], 'group': v['group'], 'team': v['team']}
            for u, v in sorted(roster.items(), key=lambda kv: kv[1]['name'].lower())]})
    finally:
        conn.close()


# ─── Teams page: Looker team figures ("Jareth SLA Copy" sheet) ──────────────
TEAM_PERIOD_TYPES = ('week', 'month', 'year')
CHANNELS = ('email', 'whatsapp', 'whatsapp_threads', 'sms')


def team_period_label(kind, start):
    return str(start.year) if kind == 'year' else period_label(kind, start)


def team_figures(m):
    """Raw metric dict for one team/period -> the figures the page shows."""
    if not m:
        return None
    g = m.get
    by_channel = {ch: g(f'{ch}.unique_conversation_count') for ch in CHANNELS}
    have_conv = [v for v in by_channel.values() if v is not None]
    csat, csat_label = g('agent_csat'), 'Agent CSAT'
    if csat is None and g('agent_plus_emma_csat') is not None:
        csat, csat_label = g('agent_plus_emma_csat'), 'Agent + Emma CSAT'
    pct = lambda v: round(v * 100, 2) if v is not None else None  # noqa: E731
    return {
        'conversations': sum(have_conv) if have_conv else None,
        'by_channel': by_channel,
        'email_with_outbound': g('email.email_conversations_with_outbound'),
        'escalated_chatbot': g('whatsapp.escalated_chatbot_conversations'),
        'email_sla_pct': pct(g('email_message_sla_percentage')),
        'email_sla_met': g('email_message_sla_met'),
        'email_sla_not_met': g('email_message_sla_not_met'),
        'chatbot_sla_pct': pct(g('whatsapp_chatbot_message_sla_percentage')),
        'chatbot_sla_met': g('whatsapp_chatbot_message_sla_met'),
        'chatbot_sla_not_met': g('whatsapp_chatbot_message_sla_not_met'),
        'csat_pct': pct(csat),
        'csat_label': csat_label,
        'avg_csat': g('avg_csat'),
        'median_csat': g('median_csat'),
        'csat_response_rate_pct': pct(g('overall_csat_response_rate')),
        'csat_responses': g('escalated_responses_q1'),
    }


def _team_metrics(conn, kind, starts):
    """{period_start: {team: {metric: value}}} for the given periods."""
    if not starts:
        return {}
    ph = ', '.join(['%s'] * len(starts))
    with conn.cursor() as c:
        c.execute(f"""SELECT period_start, team, metric, value FROM numa_team_stats
                       WHERE period_type = %s AND period_start IN ({ph})""", (kind, *starts))
        out = {}
        for r in c.fetchall():
            out.setdefault(r['period_start'], {}).setdefault(r['team'], {})[r['metric']] = _num(r['value'])
    return out


def _agent_rollup(conn, kind, start):
    """Per-agent page figures for the same period, per team (month/week only)."""
    if kind not in ('month', 'week'):
        return {}
    roster = _roster(conn)
    if not roster:
        return {}
    ph = ', '.join(['%s'] * len(roster))
    with conn.cursor() as c:
        c.execute(f"""SELECT username, conversations, whatsapp_csat_pct, email_csat, email_sla_pct FROM numa_stats
                       WHERE period_type = %s AND period_start = %s AND username IN ({ph})""",
                  (kind, start, *roster))
        rows = [{**{k: _num(v) for k, v in r.items() if k != 'username'}, 'team': roster[r['username']]['team']}
                for r in c.fetchall()]
    out = {}
    for t in TEAMS:
        rs = [r for r in rows if r['team'] == t]
        if rs:
            out[t] = {'agents': len(rs),
                      'conversations': team_value(rs, 'conversations'),
                      'whatsapp_csat_pct': team_value(rs, 'whatsapp_csat_pct'),
                      'email_sla_pct': team_value(rs, 'email_sla_pct')}
    return out


@numa_stats_bp.route('/teams')
@_require_view
def teams_page():
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("SELECT DISTINCT period_type FROM numa_team_stats")
            types = {r['period_type'] for r in c.fetchall()}
    finally:
        conn.close()
    available = [t for t in TEAM_PERIOD_TYPES if t in types] or ['month']
    default = 'month' if 'month' in available else available[0]
    return render_template('numa_stats/teams.html', available_types=available, default_type=default,
                           teams=TEAMS, is_admin=bool(session.get('is_admin')), can_sync=numa_stats_can_sync())


@numa_stats_bp.route('/api/team-stats')
@_require_view
def api_team_stats():
    kind = request.args.get('type', 'month')
    if kind not in TEAM_PERIOD_TYPES:
        return jsonify({'error': 'bad type'}), 400
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""SELECT DISTINCT period_start FROM numa_team_stats
                          WHERE period_type = %s ORDER BY period_start DESC""", (kind,))
            periods = [r['period_start'] for r in c.fetchall()]
        if not periods:
            return jsonify({'periods': [], 'teams': [], **_last_sync(conn)})
        want = request.args.get('period')
        try:
            sel = datetime.strptime(want, '%Y-%m-%d').date() if want else periods[0]
        except ValueError:
            return jsonify({'error': 'bad period'}), 400
        if sel not in periods:
            sel = periods[0]
        prev = next((p for p in periods if p < sel), None)
        trend_starts = sorted(p for p in periods if p <= sel)[-12:]
        data = _team_metrics(conn, kind, list({sel, *([prev] if prev else []), *trend_starts}))
        rollup = _agent_rollup(conn, kind, sel)
        teams = []
        for t in TEAMS:
            cur = team_figures(data.get(sel, {}).get(t))
            old = team_figures(data.get(prev, {}).get(t)) if prev else None
            teams.append({'team': t, 'looker': cur, 'prev': old, 'agents': rollup.get(t)})
        trend = {
            'labels': [team_period_label(kind, p) for p in trend_starts],
            'teams': {t: [team_figures(data.get(p, {}).get(t)) for p in trend_starts] for t in TEAMS},
        }
        return jsonify({
            'type': kind,
            'period': sel.isoformat(),
            'period_label': team_period_label(kind, sel),
            'prev_period_label': team_period_label(kind, prev) if prev else None,
            'periods': [{'value': p.isoformat(), 'label': team_period_label(kind, p)} for p in periods],
            'teams': teams,
            'trend': trend,
            **_last_sync(conn),
        })
    finally:
        conn.close()
