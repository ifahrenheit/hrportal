"""
Mocked/offline tests for the new /admin/undertime-report route
(admin_undertime_report() in app.py, ~line 10993) and its template
templates/admin/undertime_report.html.

Stage 2 (tester) of the house ship workflow. Run with:
    ./venv/bin/python -m unittest tests.test_undertime_report -v
from the repo root.

Design notes
------------
* Same RoutingCursor/RoutingConn/make_db_factory pattern as
  tests/test_awol_report.py: get_central_db()/get_db() are monkeypatched
  with fakes that route fetchall() results by which table name appears in
  the executed query text.
* Attendance fixtures are RAW punch logs (personid/date/type) fed to the
  REAL (unmocked) build_attendance_map(), not pre-computed 'hours_worked'
  values -- so the actual IN/OUT pairing and hours_worked math run for
  real in every test, same as the AWOL suite's approach for status
  derivation.
* Most correctness tests exercise the REAL template via app.test_client(),
  so a Jinja bug would actually be caught here. A few tests additionally
  capture the render_template() kwargs (records=...) to assert exact
  numeric shortfall/sort values that are awkward to pull back out of
  rendered HTML.
"""
import sys
import os
import unittest
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch

sys.path.insert(0, '/var/www/html/leavesystem')

import app as appmod  # noqa: E402  (import after sys.path tweak)

app = appmod.app
app.config['TESTING'] = True
app.config['WTF_CSRF_ENABLED'] = False


# ──────────────────────────────────────────────────────────────────
# Fakes (same shape as tests/test_awol_report.py)
# ──────────────────────────────────────────────────────────────────
LEAVE_STATUS_EXCLUDED = {'rejected', 'cancelled', 'deleted'}


class RoutingCursor:
    def __init__(self, table_rows):
        self.table_rows = table_rows
        self.last_query = None
        self.last_params = None
        self._rows_for_last_query = []
        self.execute = MagicMock(side_effect=self._execute)

    def _execute(self, query, params=None):
        self.last_query = query
        self.last_params = params
        q = query.lower()
        matched = []
        for table, rows in self.table_rows.items():
            if table.lower() in q:
                matched.append((table, rows))
        if matched:
            matched.sort(key=lambda tr: -len(tr[0]))
            table, rows = matched[0]
            if table == 'leave4day_requests':
                rows = [r for r in rows if (r.get('status') or '').lower() not in LEAVE_STATUS_EXCLUDED]
            self._rows_for_last_query = rows
        else:
            self._rows_for_last_query = []

    def fetchall(self):
        return list(self._rows_for_last_query)

    def fetchone(self):
        rows = self._rows_for_last_query
        return rows[0] if rows else None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class RoutingConn:
    def __init__(self, table_rows, cursor_log):
        self._cursor = RoutingCursor(table_rows)
        cursor_log.append(self._cursor)
        self.closed = False

    def cursor(self):
        return self._cursor

    def close(self):
        self.closed = True


def make_db_factory(table_rows, cursor_log):
    def factory():
        return RoutingConn(table_rows, cursor_log)
    return factory


def admin_session(sess, is_admin=True, can_absences=False):
    sess['user'] = {'name': 'Test Admin', 'employee_id': 'TESTADMIN', 'email': 'testadmin@example.com'}
    sess['is_admin'] = is_admin
    sess['permissions'] = {'can_absences': can_absences}


def punch(pid, dt_in, dt_out=None, typ=None):
    """Build one raw dailytimerecordsfiltered-shaped row."""
    return {'personid': pid, 'date': dt_in, 'type': typ}


