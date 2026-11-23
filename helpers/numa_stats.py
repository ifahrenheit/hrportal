"""NUMA Stat Dashboard (Google Sheet) -> central_db.numa_stats.

The sheet's "MoM Numa CS" / "WoW Numa CS" tabs are only views: a month/week
dropdown in C1 plus FILTER() formulas over the raw tabs. So we read the raw
tabs and let the portal pick the period itself:

    Raw          monthly rows ("Created Month" 2026-01 or 2026-1)
    Raw-Weekly   weekly rows  ("Created Week" = the week's Monday, 2026-11-09)
    Raw-Daily    daily rows   (not in the sheet yet; picked up once it exists)

Columns are found by header name, not position (Raw-Weekly has a blank
column A). The sheet is read with the portal's Drive service account
(GOOGLE_DRIVE_SA_KEY_FILE), which Kim shared on the sheet as Viewer.

Each sync replaces one period type's rows in a single transaction, so the
table mirrors the sheet, including corrections and deletions. Guards: a tab
missing its key columns, with no data rows, or shrinking below MIN_KEEP_SHARE
of what we already hold is skipped (a half-pasted sheet must not wipe data).
"""
import logging
import os
import re
from datetime import date, datetime

import requests

from helpers.drive_timestamps import _sa_token

log = logging.getLogger('numa_stats')

SHEET_ID = os.getenv('NUMA_STATS_SHEET_ID', '1fewDc0GG1GDZuvyMLJ5U8K_aupnHC_-ZXp87Z_HGK8s')
TABS = (
    ('month', os.getenv('NUMA_STATS_MONTH_TAB', 'Raw')),
    ('week', os.getenv('NUMA_STATS_WEEK_TAB', 'Raw-Weekly')),
    ('day', os.getenv('NUMA_STATS_DAY_TAB', 'Raw-Daily')),
)
MIN_KEEP_SHARE = 0.5
TIMEOUT = 30
API = 'https://sheets.googleapis.com/v4/spreadsheets'

# DB column -> sheet header
COLUMNS = {
    'conversations': 'Unique Conversation Count',
    'email_csat': 'Avg Csat',
    'email_sla_pct': 'Email Message SLA Percentage',
    'whatsapp_csat_pct': 'CSAT - Whatsapp',
    'q1_pct': 'Average Q1 scores',
    'q2_pct': 'Average Q2 scores',
    'q3_pct': 'Average Q3 scores',
    'avg_response_sec': 'Avg Response Time',
    'median_response_sec': 'Median Response Time',
    'p90_response_sec': 'P90 Response Time',
    'avg_first_reply_sec': 'Average First Reply Time',
    'median_first_reply_sec': 'Median First Reply Time',
}
METRICS = tuple(COLUMNS)
INT_COLS = {'conversations'}
DURATION_COLS = {c for c in COLUMNS if c.endswith('_sec')}


class SheetFormatError(Exception):
    """A tab doesn't look like the raw export we expect."""


# ─── value parsing ──────────────────────────────────────────────────────────
def _norm_header(h):
    return re.sub(r'\s+', ' ', (h or '').strip()).lower()


def parse_number(v):
    """'1,099' -> 1099.0, '74.15%' -> 74.15, '' / junk -> None."""
    s = (v or '').strip().replace(',', '').rstrip('%').strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_duration(v):
    """'0:18:36' -> 1116, '1095:02:10' -> seconds; anything else -> None."""
    m = re.fullmatch(r'(\d+):([0-5]\d):([0-5]\d)', (v or '').strip())
    if not m:
        return None
    h, mi, s = (int(x) for x in m.groups())
    return h * 3600 + mi * 60 + s


def parse_period(kind, v):
    """Period start date for a 'Created …' cell, or None."""
    s = (v or '').strip()
    if not s:
        return None
    if kind == 'month':
        m = re.fullmatch(r'(\d{4})-(\d{1,2})(?:-\d{1,2})?', s)
        if m and 1 <= int(m.group(2)) <= 12:
            return date(int(m.group(1)), int(m.group(2)), 1)
        return None
    for fmt in ('%Y-%m-%d', '%m/%d/%Y', '%Y/%m/%d'):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def parse_tab(kind, values):
    """Sheet values (list of rows, header first) -> (rows, skipped).

    rows: list of dicts with period_type, period_start, username and METRICS.
    Raises SheetFormatError when the key columns are missing.
    """
    if not values:
        raise SheetFormatError('tab is empty')
    header = [_norm_header(h) for h in values[0]]

    def col(name):
        try:
            return header.index(_norm_header(name))
        except ValueError:
            return None

    i_user = col('Username')
    i_period = next((i for i, h in enumerate(header) if h.startswith('created ')), None)
    if i_user is None or i_period is None or col(COLUMNS['conversations']) is None:
        raise SheetFormatError('missing Username / Created … / Unique Conversation Count header')
    idx = {k: col(h) for k, h in COLUMNS.items()}

    out, skipped = {}, 0
    for raw in values[1:]:
        cell = lambda i: raw[i] if i is not None and i < len(raw) else ''  # noqa: E731
        user = cell(i_user).strip().lower()
        start = parse_period(kind, cell(i_period))
        if not user or not start:
            # blank filler rows (only the sheet's derived Month/Week formula) aren't errors
            if user or cell(i_period).strip():
                skipped += 1
            continue
        row = {'period_type': kind, 'period_start': start, 'username': user}
        for k, i in idx.items():
            v = cell(i)
            if k in DURATION_COLS:
                row[k] = parse_duration(v)
            else:
                n = parse_number(v)
                row[k] = int(round(n)) if (n is not None and k in INT_COLS) else n
        out[(start, user)] = row          # duplicate key: last row wins
    return list(out.values()), skipped


