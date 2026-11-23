"""
SL Conversion report -- Analytics > SL Conversion (read-only)

Company policy 3.2.2 Sick Leave Conversion. Unused Sick Leave for an effective
year is converted and released every March 15 of the following year.

Tiers (unused SL hours for the effective year):
  >= 96 h -> 100%    >= 88 h -> 50%    >= 80 h -> 25%    < 80 h -> not convertible

To qualify:
  a. Regular as of Jan 1 of the effective year
     (regularization = gsheet_employees.operational_date + REG_MONTHS months).
  b. No AWOL during the year: leave4day_requests of the AWOL type (not rejected /
     cancelled / deleted) plus central_db.absence_records code 'AWOL'.
  c. No sickness filed as VL / LWOP / AWOL. Found by keyword match on the
     reason / notes, so it only makes the row "Needs review".
  d. Active at release time and not on notice: gsheet status 'Active' and no
     exit_date at all.

Scope: only employees whose gsheet status is 'Active' are listed; separated,
other statuses and employees with no gsheet_employees row are left out.

Data:
  unused SL = SUM(no_of_days - days_used) of the year's SL rows in
  orangehrm2.ohrm_leave_entitlement (deleted = 0), x 8 hours per day.
  Before the year ends the figures are a projection as of today.

app.py runs as __main__, so this module must not `from app import ...`.
init_sl_conversion() is called from app.py and hands over the helpers.
"""
import csv
import io
import re
from datetime import date, datetime

from flask import (Blueprint, Response, flash, redirect, render_template,
                   request, session, url_for)

from helpers.leave_policy import REG_MONTHS, add_months
from helpers.leave_credits import ACTIVE_STATUSES

sl_conversion_bp = Blueprint('sl_conversion', __name__)

_h = {}

HOURS_PER_DAY = 8
TOLERANCE = 0.02            # 95.99 h counts as 96 h
TIERS = ((96, 100), (88, 50), (80, 25))
STATUS_LABELS = {
    'qualified':       'Qualified',
    'review':          'Needs review',
    'not_qualified':   'Not qualified',
    'not_convertible': 'Not convertible',
}
STATUS_RANK = {'qualified': 0, 'review': 1, 'not_qualified': 2, 'not_convertible': 3}
SICK_CODES = ('VL', 'LWOP', 'AWOL')

# Word-boundary, case-insensitive. Fuzzy on purpose: a hit only flags the row
# for review. "rest day(s)" is excluded because it's a schedule term.
SICK_RE = re.compile(
    r"\b("
    r"sick\w*|ill|illness\w*|unwell|fever\w*|flu|influenza|"
    r"rest(?!\s*-?\s*days?\b)|resting|"
    r"hospital\w*|doctor\w*|dr|medic\w*|clinic\w*|check\s*-?\s*ups?|consult\w*|"
    r"headaches?|migraines?|toothaches?|tooth|teeth|dental|dentist\w*|eyes?|"
    r"lbm|diarrh\w*|vomit\w*|nausea\w*|cough\w*|colds?|sore|asthma\w*|dengue|"
    r"surgery|surgical|operation|operated|injur\w*|pain|painful|admitted|"
    r"infection\w*|allerg\w*|uti|pneumonia|covid\w*|stomach\w*|vertigo|"
    r"hypertension|fracture\w*|sprain\w*|tonsil\w*|pregnan\w*|prenatal|"
    r"ob-?gyne?|laboratory|x-?ray|ultrasound|therapy|confine\w*|"
    r"aches?|gout|chills?|pox|measles|hyperacidity|not\s+feeling\s+well"
    r")\b",
    re.IGNORECASE)


def init_sl_conversion(**helpers):
    """helpers: get_db (orangehrm2), get_central_db (central_db)."""
    _h.update(helpers)


# ── Pure rule logic (unit-testable, no DB) ───────────────────────────────────