# ──────────────────────────────────────────────────────────────────
# Access control
# ──────────────────────────────────────────────────────────────────
class TestUndertimeAccessControl(unittest.TestCase):

    def setUp(self):
        self.client = app.test_client()

    def test_no_session_redirects_and_never_touches_db(self):
        central_calls = MagicMock(side_effect=AssertionError('get_central_db should not be called'))
        db_calls = MagicMock(side_effect=AssertionError('get_db should not be called'))
        with patch.object(appmod, 'get_central_db', central_calls), \
             patch.object(appmod, 'get_db', db_calls):
            resp = self.client.get('/admin/undertime-report')
        self.assertIn(resp.status_code, (301, 302, 303, 307, 308),
                       'Unauthenticated request must redirect, not render/crash')
        self.assertNotEqual(resp.status_code, 200)
        central_calls.assert_not_called()
        db_calls.assert_not_called()

    def test_wrong_role_redirects_and_never_touches_db(self):
        central_calls = MagicMock(side_effect=AssertionError('get_central_db should not be called'))
        db_calls = MagicMock(side_effect=AssertionError('get_db should not be called'))
        with patch.object(appmod, 'get_central_db', central_calls), \
             patch.object(appmod, 'get_db', db_calls):
            with self.client.session_transaction() as sess:
                admin_session(sess, is_admin=False, can_absences=False)
            resp = self.client.get('/admin/undertime-report')
        self.assertIn(resp.status_code, (301, 302, 303, 307, 308))
        central_calls.assert_not_called()
        db_calls.assert_not_called()

    def test_is_admin_true_passes_gate(self):
        cursor_log = []
        table_rows = {'employee_schedules': [], 'dailytimerecordsfiltered': [], 'leave4day_requests': []}
        with patch.object(appmod, 'get_central_db', make_db_factory(table_rows, cursor_log)), \
             patch.object(appmod, 'get_db', make_db_factory(table_rows, cursor_log)):
            with self.client.session_transaction() as sess:
                admin_session(sess, is_admin=True, can_absences=False)
            resp = self.client.get('/admin/undertime-report')
        self.assertEqual(resp.status_code, 200, 'is_admin=True must pass the gate and render')
        self.assertTrue(len(cursor_log) >= 1, 'gate passing should have reached the DB layer')

    def test_can_absences_true_passes_gate_even_without_is_admin(self):
        cursor_log = []
        table_rows = {'employee_schedules': [], 'dailytimerecordsfiltered': [], 'leave4day_requests': []}
        with patch.object(appmod, 'get_central_db', make_db_factory(table_rows, cursor_log)), \
             patch.object(appmod, 'get_db', make_db_factory(table_rows, cursor_log)):
            with self.client.session_transaction() as sess:
                admin_session(sess, is_admin=False, can_absences=True)
            resp = self.client.get('/admin/undertime-report')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(len(cursor_log) >= 1)


