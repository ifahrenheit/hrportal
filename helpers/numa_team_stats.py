"""Team-level Looker figures ("Jareth SLA Copy" sheet) -> central_db.numa_team_stats.

The sheet holds several Looker exports side by side on each tab. Each export
is a block of columns that starts with a "Team Attribute" header:

    SLAs          Chatbot SLA weekly / monthly / yearly, Email SLA weekly / monthly / yearly
                  (met, not met, percentage)
    conversation  Conversations weekly / monthly / YTD, one row per conversation type
    CSAT          CSAT scores weekly / monthly / YTD (Agent CSAT, Avg Csat, response rates, …)

Blocks are recognised by their headers, and the period type by the shape of the
period value (2026-11-16 -> week, 2026-11 -> month, 2026 -> year), so a block
can move or be reordered without code changes.

Only our two teams are kept (TEAM_MAP). Numa's other teams are never stored.
Rows are upserted, never deleted: Looker's weekly exports stop at 500 rows,
so older weeks drop off the sheet but stay in the portal.

Stored narrow: one row per (block, period_type, period_start, team, metric).
Percentages are kept as Looker gives them (fractions, 0.3488 = 34.88%).
"""
import re
from datetime import date, datetime

import requests

from helpers.drive_timestamps import _sa_token
import os

SHEET_ID = os.getenv('NUMA_TEAM_SHEET_ID', '1MMUbRgcDbxIJrNB-12atXsp-ITo-os7-6-FbcGWVCNo')
TABS = ('SLAs', 'conversation', 'CSAT')
# Looker "Team Attribute" -> portal team name
TEAM_MAP = {'Team Cohere': 'GX Team', 'GX Team Night Shift': 'GX Night Shift'}
TIMEOUT = 30
API = 'https://sheets.googleapis.com/v4/spreadsheets'

# odd Looker headers -> metric key
HEADER_ALIASES = {
    'filtered front_conversations_bq.weighted_q1': 'escalated_q1',
}
PERIOD_HEADERS = {'activity period', 'feedback period', 'activity timestamp year', 'created year'}


def metric_key(header):
    h = re.sub(r'\s+', ' ', (header or '').strip()).lower()
    if h in HEADER_ALIASES:
        return HEADER_ALIASES[h]
    h = h.replace('+', ' plus ').replace('%', ' pct ')
    return re.sub(r'[^a-z0-9]+', '_', h).strip('_')


def parse_period(v):
    """(period_type, start date) from '2026-11-16' / '2026-11' / '2026', else (None, None)."""
    s = (v or '').strip()
    m = re.fullmatch(r'(\d{4})-(\d{1,2})-(\d{1,2})', s)
    if m:
        return 'week', date(*map(int, m.groups()))
    m = re.fullmatch(r'(\d{4})-(\d{1,2})', s)
    if m and 1 <= int(m.group(2)) <= 12:
        return 'month', date(int(m.group(1)), int(m.group(2)), 1)
    if re.fullmatch(r'\d{4}', s):
        return 'year', date(int(s), 1, 1)
    return None, None


def parse_value(v):
    s = (v or '').strip().replace(',', '')
    if not s:
        return None
    pct = s.endswith('%')
    try:
        n = float(s.rstrip('%'))
    except ValueError:
        return None
    return n / 100 if pct else n


def block_kind(headers):
    hs = {h.strip().lower() for h in headers}
    if 'conversation type' in hs:
        return 'conversations'
    if 'whatsapp chatbot message sla met' in hs:
        return 'chatbot_sla'
    if 'email message sla met' in hs:
        return 'email_sla'
    if any('csat' in h for h in hs):
        return 'csat'
    return None


def split_blocks(values):
    """Yield (headers, data_rows) for each "Team Attribute" block on a tab."""
    hr = next((i for i, row in enumerate(values) if 'Team Attribute' in row), None)
    if hr is None:
        return
    hdr = values[hr]
    starts = [i for i, h in enumerate(hdr) if h.strip() == 'Team Attribute']
    for s in starts:
        e = s + 1
        while e < len(hdr) and hdr[e].strip() and hdr[e].strip() != 'Team Attribute':
            e += 1
        rows = [r[s:e] + [''] * max(0, e - len(r)) for r in values[hr + 1:] if len(r) > s and r[s].strip()]
        yield hdr[s:e], rows


def parse_tab(values):
    """-> list of (block, period_type, period_start, team, metric, value)."""
    out = []
    for headers, rows in split_blocks(values):
        kind = block_kind(headers)
        if not kind:
            continue
        low = [h.strip().lower() for h in headers]
        i_period = next((i for i, h in enumerate(low) if h in PERIOD_HEADERS), None)
        if i_period is None:
            continue
        i_type = low.index('conversation type') if kind == 'conversations' else None
        skip = {0, i_period} | ({i_type} if i_type is not None else set())
        if kind == 'conversations':
            skip |= {i for i, h in enumerate(low) if h == 'year'}      # Looker filler column
        for r in rows:
            team = TEAM_MAP.get(r[0].strip())
            ptype, start = parse_period(r[i_period])
            if not team or not ptype:
                continue
            prefix = ''
            if i_type is not None:
                prefix = re.sub(r'[^a-z0-9]+', '_', r[i_type].strip().lower()).strip('_') + '.'
            for i, h in enumerate(headers):
                if i in skip:
                    continue
                out.append((kind, ptype, start, team, prefix + metric_key(h), parse_value(r[i])))
    return out


def fetch():
    h = {'Authorization': f'Bearer {_sa_token()}'}
    r = requests.get(f'{API}/{SHEET_ID}/values:batchGet', headers=h, timeout=TIMEOUT,
                     params={'ranges': [f"'{t}'" for t in TABS]})
    if r.status_code != 200:
        raise RuntimeError(f'Google Sheets returned HTTP {r.status_code} for the team sheet')
    return [vr.get('values', []) for vr in r.json().get('valueRanges', [])]


_UPSERT = """INSERT INTO numa_team_stats (block, period_type, period_start, team, metric, value, synced_at)
             VALUES (%s, %s, %s, %s, %s, %s, %s)
             ON DUPLICATE KEY UPDATE value = VALUES(value), synced_at = VALUES(synced_at)"""


def sync(conn, live=True):
    """Returns (ok, lines)."""
    rows = []
    for values in fetch():
        rows += parse_tab(values)
    # same key twice in one read (shouldn't happen): last wins
    dedup = {r[:5]: r for r in rows}
    rows = list(dedup.values())
    counts = {}
    for b, p, *_ in rows:
        counts[(b, p)] = counts.get((b, p), 0) + 1
    summary = ', '.join(f'{b}/{p} {n}' for (b, p), n in sorted(counts.items()))
    if not rows:
        return False, ['teams: SKIPPED, no rows for our teams found in the team sheet']
    if not live:
        return True, [f'teams: would upsert {len(rows)} values ({summary})']
    now = datetime.now().replace(microsecond=0)
    with conn.cursor() as c:
        try:
            c.executemany(_UPSERT, [(*r, now) for r in rows])
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return True, [f'teams: {len(rows)} values upserted ({summary})']