# ─── Google Sheets ──────────────────────────────────────────────────────────
def _get(url, params=None):
    r = requests.get(url, headers={'Authorization': f'Bearer {_sa_token()}'},
                     params=params, timeout=TIMEOUT)
    if r.status_code == 401:
        r = requests.get(url, headers={'Authorization': f'Bearer {_sa_token(force=True)}'},
                         params=params, timeout=TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f'Google Sheets returned HTTP {r.status_code}')
    return r.json()


def fetch_tabs():
    """{period_type: values} for each configured tab that exists in the sheet."""
    meta = _get(f'{API}/{SHEET_ID}', {'fields': 'sheets.properties.title'})
    titles = {s['properties']['title'] for s in meta.get('sheets', [])}
    wanted = [(k, t) for k, t in TABS if t in titles]
    if not wanted:
        return {}
    data = _get(f'{API}/{SHEET_ID}/values:batchGet',
                {'ranges': [f"'{t}'!A:Z" for _, t in wanted]})
    return {k: vr.get('values', []) for (k, _), vr in zip(wanted, data.get('valueRanges', []))}


# ─── DB write ───────────────────────────────────────────────────────────────
_INSERT = ('INSERT INTO numa_stats (period_type, period_start, username, '
           + ', '.join(METRICS) + ', synced_at) VALUES ('
           + ', '.join(['%s'] * (len(METRICS) + 4)) + ')')


def write_rows(conn, kind, rows, now, force=False):
    """Replace this period type's rows. Returns a one-line result string."""
    with conn.cursor() as c:
        c.execute('SELECT COUNT(*) AS n FROM numa_stats WHERE period_type=%s', (kind,))
        r = c.fetchone()
        have = int(r['n'] if isinstance(r, dict) else r[0])
        if not rows:
            return f'{kind}: SKIPPED, sheet has no data rows (kept {have})'
        if not force and have and len(rows) < have * MIN_KEEP_SHARE:
            return f'{kind}: SKIPPED, sheet has {len(rows)} rows vs {have} stored (possible bad paste)'
        try:
            c.execute('DELETE FROM numa_stats WHERE period_type=%s', (kind,))
            c.executemany(_INSERT, [
                (kind, r['period_start'], r['username'], *[r[m] for m in METRICS], now)
                for r in rows])
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return f'{kind}: {len(rows)} rows (was {have})'


def sync(conn, triggered_by='cron', live=True, force=False):
    """Read the sheet and (if live) refresh numa_stats. Returns (ok, lines)."""
    now = datetime.now().replace(microsecond=0)
    lines, ok = [], True
    log_id = None
    if live:
        with conn.cursor() as c:
            c.execute('SELECT GET_LOCK(%s, 0) AS got', ('numa_stats_sync',))
            r = c.fetchone()
            if not (r['got'] if isinstance(r, dict) else r[0]):
                return False, ['another sync is already running']
            c.execute('INSERT INTO numa_stats_sync_log (started_at, triggered_by) VALUES (%s, %s)',
                      (now, triggered_by[:190]))
            log_id = c.lastrowid
        conn.commit()
    try:
        tabs = fetch_tabs()
        if not tabs:
            ok = False
            lines.append('no raw tabs found in the sheet')
        for kind, tab in TABS:
            if kind not in tabs:
                if kind != 'day':
                    ok = False
                    lines.append(f'{kind}: tab "{tab}" not found')
                continue
            try:
                rows, skipped = parse_tab(kind, tabs[kind])
            except SheetFormatError as e:
                ok = False
                lines.append(f'{kind}: SKIPPED, {e}')
                continue
            note = f', {skipped} unreadable rows ignored' if skipped else ''
            if live:
                res = write_rows(conn, kind, rows, now, force=force)
                if 'SKIPPED' in res:
                    ok = False
                lines.append(res + note)
            else:
                periods = sorted({r['period_start'] for r in rows})
                span = f'{periods[0]} .. {periods[-1]}' if periods else '-'
                lines.append(f'{kind}: would load {len(rows)} rows, {len(periods)} periods ({span}){note}')
        # team-level Looker figures (second sheet); its own failure doesn't stop the agent data
        try:
            from helpers import numa_team_stats
            t_ok, t_lines = numa_team_stats.sync(conn, live=live)
            ok = ok and t_ok
            lines += t_lines
        except Exception as e:
            ok = False
            log.exception('numa team stats sync failed')
            lines.append(f'teams: error: {type(e).__name__}: {e}')
    except Exception as e:  # network / auth / DB
        ok = False
        log.exception('numa stats sync failed')
        lines.append(f'error: {type(e).__name__}: {e}')
    finally:
        if live:
            try:
                with conn.cursor() as c:
                    c.execute('UPDATE numa_stats_sync_log SET finished_at=%s, ok=%s, summary=%s WHERE id=%s',
                              (datetime.now().replace(microsecond=0), int(ok), '\n'.join(lines), log_id))
                    c.execute('SELECT RELEASE_LOCK(%s)', ('numa_stats_sync',))
                conn.commit()
            except Exception:
                log.exception('could not close numa_stats_sync_log row')
    return ok, lines
