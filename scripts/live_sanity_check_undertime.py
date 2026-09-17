"""
READ-ONLY live sanity check for /admin/undertime-report against real
orangehrm2 + central_db data. No INSERT/UPDATE/DELETE anywhere in this
script or in the route code it exercises (admin_undertime_report() is
SELECT-only against employee_schedules, dailytimerecordsfiltered [via
build_attendance_map()] and leave4day_requests -- confirmed by reading the
full route source before writing this script). Does not print any secret
(.env password) values, and prints no PII beyond employee_id.

Approach: mirrors scripts/live_sanity_check_awol.py -- rather than
re-implementing the undertime algorithm independently, this calls the REAL
route function in-process via Flask's test client, with a faked admin
session, against the REAL databases, for a short recent completed date
range. It checks the one sanity invariant that must hold if the route's
own filter (`shortfall_hours > 0`) is implemented correctly on real data,
not just in mocked fixtures:

    every row returned by the route has shortfall_hours > 0

(nothing with a zero or negative shortfall -- i.e. a full or over-worked
shift -- should ever appear in the output).
"""
import sys
from datetime import date, timedelta, datetime as dt

sys.path.insert(0, '/var/www/html/leavesystem')
import app as appmod  # noqa: E402

app = appmod.app
app.config['TESTING'] = True

# ── Safe, recent, already-completed date range (mirrors the route's own
# cutoff logic so we never ask for a day whose shifts haven't finished) ──
TODAY_CUTOFF_HOUR = 13
now = dt.now()
if now.hour < TODAY_CUTOFF_HOUR:
    safe_last_day = date.today() - timedelta(days=2)
else:
    safe_last_day = date.today() - timedelta(days=1)
date_to = safe_last_day
date_from = safe_last_day - timedelta(days=3)  # 4-day window

print(f"Date range under test: {date_from} .. {date_to}")

# ── Capture render_template kwargs without preventing the real template
# from actually rendering (so a Jinja bug would still surface as an
# exception here, same as a real request) ──
captured = {}
_orig_render_template = appmod.render_template


def _capturing_render_template(template_name_or_list, **context):
    captured[template_name_or_list] = context
    return _orig_render_template(template_name_or_list, **context)


appmod.render_template = _capturing_render_template

client = app.test_client()
with client.session_transaction() as sess:
    sess['user'] = {'name': 'Live Sanity Check', 'employee_id': 'LIVECHECK', 'email': 'livecheck@example.com'}
    sess['is_admin'] = True
    sess['permissions'] = {}

qs = {'date_from': str(date_from), 'date_to': str(date_to)}

resp_undertime = client.get('/admin/undertime-report', query_string=qs)

print(f"admin_undertime_report status: {resp_undertime.status_code}")

if resp_undertime.status_code != 200:
    print("FAIL: route did not return 200 -- see traceback above.")
    sys.exit(1)

ctx = captured.get('admin/undertime_report.html', {})
records = ctx.get('records', [])

print()
print(f"Undertime count: {len(records)}")
print()
print("Sample rows (up to 8, employee_id + date + scheduled_hours + "
      "hours_worked + shortfall_hours only):")
for r in records[:8]:
    print(f"  employee_id={r.get('employee_id')!r}  date={r.get('absent_date')}  "
          f"scheduled_hours={r.get('scheduled_hours')}  "
          f"hours_worked={r.get('hours_worked')}  "
          f"shortfall_hours={r.get('shortfall_hours')}")
if not records:
    print("  (no undertime rows in this date range)")

# ── Sanity invariant: every returned row must have shortfall_hours > 0.
# This is the route's own filter boundary (UNDERTIME_GRACE_MINUTES=0,
# `shortfall_hours > (UNDERTIME_GRACE_MINUTES / 60)`) -- confirming it on
# real data, not just fixtures, catches a real-world float-precision edge
# case (e.g. a shift that "should" be exactly 0 shortfall coming out as
# -1e-13 or 1e-13 due to rounding) that a hand-built mock fixture would
# never naturally exercise. ──
bad_rows = [r for r in records if not (r.get('shortfall_hours') is not None and r['shortfall_hours'] > 0)]

print()
invariant_holds = len(bad_rows) == 0
print(f"Invariant 'every row has shortfall_hours > 0' holds: {invariant_holds}")
if not invariant_holds:
    print(f"FAIL: {len(bad_rows)} row(s) violate the shortfall_hours > 0 invariant:")
    for r in bad_rows[:10]:
        print(f"  employee_id={r.get('employee_id')!r}  date={r.get('absent_date')}  "
              f"shortfall_hours={r.get('shortfall_hours')}")

print()
print("DONE -- read-only, no writes performed, no secrets printed.")