# ──────────────────────────────────────────────────────────────────
# Filtering correctness (the core undertime logic)
# ──────────────────────────────────────────────────────────────────
class TestUndertimeFiltering(unittest.TestCase):

    def setUp(self):
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            admin_session(sess, is_admin=True)

    def _get(self, date_from, date_to, table_rows):
        cursor_log = []
        with patch.object(appmod, 'get_central_db', make_db_factory(table_rows, cursor_log)), \
             patch.object(appmod, 'get_db', make_db_factory(table_rows, cursor_log)):
            resp = self.client.get('/admin/undertime-report',
                                    query_string={'date_from': str(date_from), 'date_to': str(date_to)})
        return resp, cursor_log

    def _get_with_capture(self, date_from, date_to, table_rows):
        cursor_log = []
        captured = {}
        orig_render = appmod.render_template

        def capturing(name, **ctx):
            captured[name] = ctx
            return orig_render(name, **ctx)

        with patch.object(appmod, 'get_central_db', make_db_factory(table_rows, cursor_log)), \
             patch.object(appmod, 'get_db', make_db_factory(table_rows, cursor_log)), \
             patch.object(appmod, 'render_template', capturing):
            resp = self.client.get('/admin/undertime-report',
                                    query_string={'date_from': str(date_from), 'date_to': str(date_to)})
        return resp, cursor_log, captured

    def test_full_shift_worked_exactly_is_not_flagged(self):
        # 7am-4pm = 9.0 scheduled hours. IN 07:00, OUT 16:00 -> hours_worked
        # 9.0 exactly -> shortfall 0.0 -> with UNDERTIME_GRACE_MINUTES=0,
        # "> 0" must be strictly false, so this must NOT appear.
        d = date(2026, 9, 1)
        scheduled = [{
            'employee_id': 'EMP-EXACT', 'schedule_name': 'Exact Shift Employee', 'tl': 'TL1',
            'group_name': 'Group A', 'shift_time': '7am-4pm', 'absent_date': d, 'personid': 301,
        }]
        raw_logs = [
            {'personid': 301, 'date': datetime(2026, 9, 1, 7, 0, 0), 'type': 'in'},
            {'personid': 301, 'date': datetime(2026, 9, 1, 16, 0, 0), 'type': 'out'},
        ]
        table_rows = {'employee_schedules': scheduled, 'dailytimerecordsfiltered': raw_logs,
                       'leave4day_requests': []}
        resp, _, captured = self._get_with_capture(d, d, table_rows)
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode()
        self.assertNotIn('Exact Shift Employee', html)
        self.assertIn('No undertime occurrences', html)
        records = captured.get('admin/undertime_report.html', {}).get('records', [])
        self.assertEqual([r for r in records if r['employee_id'] == 'EMP-EXACT'], [])

    def test_shortfall_worked_less_is_flagged_with_correct_amount(self):
        # 9.0 scheduled hours, worked 7.5 -> shortfall 1.5 exactly.
        d = date(2026, 9, 1)
        scheduled = [{
            'employee_id': 'EMP-SHORT', 'schedule_name': 'Short Shift Employee', 'tl': 'TL1',
            'group_name': 'Group A', 'shift_time': '7am-4pm', 'absent_date': d, 'personid': 302,
        }]
        raw_logs = [
            {'personid': 302, 'date': datetime(2026, 9, 1, 7, 0, 0), 'type': 'in'},
            {'personid': 302, 'date': datetime(2026, 9, 1, 14, 30, 0), 'type': 'out'},
        ]
        table_rows = {'employee_schedules': scheduled, 'dailytimerecordsfiltered': raw_logs,
                       'leave4day_requests': []}
        resp, _, captured = self._get_with_capture(d, d, table_rows)
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode()
        self.assertIn('Short Shift Employee', html)
        self.assertIn('Undertime: <strong>1</strong>', html)
        records = captured['admin/undertime_report.html']['records']
        rec = next(r for r in records if r['employee_id'] == 'EMP-SHORT')
        self.assertAlmostEqual(rec['scheduled_hours'], 9.0, places=4)
        self.assertAlmostEqual(rec['hours_worked'], 7.5, places=4)
        self.assertAlmostEqual(rec['shortfall_hours'], 1.5, places=4)
        self.assertIn('-1h 30m', html)

    def test_overtime_worked_more_is_not_flagged(self):
        # 9.0 scheduled hours, worked 10.0 (overtime, not undertime).
        d = date(2026, 9, 1)
        scheduled = [{
            'employee_id': 'EMP-OT', 'schedule_name': 'Overtime Employee', 'tl': 'TL1',
            'group_name': 'Group A', 'shift_time': '7am-4pm', 'absent_date': d, 'personid': 303,
        }]
        raw_logs = [
            {'personid': 303, 'date': datetime(2026, 9, 1, 7, 0, 0), 'type': 'in'},
            {'personid': 303, 'date': datetime(2026, 9, 1, 17, 0, 0), 'type': 'out'},
        ]
        table_rows = {'employee_schedules': scheduled, 'dailytimerecordsfiltered': raw_logs,
                       'leave4day_requests': []}
        resp, _ = self._get(d, d, table_rows)
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode()
        self.assertNotIn('Overtime Employee', html)
        self.assertIn('No undertime occurrences', html)

    def test_fts_in_status_not_considered_no_crash(self):
        # Lone 'out' punch after noon, no prior 'in' -> build_attendance_map
        # assigns FTS IN (no 'hours_worked' key at all) -- must not crash.
        d = date(2026, 9, 1)
        scheduled = [{
            'employee_id': 'EMP-FTSIN', 'schedule_name': 'FTS IN Employee', 'tl': 'TL1',
            'group_name': 'Group A', 'shift_time': '7am-4pm', 'absent_date': d, 'personid': 304,
        }]
        raw_logs = [{'personid': 304, 'date': datetime(2026, 9, 1, 14, 0, 0), 'type': 'out'}]
        table_rows = {'employee_schedules': scheduled, 'dailytimerecordsfiltered': raw_logs,
                       'leave4day_requests': []}
        resp, _ = self._get(d, d, table_rows)
        self.assertEqual(resp.status_code, 200, 'FTS IN row must not crash the route')
        html = resp.data.decode()
        self.assertNotIn('FTS IN Employee', html)
        self.assertIn('No undertime occurrences', html)

    def test_fts_out_status_not_considered_no_crash(self):
        # Lone 'in' punch late evening, nothing to pair -> FTS OUT (no
        # 'hours_worked' key) -- must not crash.
        d = date(2026, 9, 1)
        scheduled = [{
            'employee_id': 'EMP-FTSOUT', 'schedule_name': 'FTS OUT Employee', 'tl': 'TL1',
            'group_name': 'Group A', 'shift_time': '7am-4pm', 'absent_date': d, 'personid': 305,
        }]
        raw_logs = [{'personid': 305, 'date': datetime(2026, 9, 1, 20, 30, 0), 'type': 'in'}]
        table_rows = {'employee_schedules': scheduled, 'dailytimerecordsfiltered': raw_logs,
                       'leave4day_requests': []}
        resp, _ = self._get(d, d, table_rows)
        self.assertEqual(resp.status_code, 200, 'FTS OUT row must not crash the route')
        html = resp.data.decode()
        self.assertNotIn('FTS OUT Employee', html)
        self.assertIn('No undertime occurrences', html)

    def test_absent_no_attendance_entry_not_considered(self):
        d = date(2026, 9, 1)
        scheduled = [{
            'employee_id': 'EMP-ABSENT', 'schedule_name': 'Absent Employee', 'tl': 'TL1',
            'group_name': 'Group A', 'shift_time': '7am-4pm', 'absent_date': d, 'personid': 306,
        }]
        table_rows = {'employee_schedules': scheduled, 'dailytimerecordsfiltered': [],
                       'leave4day_requests': []}
        resp, _ = self._get(d, d, table_rows)
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode()
        self.assertNotIn('Absent Employee', html)
        self.assertIn('No undertime occurrences', html)

    def test_null_personid_not_considered_no_crash(self):
        d = date(2026, 9, 1)
        scheduled = [{
            'employee_id': 'EMP-NULLPID', 'schedule_name': 'Null Personid Employee', 'tl': 'TL1',
            'group_name': 'Group A', 'shift_time': '7am-4pm', 'absent_date': d, 'personid': None,
        }]
        table_rows = {'employee_schedules': scheduled, 'dailytimerecordsfiltered': [],
                       'leave4day_requests': []}
        resp, _ = self._get(d, d, table_rows)
        self.assertEqual(resp.status_code, 200, 'NULL personid must not crash the route')
        html = resp.data.decode()
        self.assertNotIn('Null Personid Employee', html)
        self.assertIn('No undertime occurrences', html)

    def test_overnight_shift_wraparound_scheduled_hours_and_shortfall(self):
        # '10pm-7am' -> start_hour=22, end_hour=7. end_h > start_h is False,
        # so scheduled_hours must be computed as end_h + 24 - start_h = 9.0,
        # never a negative number. Worked 7.0h (IN 22:00 d, OUT 05:00 d+1)
        # -> shortfall must be exactly 2.0.
        d = date(2026, 9, 5)
        scheduled = [{
            'employee_id': 'EMP-NIGHT', 'schedule_name': 'Overnight Employee', 'tl': 'TL1',
            'group_name': 'Group A', 'shift_time': '10pm-7am', 'absent_date': d, 'personid': 307,
        }]
        raw_logs = [
            {'personid': 307, 'date': datetime(2026, 9, 5, 22, 0, 0), 'type': 'in'},
            {'personid': 307, 'date': datetime(2026, 9, 6, 5, 0, 0), 'type': 'out'},
        ]
        table_rows = {'employee_schedules': scheduled, 'dailytimerecordsfiltered': raw_logs,
                       'leave4day_requests': []}
        resp, _, captured = self._get_with_capture(d, d, table_rows)
        self.assertEqual(resp.status_code, 200)
        records = captured['admin/undertime_report.html']['records']
        rec = next(r for r in records if r['employee_id'] == 'EMP-NIGHT')
        self.assertAlmostEqual(rec['scheduled_hours'], 9.0, places=4,
                                msg='overnight wraparound must give 9.0, not a negative number')
        self.assertGreater(rec['scheduled_hours'], 0)
        self.assertAlmostEqual(rec['hours_worked'], 7.0, places=4)
        self.assertAlmostEqual(rec['shortfall_hours'], 2.0, places=4)

    def test_implausible_shift_length_excluded_and_surfaced_separately(self):
        # Real production data: shift_time='6pm-3pm' (almost certainly a
        # typo for '6pm-3am') parses via the overnight-wraparound formula to
        # a 21-hour "scheduled" shift -- MAX_PLAUSIBLE_SHIFT_HOURS (16) must
        # keep this out of the ranked undertime table (it isn't a real
        # shortfall, it's bad source data) while still surfacing it
        # separately so the schedule can actually get fixed, rather than
        # just silently vanishing.
        d = date(2026, 9, 15)
        scheduled = [{
            'employee_id': 'EMP-TYPO', 'schedule_name': 'Typo Shift Employee', 'tl': 'TL1',
            'group_name': 'Group A', 'shift_time': '6pm-3pm', 'absent_date': d, 'personid': 350,
        }]
        raw_logs = [
            {'personid': 350, 'date': datetime(2026, 9, 15, 18, 0, 0), 'type': 'in'},
            {'personid': 350, 'date': datetime(2026, 9, 16, 3, 0, 0), 'type': 'out'},
        ]
        table_rows = {'employee_schedules': scheduled, 'dailytimerecordsfiltered': raw_logs,
                       'leave4day_requests': []}
        resp, _, captured = self._get_with_capture(d, d, table_rows)
        self.assertEqual(resp.status_code, 200)
        ctx = captured['admin/undertime_report.html']
        self.assertEqual(ctx['records'], [], 'implausible shift must not appear in the ranked table')
        implausible = ctx['implausible_shifts']
        self.assertEqual(len(implausible), 1)
        self.assertEqual(implausible[0]['employee_id'], 'EMP-TYPO')
        self.assertAlmostEqual(implausible[0]['scheduled_hours'], 21.0, places=4)
        html = resp.data.decode()
        self.assertIn('Typo Shift Employee', html)
        self.assertIn('implausible', html)

    def test_minute_rounding_never_overflows_to_60(self):
        # hours_worked/shortfall_hours near a whole-minute boundary must not
        # render as "Xh 60m" -- round total minutes first, then split into
        # h/m, rather than truncating hours and rounding the leftover
        # fraction independently.
        d = date(2026, 9, 1)
        scheduled = [{
            'employee_id': 'EMP-ROUND', 'schedule_name': 'Rounding Employee', 'tl': 'TL1',
            'group_name': 'Group A', 'shift_time': '9am-6pm', 'absent_date': d, 'personid': 351,
        }]
        # Worked 7.9999h of a 9h shift -- old (buggy) split: hw_h=7,
        # hw_m=round((7.9999-7)*60)=round(59.994)=60 -> "7h 60m".
        raw_logs = [
            {'personid': 351, 'date': datetime(2026, 9, 1, 9, 0, 0), 'type': 'in'},
            {'personid': 351, 'date': datetime(2026, 9, 1, 16, 59, 59, 640000), 'type': 'out'},
        ]
        table_rows = {'employee_schedules': scheduled, 'dailytimerecordsfiltered': raw_logs,
                       'leave4day_requests': []}
        resp, _ = self._get(d, d, table_rows)
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode()
        self.assertNotIn('60m', html, 'minutes must never round up to 60 in the h/m display')
        self.assertIn('8h 00m', html, 'total minutes should round up into the hours place instead')

    def test_unparseable_shift_string_skipped_no_crash(self):
        # parse_shift_time() returns (None, None) for a string with no
        # am/pm/mn/nn suffix -- the row must be silently skipped, not crash.
        d = date(2026, 9, 1)
        scheduled = [{
            'employee_id': 'EMP-BADSHIFT', 'schedule_name': 'Bad Shift Employee', 'tl': 'TL1',
            'group_name': 'Group A', 'shift_time': 'garbage-shift', 'absent_date': d, 'personid': 308,
        }]
        raw_logs = [
            {'personid': 308, 'date': datetime(2026, 9, 1, 8, 0, 0), 'type': 'in'},
            {'personid': 308, 'date': datetime(2026, 9, 1, 16, 0, 0), 'type': 'out'},
        ]
        table_rows = {'employee_schedules': scheduled, 'dailytimerecordsfiltered': raw_logs,
                       'leave4day_requests': []}
        resp, _ = self._get(d, d, table_rows)
        self.assertEqual(resp.status_code, 200, 'unparseable shift_time must not crash the route')
        html = resp.data.decode()
        self.assertNotIn('Bad Shift Employee', html)
        self.assertIn('No undertime occurrences', html)

    def test_leave_covered_short_shift_is_excluded(self):
        # Worked short (would otherwise qualify) but has an Approved leave
        # filed covering that date -- must be excluded entirely (e.g.
        # approved half-day leave), per the deliberate design decision.
        d = date(2026, 9, 1)
        scheduled = [{
            'employee_id': 'EMP-LEAVE', 'schedule_name': 'Leave Covered Employee', 'tl': 'TL1',
            'group_name': 'Group A', 'shift_time': '7am-4pm', 'absent_date': d, 'personid': 309,
        }]
        raw_logs = [
            {'personid': 309, 'date': datetime(2026, 9, 1, 7, 0, 0), 'type': 'in'},
            {'personid': 309, 'date': datetime(2026, 9, 1, 11, 0, 0), 'type': 'out'},
        ]
        leave = [{'employee_id': 'EMP-LEAVE', 'leave_type_name': 'Vacation Leave',
                  'status': 'Approved', 'leave_date': d}]
        table_rows = {'employee_schedules': scheduled, 'dailytimerecordsfiltered': raw_logs,
                       'leave4day_requests': leave}
        resp, _ = self._get(d, d, table_rows)
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode()
        self.assertNotIn('Leave Covered Employee', html)
        self.assertIn('No undertime occurrences', html)

    def test_rejected_leave_does_not_clear_undertime(self):
        # Mirrors the AWOL rejected-leave test: a Rejected leave record does
        # not count as coverage, so the employee is still flagged.
        d = date(2026, 9, 1)
        scheduled = [{
            'employee_id': 'EMP-REJ', 'schedule_name': 'Rejected Leave Employee', 'tl': 'TL1',
            'group_name': 'Group A', 'shift_time': '7am-4pm', 'absent_date': d, 'personid': 310,
        }]
        raw_logs = [
            {'personid': 310, 'date': datetime(2026, 9, 1, 7, 0, 0), 'type': 'in'},
            {'personid': 310, 'date': datetime(2026, 9, 1, 11, 0, 0), 'type': 'out'},
        ]
        leave = [{'employee_id': 'EMP-REJ', 'leave_type_name': 'Vacation Leave',
                  'status': 'Rejected', 'leave_date': d}]
        table_rows = {'employee_schedules': scheduled, 'dailytimerecordsfiltered': raw_logs,
                       'leave4day_requests': leave}
        resp, _ = self._get(d, d, table_rows)
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode()
        self.assertIn('Rejected Leave Employee', html)
        self.assertIn('Undertime: <strong>1</strong>', html)

    def test_records_sorted_descending_by_shortfall(self):
        # Single group so the flat sort order is unambiguous in the HTML
        # row order; multi-group interaction is covered (and a bug flagged)
        # separately below.
        d = date(2026, 9, 1)
        scheduled = []
        raw_logs = []
        shifts = [
            ('EMP-SORT-SMALL', 'Small Shortfall', 311, 15, 30),   # 0.5h short
            ('EMP-SORT-BIG', 'Big Shortfall', 312, 10, 0),        # 6.0h short
            ('EMP-SORT-MED', 'Medium Shortfall', 313, 13, 0),     # 3.0h short
        ]
        for emp_id, name, pid, out_h, out_m in shifts:
            scheduled.append({
                'employee_id': emp_id, 'schedule_name': name, 'tl': 'TL1',
                'group_name': 'Group A', 'shift_time': '7am-4pm', 'absent_date': d, 'personid': pid,
            })
            raw_logs.append({'personid': pid, 'date': datetime(2026, 9, 1, 7, 0, 0), 'type': 'in'})
            raw_logs.append({'personid': pid, 'date': datetime(2026, 9, 1, out_h, out_m, 0), 'type': 'out'})
        table_rows = {'employee_schedules': scheduled, 'dailytimerecordsfiltered': raw_logs,
                       'leave4day_requests': []}
        resp, _, captured = self._get_with_capture(d, d, table_rows)
        self.assertEqual(resp.status_code, 200)
        records = captured['admin/undertime_report.html']['records']
        self.assertEqual(len(records), 3)
        shortfalls = [r['shortfall_hours'] for r in records]
        self.assertEqual(shortfalls, sorted(shortfalls, reverse=True),
                          'records must be sorted descending by shortfall_hours')
        self.assertEqual(records[0]['employee_id'], 'EMP-SORT-BIG')
        self.assertEqual(records[1]['employee_id'], 'EMP-SORT-MED')
        self.assertEqual(records[2]['employee_id'], 'EMP-SORT-SMALL')

    def test_shortfall_sort_does_not_fragment_the_table(self):
        # Fixed: this report is a single flat table ranked by shortfall,
        # NOT grouped into one <div class="card"> per group_name like the
        # sibling reports -- a global severity sort is fundamentally
        # incompatible with per-group card splitting (see git history for
        # the bug this replaced: the old grouped-card template rendered the
        # same group_name as multiple separate cards once the shortfall
        # sort interleaved rows from different groups). Department is shown
        # as its own column instead.
        #
        # Fixture: Group A has two employees (shortfalls 3.0 and 1.0),
        # Group B has one employee (shortfall 2.0), in SQL order A1, A2, B1
        # (ORDER BY group_name, tl, schedule_date, schedule_name puts A
        # before B). After the descending shortfall sort the row order must
        # be A1(3.0), B1(2.0), A2(1.0) -- interleaved groups are fine now,
        # since there's no per-group card to fragment.
        d = date(2026, 9, 1)
        scheduled = [
            {'employee_id': 'EMP-A1', 'schedule_name': 'Group A Big', 'tl': 'TL1',
             'group_name': 'Group A', 'shift_time': '7am-4pm', 'absent_date': d, 'personid': 320},
            {'employee_id': 'EMP-A2', 'schedule_name': 'Group A Small', 'tl': 'TL1',
             'group_name': 'Group A', 'shift_time': '7am-4pm', 'absent_date': d, 'personid': 321},
            {'employee_id': 'EMP-B1', 'schedule_name': 'Group B Mid', 'tl': 'TL2',
             'group_name': 'Group B', 'shift_time': '7am-4pm', 'absent_date': d, 'personid': 322},
        ]
        raw_logs = [
            # A1: worked 6h of 9h -> shortfall 3.0
            {'personid': 320, 'date': datetime(2026, 9, 1, 7, 0, 0), 'type': 'in'},
            {'personid': 320, 'date': datetime(2026, 9, 1, 13, 0, 0), 'type': 'out'},
            # A2: worked 8h of 9h -> shortfall 1.0
            {'personid': 321, 'date': datetime(2026, 9, 1, 7, 0, 0), 'type': 'in'},
            {'personid': 321, 'date': datetime(2026, 9, 1, 15, 0, 0), 'type': 'out'},
            # B1: worked 7h of 9h -> shortfall 2.0
            {'personid': 322, 'date': datetime(2026, 9, 1, 7, 0, 0), 'type': 'in'},
            {'personid': 322, 'date': datetime(2026, 9, 1, 14, 0, 0), 'type': 'out'},
        ]
        table_rows = {'employee_schedules': scheduled, 'dailytimerecordsfiltered': raw_logs,
                       'leave4day_requests': []}
        resp, _, captured = self._get_with_capture(d, d, table_rows)
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode()

        # Exactly one table -- no per-group card splitting.
        self.assertEqual(html.count('class="table table-hover mb-0 undertime-table"'), 1)

        records = captured['admin/undertime_report.html']['records']
        self.assertEqual([r['employee_id'] for r in records], ['EMP-A1', 'EMP-B1', 'EMP-A2'],
                          'rows must be globally sorted by shortfall descending, interleaving groups freely')

        # Group is now a plain column value per row, visible in the HTML.
        self.assertIn('Group A', html)
        self.assertIn('Group B', html)


