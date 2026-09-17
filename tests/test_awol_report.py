"""
Mocked/offline tests for the new /admin/awol-report route (admin_awol_report()
in app.py, ~line 10769) and its template templates/admin/awol_report.html.

Stage 2 (tester) of the house ship workflow. Run with:
    ./venv/bin/python -m unittest /tmp/.../test_awol_report.py -v
(copied into repo root as test_awol_report.py so `app` imports cleanly with
its relative .env path, then run from there -- see runner notes at bottom.)

Design notes
------------
* No real DB connections are made. `app.get_central_db` and `app.get_db`
  are monkeypatched with fakes that route fetchall() results by which
  table name appears in the executed query text, so call-order in the
  route doesn't need to be hardcoded here (robust to minor refactors).
* Every fake cursor logs the exact (query, params) passed to .execute(),
  which is reused for the SQL-injection-safety assertions.
* Most correctness tests exercise the REAL template (no render_template
  mocking) via app.test_client(), so a Jinja bug (e.g. calling
  .strftime() on a None/non-date value) would actually be caught here,
  not hidden behind a mock.
* Fixture rows are plain dicts matching the real column names/types
  confirmed against the live schema (see report) -- e.g.
  leave4day_requests has ONE ROW PER CALENDAR DAY (leave_date is a
  scalar `date` column, not a range), which the multi-day test below
  relies on.
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
# Fakes: route get_central_db()/get_db() fetchall() results by table
# name found in the executed SQL text. Each fake connection creates a
# fresh cursor (matching real code closing/reopening a connection per
# call) but all cursors created from one factory share the same
# `cursor_log` list so tests can inspect every execute() call after
# the request completes.
# ──────────────────────────────────────────────────────────────────
# This is the EXACT status-exclusion list from the route's own SQL text
# (app.py, admin_awol_report Step 5: "status NOT IN (...)"). Since that
# filter runs server-side in MySQL, a Python-level mock of get_db() can
# never observe it happening -- the mock has to apply the same predicate
# itself to faithfully reproduce what the real query would hand back.
# This is why the SQL-safety test separately asserts the literal WHERE
# clause text still contains this exact (case-insensitive) predicate, and
# why the live sanity check (script 2) additionally verifies this against
# the real server.
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
        # Prefer the longest/most-specific table name match (e.g. avoid
        # 'ot_requests' matching inside some other longer table name).
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
        # Other code paths reached incidentally via shared templates (e.g.
        # break_log's nav-permission helper, which also calls
        # get_central_db()) use fetchone(); harmless no-match -> None here.
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
    """Returns a zero-arg callable usable as get_central_db/get_db side_effect."""
    def factory():
        return RoutingConn(table_rows, cursor_log)
    return factory


def admin_session(sess, is_admin=True, can_absences=False):
    sess['user'] = {'name': 'Test Admin', 'employee_id': 'TESTADMIN', 'email': 'testadmin@example.com'}
    sess['is_admin'] = is_admin
    sess['permissions'] = {'can_absences': can_absences}


# ──────────────────────────────────────────────────────────────────
# Access control
# ──────────────────────────────────────────────────────────────────
class TestAwolAccessControl(unittest.TestCase):

    def setUp(self):
        self.client = app.test_client()

    def test_no_session_redirects_and_never_touches_db(self):
        central_calls = MagicMock(side_effect=AssertionError('get_central_db should not be called'))
        db_calls = MagicMock(side_effect=AssertionError('get_db should not be called'))
        with patch.object(appmod, 'get_central_db', central_calls), \
             patch.object(appmod, 'get_db', db_calls):
            resp = self.client.get('/admin/awol-report')
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
            resp = self.client.get('/admin/awol-report')
        self.assertIn(resp.status_code, (301, 302, 303, 307, 308))
        central_calls.assert_not_called()
        db_calls.assert_not_called()

    def test_is_admin_true_passes_gate(self):
        cursor_log = []
        table_rows = {'employee_schedules': [], 'dailytimerecordsfiltered': [],
                       'ot_requests': [], 'leave4day_requests': []}
        with patch.object(appmod, 'get_central_db', make_db_factory(table_rows, cursor_log)), \
             patch.object(appmod, 'get_db', make_db_factory(table_rows, cursor_log)):
            with self.client.session_transaction() as sess:
                admin_session(sess, is_admin=True, can_absences=False)
            resp = self.client.get('/admin/awol-report')
        self.assertEqual(resp.status_code, 200, 'is_admin=True must pass the gate and render')
        self.assertTrue(len(cursor_log) >= 1, 'gate passing should have reached the DB layer')

    def test_can_absences_true_passes_gate_even_without_is_admin(self):
        cursor_log = []
        table_rows = {'employee_schedules': [], 'dailytimerecordsfiltered': [],
                       'ot_requests': [], 'leave4day_requests': []}
        with patch.object(appmod, 'get_central_db', make_db_factory(table_rows, cursor_log)), \
             patch.object(appmod, 'get_db', make_db_factory(table_rows, cursor_log)):
            with self.client.session_transaction() as sess:
                admin_session(sess, is_admin=False, can_absences=True)
            resp = self.client.get('/admin/awol-report')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(len(cursor_log) >= 1)


# ──────────────────────────────────────────────────────────────────
# Filtering correctness (the core AWOL logic)
# ──────────────────────────────────────────────────────────────────
class TestAwolFiltering(unittest.TestCase):

    def setUp(self):
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            admin_session(sess, is_admin=True)

    def _get(self, date_from, date_to, table_rows):
        cursor_log = []
        with patch.object(appmod, 'get_central_db', make_db_factory(table_rows, cursor_log)), \
             patch.object(appmod, 'get_db', make_db_factory(table_rows, cursor_log)):
            resp = self.client.get('/admin/awol-report',
                                    query_string={'date_from': str(date_from), 'date_to': str(date_to)})
        return resp, cursor_log

    def test_no_attendance_record_at_all_is_awol(self):
        d = date(2026, 9, 1)
        scheduled = [{
            'employee_id': 'EMP-NOSHOW', 'schedule_name': 'No Show Employee', 'tl': 'TL1',
            'group_name': 'Group A', 'shift_time': '7am-4pm', 'absent_date': d, 'personid': 201,
        }]
        table_rows = {'employee_schedules': scheduled, 'dailytimerecordsfiltered': [],
                       'ot_requests': [], 'leave4day_requests': []}
        resp, _ = self._get(d, d, table_rows)
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode()
        self.assertIn('No Show Employee', html)
        self.assertIn('AWOL: <strong>1</strong>', html)

    def test_leave_covered_absence_is_not_awol(self):
        d = date(2026, 9, 1)
        scheduled = [{
            'employee_id': 'EMP-LEAVE', 'schedule_name': 'Leave Covered Employee', 'tl': 'TL1',
            'group_name': 'Group A', 'shift_time': '7am-4pm', 'absent_date': d, 'personid': 202,
        }]
        leave = [{
            'employee_id': 'EMP-LEAVE', 'leave_type_name': 'Sick Leave',
            'status': 'Approved', 'leave_date': d,
        }]
        table_rows = {'employee_schedules': scheduled, 'dailytimerecordsfiltered': [],
                       'ot_requests': [], 'leave4day_requests': leave}
        resp, _ = self._get(d, d, table_rows)
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode()
        self.assertNotIn('Leave Covered Employee', html)
        self.assertIn('No AWOL occurrences', html)

    def test_rejected_leave_does_not_clear_awol(self):
        d = date(2026, 9, 1)
        scheduled = [{
            'employee_id': 'EMP-REJ', 'schedule_name': 'Rejected Leave Employee', 'tl': 'TL1',
            'group_name': 'Group A', 'shift_time': '7am-4pm', 'absent_date': d, 'personid': 203,
        }]
        leave = [{
            'employee_id': 'EMP-REJ', 'leave_type_name': 'Vacation Leave',
            'status': 'Rejected', 'leave_date': d,
        }]
        table_rows = {'employee_schedules': scheduled, 'dailytimerecordsfiltered': [],
                       'ot_requests': [], 'leave4day_requests': leave}
        resp, _ = self._get(d, d, table_rows)
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode()
        self.assertIn('Rejected Leave Employee', html)
        self.assertIn('AWOL: <strong>1</strong>', html)

    def test_fts_in_status_excluded_from_awol(self):
        # Lone 'out' punch after noon, no prior 'in' -> build_attendance_map
        # assigns FTS IN to that same calendar date (noon heuristic).
        d = date(2026, 9, 1)
        scheduled = [{
            'employee_id': 'EMP-FTSIN', 'schedule_name': 'FTS IN Employee', 'tl': 'TL1',
            'group_name': 'Group A', 'shift_time': '7am-4pm', 'absent_date': d, 'personid': 204,
        }]
        raw_logs = [{'personid': 204, 'date': datetime(2026, 9, 1, 14, 0, 0), 'type': 'out'}]
        table_rows = {'employee_schedules': scheduled, 'dailytimerecordsfiltered': raw_logs,
                       'ot_requests': [], 'leave4day_requests': []}
        resp, _ = self._get(d, d, table_rows)
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode()
        self.assertNotIn('FTS IN Employee', html)
        self.assertIn('No AWOL occurrences', html)

    def test_fts_out_status_excluded_from_awol(self):
        # Lone 'in' punch late evening with nothing to pair it with in the
        # window -> trailing dangling-IN branch assigns FTS OUT.
        d = date(2026, 9, 1)
        scheduled = [{
            'employee_id': 'EMP-FTSOUT', 'schedule_name': 'FTS OUT Employee', 'tl': 'TL1',
            'group_name': 'Group A', 'shift_time': '10pm-7am', 'absent_date': d, 'personid': 205,
        }]
        raw_logs = [{'personid': 205, 'date': datetime(2026, 9, 1, 22, 0, 0), 'type': 'in'}]
        table_rows = {'employee_schedules': scheduled, 'dailytimerecordsfiltered': raw_logs,
                       'ot_requests': [], 'leave4day_requests': []}
        resp, _ = self._get(d, d, table_rows)
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode()
        self.assertNotIn('FTS OUT Employee', html)
        self.assertIn('No AWOL occurrences', html)

    def test_approved_ot_within_window_clears_awol(self):
        d = date(2026, 9, 1)
        scheduled = [{
            'employee_id': 'EMP-OT', 'schedule_name': 'OT Covered Employee', 'tl': 'TL1',
            'group_name': 'Group A', 'shift_time': '7am-4pm', 'absent_date': d, 'personid': 206,
        }]
        ot_rows = [{'employee_id': 'EMP-OT', 'ot_date': d}]
        table_rows = {'employee_schedules': scheduled, 'dailytimerecordsfiltered': [],
                       'ot_requests': ot_rows, 'leave4day_requests': []}
        resp, _ = self._get(d, d, table_rows)
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode()
        self.assertNotIn('OT Covered Employee', html)
        self.assertIn('No AWOL occurrences', html)

    def test_ot_one_day_offset_still_clears_awol(self):
        # OT filed for the day AFTER the absence -- still within +/-1 day.
        d = date(2026, 9, 1)
        scheduled = [{
            'employee_id': 'EMP-OT2', 'schedule_name': 'OT Offset Employee', 'tl': 'TL1',
            'group_name': 'Group A', 'shift_time': '7am-4pm', 'absent_date': d, 'personid': 207,
        }]
        ot_rows = [{'employee_id': 'EMP-OT2', 'ot_date': d + timedelta(days=1)}]
        table_rows = {'employee_schedules': scheduled, 'dailytimerecordsfiltered': [],
                       'ot_requests': ot_rows, 'leave4day_requests': []}
        resp, _ = self._get(d, d, table_rows)
        html = resp.data.decode()
        self.assertNotIn('OT Offset Employee', html)

    def test_multiday_leave_clears_all_three_days(self):
        # leave4day_requests has ONE ROW PER CALENDAR DAY -- confirm a
        # 3-day absence stretch with 3 matching per-day leave rows clears
        # all three, not just the first/last.
        d1, d2, d3 = date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3)
        scheduled = [
            {'employee_id': 'EMP-MULTI', 'schedule_name': 'Multi Day Employee', 'tl': 'TL1',
             'group_name': 'Group A', 'shift_time': '7am-4pm', 'absent_date': d, 'personid': 208}
            for d in (d1, d2, d3)
        ]
        leave = [
            {'employee_id': 'EMP-MULTI', 'leave_type_name': 'Vacation Leave', 'status': 'Approved', 'leave_date': d}
            for d in (d1, d2, d3)
        ]
        table_rows = {'employee_schedules': scheduled, 'dailytimerecordsfiltered': [],
                       'ot_requests': [], 'leave4day_requests': leave}
        resp, _ = self._get(d1, d3, table_rows)
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode()
        self.assertNotIn('Multi Day Employee', html)
        self.assertIn('No AWOL occurrences', html)

    def test_multiday_leave_missing_middle_day_still_flags_that_day(self):
        # Sibling of the above: only day 1 and day 3 have leave rows, day 2
        # doesn't -- day 2 alone should surface as AWOL.
        d1, d2, d3 = date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3)
        scheduled = [
            {'employee_id': 'EMP-GAP', 'schedule_name': 'Gap Day Employee', 'tl': 'TL1',
             'group_name': 'Group A', 'shift_time': '7am-4pm', 'absent_date': d, 'personid': 209}
            for d in (d1, d2, d3)
        ]
        leave = [
            {'employee_id': 'EMP-GAP', 'leave_type_name': 'Vacation Leave', 'status': 'Approved', 'leave_date': d}
            for d in (d1, d3)
        ]
        table_rows = {'employee_schedules': scheduled, 'dailytimerecordsfiltered': [],
                       'ot_requests': [], 'leave4day_requests': leave}
        resp, _ = self._get(d1, d3, table_rows)
        html = resp.data.decode()
        self.assertIn('Gap Day Employee', html)
        self.assertIn('AWOL: <strong>1</strong>', html)


# ──────────────────────────────────────────────────────────────────
# Boundary conditions
# ──────────────────────────────────────────────────────────────────
class TestAwolBoundaries(unittest.TestCase):

    def setUp(self):
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            admin_session(sess, is_admin=True)

    def test_empty_scheduled_renders_empty_state_no_crash(self):
        d = date(2026, 9, 1)
        cursor_log = []
        table_rows = {'employee_schedules': [], 'dailytimerecordsfiltered': [],
                       'ot_requests': [], 'leave4day_requests': []}
        with patch.object(appmod, 'get_central_db', make_db_factory(table_rows, cursor_log)) as gcdb, \
             patch.object(appmod, 'get_db', make_db_factory(table_rows, cursor_log)) as gdb:
            resp = self.client.get('/admin/awol-report',
                                    query_string={'date_from': str(d), 'date_to': str(d)})
        self.assertEqual(resp.status_code, 200)
        self.assertIn('No AWOL occurrences', resp.data.decode())
        # scheduled empty -> route never enters the `if scheduled:` block,
        # so exactly one get_central_db() call (the scheduled query) and
        # zero get_db() calls should have happened.
        self.assertEqual(len(cursor_log), 1)

    def test_null_personid_does_not_crash_and_surfaces_as_unverified(self):
        # The join allows personid to come back NULL when userdata has no
        # matching companyid. Confirm this doesn't crash the attendance-map
        # lookup, and -- per the reviewer's blocking fix -- that it no
        # longer defaults straight to an "AWOL" accusation: it must show up
        # in the separate Unverified section instead, and NOT count toward
        # the AWOL total.
        d = date(2026, 9, 1)
        scheduled = [{
            'employee_id': 'EMP-NULLPID', 'schedule_name': 'Null Personid Employee', 'tl': 'TL1',
            'group_name': 'Group A', 'shift_time': '9am-6pm', 'absent_date': d, 'personid': None,
        }]
        table_rows = {'employee_schedules': scheduled, 'dailytimerecordsfiltered': [],
                       'ot_requests': [], 'leave4day_requests': []}
        cursor_log = []
        with patch.object(appmod, 'get_central_db', make_db_factory(table_rows, cursor_log)), \
             patch.object(appmod, 'get_db', make_db_factory(table_rows, cursor_log)):
            resp = self.client.get('/admin/awol-report',
                                    query_string={'date_from': str(d), 'date_to': str(d)})
        self.assertEqual(resp.status_code, 200, 'NULL personid must not crash the route')
        html = resp.data.decode()
        self.assertIn('Null Personid Employee', html)
        self.assertIn('could not be verified', html)
        # The AWOL summary pill only renders when there's at least one AWOL
        # record; with zero AWOL records (only an Unverified one), the empty
        # state shows instead and no "AWOL: <strong>N</strong>" pill exists
        # at all -- so the real assertion is that this employee is NOT
        # counted as AWOL, not that a "0" pill appears.
        self.assertNotIn('AWOL: <strong>1</strong>', html)
        self.assertIn('No AWOL occurrences found', html)

    def test_null_personid_covered_by_leave_is_fully_cleared(self):
        # Leave coverage is keyed by employee_id/date, not personid, so a
        # filed leave should fully explain a NULL-personid day too -- it
        # should not appear as AWOL *or* as Unverified noise, since the
        # leave record already explains the day.
        d = date(2026, 9, 1)
        scheduled = [{
            'employee_id': 'EMP-NULLPID2', 'schedule_name': 'Null Personid Covered', 'tl': 'TL1',
            'group_name': 'Group A', 'shift_time': '9am-6pm', 'absent_date': d, 'personid': None,
        }]
        leave = [{'employee_id': 'EMP-NULLPID2', 'leave_type_name': 'Sick Leave',
                  'status': 'Approved', 'leave_date': d}]
        table_rows = {'employee_schedules': scheduled, 'dailytimerecordsfiltered': [],
                       'ot_requests': [], 'leave4day_requests': leave}
        cursor_log = []
        with patch.object(appmod, 'get_central_db', make_db_factory(table_rows, cursor_log)), \
             patch.object(appmod, 'get_db', make_db_factory(table_rows, cursor_log)):
            resp = self.client.get('/admin/awol-report',
                                    query_string={'date_from': str(d), 'date_to': str(d)})
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode()
        self.assertNotIn('Null Personid Covered', html)
        self.assertNotIn('could not be verified', html)
        self.assertIn('No AWOL occurrences found', html)

    def test_malformed_date_param_falls_back_safely(self):
        # parse_date() wraps date.fromisoformat in try/except ValueError.
        # A garbage / injection-shaped date_from should not crash the route.
        cursor_log = []
        table_rows = {'employee_schedules': [], 'dailytimerecordsfiltered': [],
                       'ot_requests': [], 'leave4day_requests': []}
        with patch.object(appmod, 'get_central_db', make_db_factory(table_rows, cursor_log)), \
             patch.object(appmod, 'get_db', make_db_factory(table_rows, cursor_log)):
            resp = self.client.get('/admin/awol-report',
                                    query_string={'date_from': "2026-09-01'; DROP TABLE x--",
                                                   'date_to': 'not-a-date'})
        self.assertEqual(resp.status_code, 200)

    def test_special_characters_in_names_render_safely(self):
        d = date(2026, 9, 1)
        scheduled = [{
            'employee_id': "EMP-<script>", 'schedule_name': 'O\'Brien <script>alert(1)</script> Ünïcödé',
            'tl': 'TL "Quoted"', 'group_name': "Group % Wild'card",
            'shift_time': '7am-4pm', 'absent_date': d, 'personid': 210,
        }]
        table_rows = {'employee_schedules': scheduled, 'dailytimerecordsfiltered': [],
                       'ot_requests': [], 'leave4day_requests': []}
        cursor_log = []
        with patch.object(appmod, 'get_central_db', make_db_factory(table_rows, cursor_log)), \
             patch.object(appmod, 'get_db', make_db_factory(table_rows, cursor_log)):
            resp = self.client.get('/admin/awol-report',
                                    query_string={'date_from': str(d), 'date_to': str(d)})
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode()
        # Jinja autoescape must neutralize the raw <script> tag.
        self.assertNotIn('<script>alert(1)</script>', html)
        self.assertIn('Ünïcödé', html)


# ──────────────────────────────────────────────────────────────────
# SQL parameter safety
# ──────────────────────────────────────────────────────────────────
class TestAwolSqlSafety(unittest.TestCase):

    def setUp(self):
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            admin_session(sess, is_admin=True)

    def test_queries_use_placeholders_not_string_interpolated_dates(self):
        d_from, d_to = date(2026, 9, 1), date(2026, 9, 3)
        scheduled = [
            {'employee_id': 'EMP-SQL1', 'schedule_name': 'SQL Safety Employee', 'tl': 'TL1',
             'group_name': 'Group A', 'shift_time': '7am-4pm', 'absent_date': d_from, 'personid': 211},
        ]
        table_rows = {'employee_schedules': scheduled, 'dailytimerecordsfiltered': [],
                       'ot_requests': [], 'leave4day_requests': []}
        cursor_log = []
        with patch.object(appmod, 'get_central_db', make_db_factory(table_rows, cursor_log)), \
             patch.object(appmod, 'get_db', make_db_factory(table_rows, cursor_log)):
            resp = self.client.get('/admin/awol-report',
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
        # Static check directly on the route's source: BETWEEN %s AND %s
        # for date_from/date_to, never an f-string embedding the actual
        # variables (only the dynamic IN(...) placeholder COUNT is built
        # via f-string, which is safe since it only repeats the '%s'
        # token, never a value).
        import inspect
        src = inspect.getsource(appmod.admin_awol_report)
        self.assertIn('es.schedule_date BETWEEN %s AND %s', src)
        self.assertIn('ot_date BETWEEN %s AND %s', src)
        self.assertIn('lr.leave_date BETWEEN %s AND %s', src)
        self.assertNotRegex(src, r'BETWEEN\s*\{date_from\}\s*AND\s*\{date_to\}')
        self.assertIn("LOWER(lr.status) NOT IN ('rejected','cancelled','deleted')", src)


# ──────────────────────────────────────────────────────────────────
# Jinja syntax sanity for the two edited tab-bar templates + new one
# ──────────────────────────────────────────────────────────────────
class TestTemplateSyntax(unittest.TestCase):
    def test_awol_report_template_compiles(self):
        appmod.app.jinja_env.get_template('admin/awol_report.html')

    def test_absences_template_compiles(self):
        appmod.app.jinja_env.get_template('admin/absences.html')

    def test_attendance_grid_template_compiles(self):
        appmod.app.jinja_env.get_template('admin/attendance_grid.html')


# ──────────────────────────────────────────────────────────────────
# Step 3c: SUS (suspension) exclusion
#
# central_db.absence_records WHERE code = 'SUS' fully explains an
# otherwise-AWOL scheduled absence -- excluded from BOTH awol_records and
# the unverified (NULL-personid) list. Fixture rows for this table mimic
# what the mocked cursor hands back for:
#     SELECT employee_id, DATE_FORMAT(absence_date, '%%Y-%%m-%%d') AS d
#     FROM absence_records WHERE code = 'SUS' ...
# i.e. already-formatted {'employee_id': ..., 'd': 'YYYY-MM-DD'} rows,
# since RoutingCursor doesn't actually evaluate DATE_FORMAT() -- it just
# routes whatever rows are given in table_rows['absence_records'] back
# verbatim, same as every other fixture table in this file.
# ──────────────────────────────────────────────────────────────────
class TestAwolSusExclusion(unittest.TestCase):

    def setUp(self):
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            admin_session(sess, is_admin=True)

    def _get(self, date_from, date_to, table_rows):
        cursor_log = []
        with patch.object(appmod, 'get_central_db', make_db_factory(table_rows, cursor_log)), \
             patch.object(appmod, 'get_db', make_db_factory(table_rows, cursor_log)):
            resp = self.client.get('/admin/awol-report',
                                    query_string={'date_from': str(date_from), 'date_to': str(date_to)})
        return resp, cursor_log

    def test_sus_on_matching_date_excludes_from_awol_records(self):
        d = date(2026, 9, 1)
        scheduled = [{
            'employee_id': 'EMP-SUS', 'schedule_name': 'Suspended Employee', 'tl': 'TL1',
            'group_name': 'Group A', 'shift_time': '7am-4pm', 'absent_date': d, 'personid': 301,
        }]
        sus_rows = [{'employee_id': 'EMP-SUS', 'd': '2026-09-01'}]
        table_rows = {'employee_schedules': scheduled, 'dailytimerecordsfiltered': [],
                       'ot_requests': [], 'leave4day_requests': [], 'absence_records': sus_rows,
                       'incident_reports': []}
        resp, _ = self._get(d, d, table_rows)
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode()
        self.assertNotIn('Suspended Employee', html)
        self.assertIn('No AWOL occurrences', html)
        self.assertNotIn('could not be verified', html)

    def test_sus_on_different_date_does_not_exclude(self):
        d = date(2026, 9, 1)
        other_day = date(2026, 9, 2)
        scheduled = [{
            'employee_id': 'EMP-SUS2', 'schedule_name': 'Not Suspended Today', 'tl': 'TL1',
            'group_name': 'Group A', 'shift_time': '7am-4pm', 'absent_date': d, 'personid': 302,
        }]
        # SUS record exists for this employee but on a DIFFERENT date -- must
        # not clear the AWOL flag for `d`.
        sus_rows = [{'employee_id': 'EMP-SUS2', 'd': str(other_day)}]
        table_rows = {'employee_schedules': scheduled, 'dailytimerecordsfiltered': [],
                       'ot_requests': [], 'leave4day_requests': [], 'absence_records': sus_rows,
                       'incident_reports': []}
        resp, _ = self._get(d, d, table_rows)
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode()
        self.assertIn('Not Suspended Today', html)
        self.assertIn('AWOL: <strong>1</strong>', html)

    def test_sus_excludes_unverified_null_personid_path_too(self):
        # Same exclusion must apply on the NULL-personid (Unverified) branch:
        # a suspended employee with no personid mapping should appear in
        # NEITHER awol_records NOR unverified.
        d = date(2026, 9, 1)
        scheduled = [{
            'employee_id': 'EMP-SUS-NULLPID', 'schedule_name': 'Suspended Unverifiable', 'tl': 'TL1',
            'group_name': 'Group A', 'shift_time': '9am-6pm', 'absent_date': d, 'personid': None,
        }]
        sus_rows = [{'employee_id': 'EMP-SUS-NULLPID', 'd': '2026-09-01'}]
        table_rows = {'employee_schedules': scheduled, 'dailytimerecordsfiltered': [],
                       'ot_requests': [], 'leave4day_requests': [], 'absence_records': sus_rows,
                       'incident_reports': []}
        resp, _ = self._get(d, d, table_rows)
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode()
        self.assertNotIn('Suspended Unverifiable', html)
        self.assertNotIn('could not be verified', html)
        self.assertIn('No AWOL occurrences found', html)

    def test_sus_excluded_from_both_awol_and_unverified_mixed_batch(self):
        # Belt-and-suspenders: one AWOL-bound employee and one
        # unverified-bound employee, BOTH suspended on the exact date,
        # alongside a third (unrelated) employee who should still surface
        # as AWOL normally. Confirms the exclusion doesn't over-fire.
        d = date(2026, 9, 1)
        scheduled = [
            {'employee_id': 'EMP-SUS-A', 'schedule_name': 'Suspended AWOL Candidate', 'tl': 'TL1',
             'group_name': 'Group A', 'shift_time': '7am-4pm', 'absent_date': d, 'personid': 303},
            {'employee_id': 'EMP-SUS-B', 'schedule_name': 'Suspended Null Personid', 'tl': 'TL1',
             'group_name': 'Group A', 'shift_time': '7am-4pm', 'absent_date': d, 'personid': None},
            {'employee_id': 'EMP-SUS-C', 'schedule_name': 'Ordinary No Show', 'tl': 'TL1',
             'group_name': 'Group A', 'shift_time': '7am-4pm', 'absent_date': d, 'personid': 304},
        ]
        sus_rows = [
            {'employee_id': 'EMP-SUS-A', 'd': '2026-09-01'},
            {'employee_id': 'EMP-SUS-B', 'd': '2026-09-01'},
        ]
        table_rows = {'employee_schedules': scheduled, 'dailytimerecordsfiltered': [],
                       'ot_requests': [], 'leave4day_requests': [], 'absence_records': sus_rows,
                       'incident_reports': []}
        resp, _ = self._get(d, d, table_rows)
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode()
        self.assertNotIn('Suspended AWOL Candidate', html)
        self.assertNotIn('Suspended Null Personid', html)
        self.assertIn('Ordinary No Show', html)
        self.assertIn('AWOL: <strong>1</strong>', html)

    def test_sus_lookup_matches_attendance_grid_definition(self):
        # The user explicitly asked for this to reuse Attendance Grid's
        # existing SUS handling -- static cross-check that both routes
        # query the same table/column/code combination.
        import inspect
        awol_src = inspect.getsource(appmod.admin_awol_report)
        grid_src = inspect.getsource(appmod.admin_attendance_grid)
        for snippet in ("FROM absence_records", "code = 'SUS'",
                         "employee_id IS NOT NULL", "absence_date IS NOT NULL"):
            self.assertIn(snippet, awol_src, f'AWOL report missing SUS snippet: {snippet!r}')
            self.assertIn(snippet, grid_src, f'Attendance Grid missing SUS snippet: {snippet!r}')
        # AWOL report's version takes date-range params (has %s placeholders
        # and therefore needs the doubled %%Y-%%m-%%d); Attendance Grid's
        # older version has no params for this query, hence single %Y.
        self.assertIn("DATE_FORMAT(absence_date, '%%Y-%%m-%%d')", awol_src)
        self.assertIn("DATE_FORMAT(absence_date, '%Y-%m-%d')", grid_src)


# ──────────────────────────────────────────────────────────────────
# Step 6: Incident Report (IR) linking
# ──────────────────────────────────────────────────────────────────
class TestAwolIrLinking(unittest.TestCase):

    def setUp(self):
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            admin_session(sess, is_admin=True)

    def _get(self, date_from, date_to, table_rows):
        cursor_log = []
        with patch.object(appmod, 'get_central_db', make_db_factory(table_rows, cursor_log)), \
             patch.object(appmod, 'get_db', make_db_factory(table_rows, cursor_log)):
            resp = self.client.get('/admin/awol-report',
                                    query_string={'date_from': str(date_from), 'date_to': str(date_to)})
        return resp, cursor_log

    def test_matching_ir_renders_link_with_report_number(self):
        d = date(2026, 9, 1)
        scheduled = [{
            'employee_id': 'EMP-IR1', 'schedule_name': 'Incident Reported Employee', 'tl': 'TL1',
            'group_name': 'Group A', 'shift_time': '7am-4pm', 'absent_date': d, 'personid': 401,
        }]
        ir_rows = [{'employee_id': 'EMP-IR1', 'incident_date': d, 'report_number': 'IR-2026-0001'}]
        table_rows = {'employee_schedules': scheduled, 'dailytimerecordsfiltered': [],
                       'ot_requests': [], 'leave4day_requests': [], 'absence_records': [],
                       'incident_reports': ir_rows}
        resp, _ = self._get(d, d, table_rows)
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode()
        self.assertIn('Incident Reported Employee', html)
        self.assertIn('AWOL: <strong>1</strong>', html)
        self.assertIn('IR-2026-0001', html)
        # Blueprint is registered with url_prefix='/incident-reports' and
        # view_report(report_number) takes the raw report number as the
        # last path segment -- see app.py's
        # `app.register_blueprint(ir_bp, url_prefix='/incident-reports')`
        # and modules/incident_reports.py's `@ir_bp.route('/<report_number>')`.
        self.assertIn('href="/incident-reports/IR-2026-0001"', html)

    def test_no_matching_ir_renders_dash_not_broken_link(self):
        d = date(2026, 9, 1)
        scheduled = [{
            'employee_id': 'EMP-IR2', 'schedule_name': 'No Incident Employee', 'tl': 'TL1',
            'group_name': 'Group A', 'shift_time': '7am-4pm', 'absent_date': d, 'personid': 402,
        }]
        table_rows = {'employee_schedules': scheduled, 'dailytimerecordsfiltered': [],
                       'ot_requests': [], 'leave4day_requests': [], 'absence_records': [],
                       'incident_reports': []}
        resp, _ = self._get(d, d, table_rows)
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode()
        self.assertIn('No Incident Employee', html)
        # The base template's own sidebar nav legitimately links to
        # /incident-reports/, /incident-reports/new, and reuses the
        # file-text lucide icon elsewhere (e.g. "Employee Memos"), so assert
        # against the specific markup the awol_report.html template emits
        # for a populated IR cell -- `url_for('incident_reports.view_report',
        # report_number=...)` always resolves to "/incident-reports/<id>",
        # so this substring can only appear from that one Jinja branch.
        self.assertNotIn('href="/incident-reports/IR-', html,
                          'no IR report link should render when there is no matching report')
        self.assertIn('—', html)  # dash placeholder for the empty IR cell

    def test_ir_on_different_date_does_not_match(self):
        # An IR filed for the same employee but a different incident_date
        # must not be picked up for this AWOL row.
        d = date(2026, 9, 1)
        other_day = date(2026, 9, 5)
        scheduled = [{
            'employee_id': 'EMP-IR3', 'schedule_name': 'Wrong Date IR Employee', 'tl': 'TL1',
            'group_name': 'Group A', 'shift_time': '7am-4pm', 'absent_date': d, 'personid': 403,
        }]
        ir_rows = [{'employee_id': 'EMP-IR3', 'incident_date': other_day, 'report_number': 'IR-2026-0002'}]
        table_rows = {'employee_schedules': scheduled, 'dailytimerecordsfiltered': [],
                       'ot_requests': [], 'leave4day_requests': [], 'absence_records': [],
                       'incident_reports': ir_rows}
        resp, _ = self._get(d, d, table_rows)
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode()
        self.assertIn('Wrong Date IR Employee', html)
        self.assertNotIn('IR-2026-0002', html)

    def test_multiple_ir_same_employee_and_date_most_recent_created_wins(self):
        # Real SQL orders `ORDER BY created_at DESC, id DESC`, so the fixture
        # here lists rows already in that DESC order (RoutingCursor doesn't
        # itself evaluate ORDER BY -- it just hands back table_rows in the
        # order given, mirroring what the DB would already have sorted).
        # ir_map.setdefault() means the FIRST row seen for a given key wins,
        # so putting the most-recently-created row first is what makes it
        # the one that's kept.
        d = date(2026, 9, 1)
        scheduled = [{
            'employee_id': 'EMP-IR4', 'schedule_name': 'Multiple IR Employee', 'tl': 'TL1',
            'group_name': 'Group A', 'shift_time': '7am-4pm', 'absent_date': d, 'personid': 404,
        }]
        ir_rows = [
            # Most recently created (DESC order puts this first) -- must win.
            {'employee_id': 'EMP-IR4', 'incident_date': d, 'report_number': 'IR-2026-NEWEST'},
            {'employee_id': 'EMP-IR4', 'incident_date': d, 'report_number': 'IR-2026-OLDER'},
        ]
        table_rows = {'employee_schedules': scheduled, 'dailytimerecordsfiltered': [],
                       'ot_requests': [], 'leave4day_requests': [], 'absence_records': [],
                       'incident_reports': ir_rows}
        resp, _ = self._get(d, d, table_rows)
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode()
        self.assertIn('IR-2026-NEWEST', html)
        self.assertNotIn('IR-2026-OLDER', html)

    def test_ir_query_only_runs_when_emp_ids_nonempty(self):
        # No AWOL/unverified candidates at all (empty scheduled) -> emp_ids
        # is empty -> Step 6 IR query must never execute. Assert via the
        # same "should not be called" pattern the access-control tests use,
        # but at the cursor level: no query text should ever mention
        # incident_reports.
        d = date(2026, 9, 1)
        table_rows = {'employee_schedules': [], 'dailytimerecordsfiltered': [],
                       'ot_requests': [], 'leave4day_requests': [], 'absence_records': [],
                       'incident_reports': []}
        resp, cursor_log = self._get(d, d, table_rows)
        self.assertEqual(resp.status_code, 200)
        for cur in cursor_log:
            if cur.last_query:
                self.assertNotIn('incident_reports', cur.last_query.lower())


# ──────────────────────────────────────────────────────────────────
# SQL parameter safety for the two new Step 3c / Step 6 queries
# ──────────────────────────────────────────────────────────────────
class TestAwolSusAndIrSqlSafety(unittest.TestCase):

    def setUp(self):
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            admin_session(sess, is_admin=True)

    def test_sus_and_ir_queries_use_placeholders_not_interpolated_dates(self):
        d_from, d_to = date(2026, 9, 1), date(2026, 9, 3)
        scheduled = [{
            'employee_id': 'EMP-SAFE1', 'schedule_name': 'SQL Safety Employee 2', 'tl': 'TL1',
            'group_name': 'Group A', 'shift_time': '7am-4pm', 'absent_date': d_from, 'personid': 501,
        }]
        # SUS row deliberately does NOT match d_from -- this test wants the
        # SUS query to execute (to inspect its SQL text/params) without
        # actually excluding EMP-SAFE1, so it stays an AWOL record and the
        # Step 6 IR query still has an emp_id to look up.
        sus_rows = [{'employee_id': 'EMP-SAFE1', 'd': str(d_to + timedelta(days=30))}]
        ir_rows = [{'employee_id': 'EMP-SAFE1', 'incident_date': d_from, 'report_number': 'IR-SAFE-1'}]
        table_rows = {'employee_schedules': scheduled, 'dailytimerecordsfiltered': [],
                       'ot_requests': [], 'leave4day_requests': [], 'absence_records': sus_rows,
                       'incident_reports': ir_rows}
        cursor_log = []
        with patch.object(appmod, 'get_central_db', make_db_factory(table_rows, cursor_log)), \
             patch.object(appmod, 'get_db', make_db_factory(table_rows, cursor_log)):
            resp = self.client.get('/admin/awol-report',
                                    query_string={'date_from': str(d_from), 'date_to': str(d_to)})
        self.assertEqual(resp.status_code, 200)

        saw_sus_query = False
        saw_ir_query = False
        for cur in cursor_log:
            query, params = cur.last_query, cur.last_params
            if query is None:
                continue
            self.assertNotIn(str(d_from), query,
                              'date value must not be string-interpolated into the SQL text')
            self.assertNotIn(str(d_to), query,
                              'date value must not be string-interpolated into the SQL text')
            qlow = query.lower()
            if 'absence_records' in qlow:
                saw_sus_query = True
                # %%Y-%%m-%%d must survive intact -- the mock cursor's
                # execute() never performs PyMySQL's %-substitution itself,
                # so this is exactly the literal query text app.py builds.
                self.assertIn("%%Y-%%m-%%d", query)
                self.assertNotIn("%Y-%m-%d", query.replace('%%Y-%%m-%%d', ''),
                                  'no stray single-%% DATE_FORMAT leftover outside the doubled literal')
                self.assertIn('%s', query)
                self.assertIsNotNone(params)
                self.assertIn(d_from, tuple(params))
                self.assertIn(d_to, tuple(params))
            if 'incident_reports' in qlow:
                saw_ir_query = True
                self.assertIn('%s', query)
                self.assertIsNotNone(params)
                self.assertIn('EMP-SAFE1', tuple(params))
                self.assertIn(d_from, tuple(params))
                self.assertIn(d_to, tuple(params))
        self.assertTrue(saw_sus_query, 'expected the Step 3c SUS lookup query to run')
        self.assertTrue(saw_ir_query, 'expected the Step 6 IR lookup query to run')

    def test_source_query_text_for_sus_and_ir_uses_bound_placeholders(self):
        import inspect
        src = inspect.getsource(appmod.admin_awol_report)
        # SUS (Step 3c)
        self.assertIn("WHERE code = 'SUS'", src)
        self.assertIn("absence_date BETWEEN %s AND %s", src)
        self.assertIn("DATE_FORMAT(absence_date, '%%Y-%%m-%%d')", src)
        # IR (Step 6). NOTE: as currently written, this query has no COLLATE
        # clause on employee_id (unlike several join-based comparisons
        # elsewhere in app.py) -- it's a plain column-IN-params comparison
        # against literal parameter values, not a cross-table join, so no
        # COLLATE is needed for that to work correctly. See the report notes
        # for more on this.
        self.assertIn("FROM incident_reports", src)
        self.assertIn("incident_date BETWEEN %s AND %s", src)
        self.assertIn("employee_id IN ({placeholders})", src)
        self.assertIn("ORDER BY created_at DESC, id DESC", src)
        # Never an f-string embedding the actual date variables directly.
        self.assertNotRegex(src, r'BETWEEN\s*\{date_from\}\s*AND\s*\{date_to\}')


if __name__ == '__main__':
    unittest.main(verbosity=2)