def tier_for(hours):
    """Conversion % for unused SL hours (0 when not convertible)."""
    h = float(hours or 0)
    for threshold, pct in TIERS:
        if h + TOLERANCE >= threshold:
            return pct
    return 0


def sick_match(text):
    """The first sickness keyword in text, or None."""
    m = SICK_RE.search(text or '')
    return m.group(0) if m else None


def regularization_date(operational_date):
    d = _as_date(operational_date)
    return add_months(d, REG_MONTHS) if d else None


def release_date(year):
    return date(year + 1, 3, 15)


def is_projection(year, today):
    """True while the effective year hasn't ended (SL can still be used)."""
    return today <= date(year, 12, 31)


def _as_date(v):
    """DATE/DATETIME -> date; anything else (None, '0000-00-00' strings) -> None."""
    if isinstance(v, datetime):
        return v.date()
    return v if isinstance(v, date) else None


# Cells starting with these run as formulas in Excel / Sheets.
_FORMULA_PREFIX = ('=', '+', '-', '@', '\t', '\r')


def _cell(v):
    s = '' if v is None else str(v)
    return "'" + s if s.startswith(_FORMULA_PREFIX) else s


def _fmt_day(d):
    return f"{d.strftime('%b')} {d.day}"


def evaluate(emp, awol_list, sick_list, year, today):
    """Apply policy 3.2.2 to one employee.

    emp: dict with unused_days, and (when a gsheet row exists) has_master=True,
         status, operational_date, exit_date.
    awol_list: sorted list of AWOL dates in the year.
    sick_list: list of {'date', 'code', 'reason', 'keyword'} hits.
    Returns a dict: unused_hours, unused_days, reg_date, tier, converted_hours,
    converted_days, status, reasons (failed rules), review (review notes)."""
    unused_days = round(float(emp.get('unused_days') or 0), 4)
    unused_hours = round(unused_days * HOURS_PER_DAY, 2)
    tier = tier_for(unused_hours)
    converted_hours = round(unused_hours * tier / 100, 2)

    reasons, review = [], []
    reg = regularization_date(emp.get('operational_date'))

    if not emp.get('has_master'):
        reasons.append('No employee master record (gsheet_employees)')
    else:
        # a. regular by Jan 1
        if reg is None:
            reasons.append('No operational date (regularization unknown)')
        elif reg > date(year, 1, 1):
            reasons.append(f'Not regular by Jan 1 (reg {reg.isoformat()})')
        # d. active, not separated / on notice
        status = (emp.get('status') or '').strip()
        if status.casefold() == 'separated':
            reasons.append('Separated')
        elif status.casefold() != 'active':
            reasons.append(f'Status {status or "blank"}')
        # An exit_date followed by a later promotion_date is an internal move, not an exit
        # (same rule as the attrition report).
        exit_d, promo_d = _as_date(emp.get('exit_date')), _as_date(emp.get('promotion_date'))
        if exit_d and not (promo_d and promo_d > exit_d):
            reasons.append(f"Exit date {exit_d.isoformat()}")
        elif emp.get('exit_date') and not exit_d:
            review.append(f"Unreadable exit date '{emp['exit_date']}' in the sheet")

    # b. AWOL
    if awol_list:
        reasons.append(f'AWOL ×{len(awol_list)}: ' + ', '.join(_fmt_day(d) for d in awol_list))

    # c. sickness filed as VL / LWOP / AWOL
    for s in sick_list:
        reason = ' '.join((s.get('reason') or '').split())
        review.append(f"Sick reason as {s['code']}: {_fmt_day(s['date'])} '{reason}'")

    if tier == 0:
        status_key = 'not_convertible'
    elif reasons:
        status_key = 'not_qualified'
    elif review:
        status_key = 'review'
    else:
        status_key = 'qualified'

    return {
        'unused_hours': unused_hours,
        'unused_days': round(unused_days, 2),
        'reg_date': reg,
        'tier': tier,
        'converted_hours': converted_hours,
        'converted_days': round(converted_hours / HOURS_PER_DAY, 2),
        'status': status_key,
        'status_label': STATUS_LABELS[status_key],
        'reasons': reasons,
        'review': review,
    }


