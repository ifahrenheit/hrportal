"""
READ-ONLY live sanity check for /admin/awol-report against real orangehrm2 +
central_db data. No INSERT/UPDATE/DELETE anywhere in this script or in the
route code it exercises (admin_awol_report / admin_absences are both
SELECT-only). Does not print any secret (.env password) values.

Approach: rather than re-implementing the AWOL algorithm independently (which
would just be a second copy of the same complex attendance-pairing logic,
and could share the same bugs), this calls the REAL route functions
in-process via Flask's test client, with a faked admin session, against the
REAL databases. It cross-checks the sanity invariant:

    AWOL count <= Absent count (for the same date range)

since AWOL is defined as Absent minus (leave-covered or OT-covered), i.e. a
strict subset of the "Absent" bucket admin_absences() reports.
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

resp_awol = client.get('/admin/awol-report', query_string=qs)
resp_abs = client.get('/admin/absences', query_string=qs)

print(f"admin_awol_report status: {resp_awol.status_code}")
print(f"admin_absences status:    {resp_abs.status_code}")

if resp_awol.status_code != 200 or resp_abs.status_code != 200:
    print("FAIL: one or both routes did not return 200 -- see traceback above.")
    sys.exit(1)

awol_ctx = captured.get('admin/awol_report.html', {})
abs_ctx = captured.get('admin/absences.html', {})

awol_records = awol_ctx.get('records', [])
awol_count = len(awol_records)

absent_count = abs_ctx.get('summary', {}).get('absent', 0)
fts_in_count = abs_ctx.get('summary', {}).get('fts_in', 0)
fts_out_count = abs_ctx.get('summary', {}).get('fts_out', 0)

print()
print(f"AWOL count:        {awol_count}")
print(f"Absent count:      {absent_count}  (admin_absences summary.absent)")
print(f"FTS IN count:      {fts_in_count}")
print(f"FTS OUT count:     {fts_out_count}")
print()

invariant_holds = awol_count <= absent_count
print(f"Invariant AWOL <= Absent holds: {invariant_holds}")
if not invariant_holds:
    print("FAIL: AWOL count exceeds Absent count -- this should be impossible "
          "since AWOL is defined as a strict subset of Absent. Flag to reviewer.")

print()
print(f"Sample AWOL rows (up to 5, employee_id + date only):")
for r in awol_records[:5]:
    print(f"  employee_id={r.get('employee_id')!r}  date={r.get('absent_date')}")

if not awol_records:
    print("  (no AWOL rows in this date range)")

print()
print("DONE -- read-only, no writes performed, no secrets printed.")