# ──────────────────────────────────────────────────────────────────
# Boundary conditions
# ──────────────────────────────────────────────────────────────────
class TestUndertimeBoundaries(unittest.TestCase):

    def setUp(self):
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            admin_session(sess, is_admin=True)

    def test_empty_scheduled_renders_empty_state_no_crash(self):
        d = date(2026, 9, 1)
        cursor_log = []
        table_rows = {'employee_schedules': [], 'dailytimerecordsfiltered': [], 'leave4day_requests': []}
        with patch.object(appmod, 'get_central_db', make_db_factory(table_rows, cursor_log)), \
             patch.object(appmod, 'get_db', make_db_factory(table_rows, cursor_log)):
            resp = self.client.get('/admin/undertime-report',
                                    query_string={'date_from': str(d), 'date_to': str(d)})
        self.assertEqual(resp.status_code, 200)
        self.assertIn('No undertime occurrences', resp.data.decode())
        # scheduled empty -> route never enters the `if scheduled:` block,
        # so exactly one get_central_db() call (the scheduled query) and
        # zero get_db() calls should have happened.
        self.assertEqual(len(cursor_log), 1)

    def test_malformed_date_param_falls_back_safely(self):
        cursor_log = []
        table_rows = {'employee_schedules': [], 'dailytimerecordsfiltered': [], 'leave4day_requests': []}
        with patch.object(appmod, 'get_central_db', make_db_factory(table_rows, cursor_log)), \
             patch.object(appmod, 'get_db', make_db_factory(table_rows, cursor_log)):
            resp = self.client.get('/admin/undertime-report',
                                    query_string={'date_from': "2026-09-01'; DROP TABLE x--",
                                                   'date_to': 'not-a-date'})
        self.assertEqual(resp.status_code, 200)

    def test_special_characters_in_names_render_safely(self):
        d = date(2026, 9, 1)
        scheduled = [{
            'employee_id': "EMP-<script>", 'schedule_name': 'O\'Brien <script>alert(1)</script> Ünïcödé',
            'tl': 'TL "Quoted"', 'group_name': "Group % Wild'card",
            'shift_time': '7am-4pm', 'absent_date': d, 'personid': 330,
        }]
        raw_logs = [
            {'personid': 330, 'date': datetime(2026, 9, 1, 7, 0, 0), 'type': 'in'},
            {'personid': 330, 'date': datetime(2026, 9, 1, 11, 0, 0), 'type': 'out'},
        ]
        table_rows = {'employee_schedules': scheduled, 'dailytimerecordsfiltered': raw_logs,
                       'leave4day_requests': []}
        cursor_log = []
        with patch.object(appmod, 'get_central_db', make_db_factory(table_rows, cursor_log)), \
             patch.object(appmod, 'get_db', make_db_factory(table_rows, cursor_log)):
            resp = self.client.get('/admin/undertime-report',
                                    query_string={'date_from': str(d), 'date_to': str(d)})
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode()
        self.assertNotIn('<script>alert(1)</script>', html)
        self.assertIn('Ünïcödé', html)