def sort_key(row):
    return (STATUS_RANK[row['status']], -row['converted_hours'], -row['unused_hours'],
            (row.get('name') or '').casefold())


def summarize(rows):
    s = {'t100': 0, 't50': 0, 't25': 0, 't0': 0, 'qualified': 0, 'review': 0,
         'not_qualified': 0, 'not_convertible': 0,
         'q100': 0, 'q50': 0, 'q25': 0, 'r100': 0, 'r50': 0, 'r25': 0,
         'qualified_days': 0.0, 'review_days': 0.0, 'total': len(rows)}
    for r in rows:
        s[{100: 't100', 50: 't50', 25: 't25'}.get(r['tier'], 't0')] += 1
        # Tier cards count qualified employees only (q*); review cases per
        # tier (r*) are shown underneath as "+N needs review".
        if r['tier'] in (100, 50, 25) and r['status'] in ('qualified', 'review'):
            s[('q' if r['status'] == 'qualified' else 'r') + str(r['tier'])] += 1
        s[r['status']] += 1
        if r['status'] == 'qualified':
            s['qualified_days'] += r['converted_days']
        elif r['status'] == 'review':
            s['review_days'] += r['converted_days']
    s['qualified_days'] = round(s['qualified_days'], 2)
    s['review_days'] = round(s['review_days'], 2)
    return s


def dedupe_sick(hits):
    """One hit per (date, code); keeps the first one seen."""
    seen, out = set(), []
    for h in sorted(hits, key=lambda x: (x['date'], x['code'])):
        k = (h['date'], h['code'])
        if k not in seen:
            seen.add(k)
            out.append(h)
    return out


# ── Data loading (batched per year, no per-employee queries) ─────────────────

def _type_ids(c):
    c.execute("SELECT id, name FROM ohrm_leave_type WHERE deleted = 0")
    by_name = {r['name'].strip().casefold(): r['id'] for r in c.fetchall()}

    def find(*names):
        for n in names:
            if n.casefold() in by_name:
                return by_name[n.casefold()]
        return None
    return {
        'SL': find('Sick Leave'),
        'VL': find('Vacation Leave'),
        'LWOP': find('Leave Without Pay (LWOP)', 'Leave Without Pay', 'LWOP'),
        'AWOL': find('AWOL'),
    }


def available_years():
    db = _h['get_db']()
    try:
        with db.cursor() as c:
            sl = _type_ids(c)['SL']
            if not sl:
                return []
            c.execute("""SELECT DISTINCT YEAR(from_date) AS y FROM ohrm_leave_entitlement
                         WHERE leave_type_id = %s AND deleted = 0 ORDER BY y DESC""", (sl,))
            return [int(r['y']) for r in c.fetchall() if r['y']]
    finally:
        db.close()


def build_report(year, today=None):
    """Every Active employee (gsheet status) with an SL entitlement row in `year`, evaluated."""
    today = today or date.today()
    y_from, y_to = date(year, 1, 1), date(year, 12, 31)

    db = _h['get_db']()
    try:
        with db.cursor() as c:
            types = _type_ids(c)
            if not types['SL']:
                return []
            c.execute("""SELECT e.emp_number, h.employee_id,
                                TRIM(CONCAT(COALESCE(h.emp_firstname, ''), ' ',
                                            COALESCE(h.emp_lastname, ''))) AS ohrm_name,
                                SUM(e.no_of_days - e.days_used) AS unused_days
                         FROM ohrm_leave_entitlement e
                         JOIN hs_hr_employee h ON h.emp_number = e.emp_number
                         WHERE e.leave_type_id = %s AND e.deleted = 0
                           AND e.from_date >= %s AND e.from_date < %s
                         GROUP BY e.emp_number, h.employee_id, ohrm_name""",
                      (types['SL'], y_from, date(year + 1, 1, 1)))
            ents = c.fetchall()

            want = {k: types[k] for k in ('VL', 'LWOP', 'AWOL') if types[k]}
            portal = []
            if want:
                fmt = ','.join(['%s'] * len(want))
                c.execute(f"""SELECT emp_number, employee_id, leave_type_id, leave_date, notes
                              FROM leave4day_requests
                              WHERE leave_type_id IN ({fmt})
                                AND leave_date BETWEEN %s AND %s
                                AND status IN ({','.join(['%s'] * len(ACTIVE_STATUSES))})
                                AND deleted_at IS NULL""",
                          (*want.values(), y_from, y_to, *ACTIVE_STATUSES))
                portal = c.fetchall()
    finally:
        db.close()

    cdb = _h['get_central_db']()
    try:
        with cdb.cursor() as c:
            c.execute("""SELECT employee_id, zd_name, email, status, group_name, account,
                                operational_date, exit_date, promotion_date
                         FROM gsheet_employees""")
            master = c.fetchall()
            fmt = ','.join(['%s'] * len(SICK_CODES))
            c.execute(f"""SELECT employee_id, email, absence_date, code, reason
                          FROM absence_records
                          WHERE absence_date BETWEEN %s AND %s AND code IN ({fmt})
                            AND COALESCE(approval_status, '') NOT IN ('Disapproved', 'Exempted')""",
                      (y_from, y_to, *SICK_CODES))
            absences = c.fetchall()
    finally:
        cdb.close()

    by_eid = {(m['employee_id'] or '').strip(): m for m in master if m['employee_id']}
    eid_by_email = {}
    for m in master:
        if m['email'] and m['employee_id']:
            eid_by_email.setdefault(m['email'].strip().casefold(), m['employee_id'].strip())
    eid_by_empnum = {e['emp_number']: (e['employee_id'] or '').strip() for e in ents}
    code_by_type = {v: k for k, v in want.items()}

    awol = {}    # employee_id -> set(dates)
    sick = {}    # employee_id -> [hits]
    for a in absences:
        eid = (a['employee_id'] or '').strip()
        if not eid and a['email']:
            eid = eid_by_email.get(a['email'].strip().casefold(), '')
        if not eid or not a['absence_date']:
            continue
        code = (a['code'] or '').strip().upper()
        if code == 'AWOL':
            awol.setdefault(eid, set()).add(a['absence_date'])
        kw = sick_match(a['reason'])
        if kw:
            sick.setdefault(eid, []).append({'date': a['absence_date'], 'code': code,
                                             'reason': a['reason'], 'keyword': kw})
    for p in portal:
        eid = (p['employee_id'] or '').strip() or eid_by_empnum.get(p['emp_number'], '')
        if not eid:
            continue
        code = code_by_type.get(p['leave_type_id'])
        if code == 'AWOL':
            awol.setdefault(eid, set()).add(p['leave_date'])
        kw = sick_match(p['notes'])
        if kw:
            sick.setdefault(eid, []).append({'date': p['leave_date'], 'code': code,
                                             'reason': p['notes'], 'keyword': kw})

    rows = []
    for e in ents:
        eid = (e['employee_id'] or '').strip()
        m = by_eid.get(eid)
        # Active employees only: separated / other statuses and anyone without a
        # gsheet_employees row are left out of the report entirely.
        if not m or (m['status'] or '').strip().casefold() != 'active':
            continue
        emp = {'unused_days': e['unused_days'], 'has_master': bool(m)}
        if m:
            emp.update(status=m['status'], operational_date=m['operational_date'],
                       exit_date=m['exit_date'], promotion_date=m.get('promotion_date'))
        res = evaluate(emp, sorted(awol.get(eid, ())), dedupe_sick(sick.get(eid, [])), year, today)
        res.update(
            emp_number=e['emp_number'],
            employee_id=eid,
            name=(e['ohrm_name'] or '').strip() or (m['zd_name'] if m else '') or eid,
            group_name=(m['group_name'] if m else '') or '',
            account=(m['account'] if m else '') or '',
            emp_status=(m['status'] if m else '') or '',
        )
        rows.append(res)
    rows.sort(key=sort_key)
    return rows