# ──────────────────────────────────────────────────────────────────
# SQL parameter safety
# ──────────────────────────────────────────────────────────────────
class TestUndertimeSqlSafety(unittest.TestCase):

    def setUp(self):
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            admin_session(sess, is_admin=True)

    def test_queries_use_placeholders_not_string_interpolated_dates(self):
        d_from, d_to = date(2026, 9, 1), date(2026, 9, 3)
        scheduled = [
            {'employee_id': 'EMP-SQL1', 'schedule_name': 'SQL Safety Employee', 'tl': 'TL1',
             'group_name': 'Group A', 'shift_time': '7am-4pm', 'absent_date': d_from, 'personid': 340},
        ]
        table_rows = {'employee_schedules': scheduled, 'dailytimerecordsfiltered': [],
                       'leave4day_requests': []}
        cursor_log = []
        with patch.object(appmod, 'get_central_db', make_db_factory(table_rows, cursor_log)), \
             patch.object(appmod, 'get_db', make_db_factory(table_rows, cursor_log)):
            resp = self.client.get('/admin/undertime-report',
                                    query_string={'date_from': str(d_from), 'date_to': str(d_to)})
        self.assertEqual(resp.status_code, 200)
        self.assertGreaterEqual(len(cursor_log), 1)

        saw_scheduled_query = False
        for cur in cursor_log:
            query, params = cur.last_query, cur.last_params
            if query is None:
                continue
            self.assertNotIn(str(d_from), query,
                              'date value must not be string-interpolated into the SQL text')
            self.assertNotIn(str(d_to), query,
                              'date value must not be string-interpolated into the SQL text')
            if 'employee_schedules' in query.lower():
                saw_scheduled_query = True
                self.assertIn('%s', query)
                self.assertIsNotNone(params)
                self.assertIn(d_from, tuple(params))
                self.assertIn(d_to, tuple(params))
        self.assertTrue(saw_scheduled_query, 'expected the Step 1 scheduled-employees query to run')

    def test_source_query_text_uses_bound_placeholders(self):
        import inspect
        src = inspect.getsource(appmod.admin_undertime_report)
        self.assertIn('es.schedule_date BETWEEN %s AND %s', src)
        self.assertIn('lr.leave_date BETWEEN %s AND %s', src)
        self.assertNotRegex(src, r'BETWEEN\s*\{date_from\}\s*AND\s*\{date_to\}')
        self.assertIn("LOWER(lr.status) NOT IN ('rejected','cancelled','deleted')", src)
        # IN(...) clauses must only ever f-string-interpolate the
        # placeholder COUNT, never a value -- confirmed by the absence of
        # any f-string embedding emp_ids/date variables directly.
        self.assertNotRegex(src, r'IN\s*\(\s*\{emp_ids\}\s*\)')


# ──────────────────────────────────────────────────────────────────
# Jinja syntax sanity for the new template + the three edited tab-bars
# ──────────────────────────────────────────────────────────────────
class TestTemplateSyntax(unittest.TestCase):
    def test_undertime_report_template_compiles(self):
        appmod.app.jinja_env.get_template('admin/undertime_report.html')

    def test_awol_report_template_compiles(self):
        appmod.app.jinja_env.get_template('admin/awol_report.html')

    def test_absences_template_compiles(self):
        appmod.app.jinja_env.get_template('admin/absences.html')

    def test_attendance_grid_template_compiles(self):
        appmod.app.jinja_env.get_template('admin/attendance_grid.html')


if __name__ == '__main__':
    unittest.main(verbosity=2)