TIER_OPTIONS = (100, 50, 25, 0)


def filter_rows(rows, group='', status='all', q='', tier=None):
    """group / q filters (summary scope) and status / tier filters, returned separately."""
    scoped = rows
    if group:
        g = group.casefold()
        scoped = [r for r in scoped if r['group_name'].casefold() == g]
    if q:
        ql = q.casefold()
        scoped = [r for r in scoped
                  if ql in r['name'].casefold() or ql in r['employee_id'].casefold()]
    shown = scoped if status == 'all' else [r for r in scoped if r['status'] == status]
    if tier is not None:
        shown = [r for r in shown if r['tier'] == tier]
    return scoped, shown


# ── Route ────────────────────────────────────────────────────────────────────

def _allowed():
    perms = session.get('permissions') or {}
    return bool(session.get('is_admin') or perms.get('can_entitlements') or perms.get('can_reports'))


def _csv_response(rows, year):
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(['Employee ID', 'Name', 'Group', 'Account', 'Gsheet Status', 'Regularization Date',
                'Unused SL Hours', 'Unused SL Days', 'Tier %', 'Converted Hours',
                'Converted Days', 'Status', 'Reasons', 'Needs Review'])
    for r in rows:
        w.writerow([_cell(r['employee_id']), _cell(r['name']), _cell(r['group_name']),
                    _cell(r['account']), _cell(r['emp_status']),
                    r['reg_date'].isoformat() if r['reg_date'] else '',
                    f"{r['unused_hours']:.2f}", f"{r['unused_days']:.2f}", r['tier'],
                    f"{r['converted_hours']:.2f}", f"{r['converted_days']:.2f}",
                    r['status_label'], _cell('; '.join(r['reasons'])), _cell('; '.join(r['review']))])
    return Response(out.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition':
                             f'attachment; filename=sl_conversion_{year}.csv',
                             'Cache-Control': 'no-store'})


@sl_conversion_bp.route('/admin/reports/sl-conversion')
def sl_conversion_report():
    if not session.get('user'):
        return redirect(url_for('login'))
    if not _allowed():
        flash('You do not have permission to access this page.', 'danger')
        return redirect(url_for('dashboard'))

    today = date.today()
    try:
        year = int(request.args.get('year') or today.year)
    except (TypeError, ValueError):
        year = today.year
    if not 2000 <= year <= today.year + 1:
        year = today.year

    group = (request.args.get('group') or '').strip()
    status = request.args.get('status') or 'all'
    if status != 'all' and status not in STATUS_LABELS:
        status = 'all'
    q = (request.args.get('q') or '').strip()[:100]
    try:
        tier = int(request.args.get('tier', ''))
    except (TypeError, ValueError):
        tier = None
    if tier not in TIER_OPTIONS:
        tier = None

    rows = build_report(year, today)
    scoped, shown = filter_rows(rows, group, status, q, tier)

    if request.args.get('format') == 'csv':
        return _csv_response(shown, year)

    years = available_years()
    if year not in years:
        years = sorted(set(years) | {year}, reverse=True)
    groups = sorted({r['group_name'] for r in rows if r['group_name']}, key=str.casefold)

    return render_template(
        'admin/sl_conversion.html',
        rows=shown, summary=summarize(scoped), total=len(rows),
        year=year, years=years, group=group, groups=groups, status=status, q=q, tier=tier,
        status_labels=STATUS_LABELS, projection=is_projection(year, today),
        release=release_date(year), today=today, reg_months=REG_MONTHS,
    )
