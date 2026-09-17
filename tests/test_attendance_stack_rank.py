"""
Mocked/offline tests for the new /admin/attendance-stack-rank route
(admin_attendance_stack_rank() in app.py, ~line 14632) and its template
templates/admin/attendance_stack_rank.html.

Stage 2 (tester) of the house ship workflow. Run with:
    ./venv/bin/python -m unittest tests.test_attendance_stack_rank -v
from the repo root.

Design notes
------------
* Same RoutingCursor/RoutingConn/make_db_factory pattern as
  tests/test_awol_report.py and tests/test_undertime_report.py:
  get_central_db()/get_db() are monkeypatched with fakes that route
  fetchall() results by which table name appears in the executed query
  text (longest match wins, so a combined JOIN query resolves to its
  primary/most-specific table).
* Attendance fixtures are RAW punch logs (personid/date/type) fed to the
  REAL (unmocked) build_attendance_map(), not pre-computed statuses --
  same approach as the undertime/AWOL suites, so the real IN/OUT pairing
  logic runs in every test.
* Most correctness tests capture the render_template() kwargs (via a
  capturing wrapper) to assert exact scheduled_count/present_count/
  attendance_pct/rank values, which are awkward (and more brittle) to
  pull back out of rendered HTML alone.
* The reconciliation suite runs the SAME underlying fixtures (schedule
  rows + raw punches + leave requests) through both
  admin_attendance_stack_rank() and admin_attendance_grid(), using an
  equivalent date range (a full past calendar month so the grid's
  month-based params and the stack-rank route's date_from/date_to line
  up exactly), and asserts the two routes' per-employee scheduled_count/
  present_count agree -- i.e. the duplicated precedence logic wasn't
  subtly reimplemented differently.
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
# Fakes (same shape as tests/test_awol_report.py / test_undertime_report.py)
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


def sched_row(employee_id, name, tl, group, personid, sdate, is_rest=False,
              shift_time='7am-4pm', status='Active', exit_date=None):
    return {
        'employee_id': employee_id, 'schedule_name': name, 'tl': tl,
        'group_name': group, 'status': status, 'exit_date': exit_date,
        'schedule_date': sdate, 'is_rest_day': is_rest, 'shift_time': shift_time,
        'personid': personid,
    }


def in_out(personid, d, in_h=7, in_m=0, out_h=16, out_m=0):
    return [
        {'personid': personid, 'date': datetime(d.year, d.month, d.day, in_h, in_m, 0), 'type': 'in'},
        {'personid': personid, 'date': datetime(d.year, d.month, d.day, out_h, out_m, 0), 'type': 'out'},
    ]


def base_dropdown():
    """Minimal gsheet_employees fixture answering the DISTINCT group_name /
    DISTINCT tl dropdown queries -- content doesn't matter for these tests."""
    return [{'group_name': 'Group A', 'tl': 'TL1'}]


# ──────────────────────────────────────────────────────────────────
# 4. Permission gate
# ──────────────────────────────────────────────────────────────────
class TestAccessControl(unittest.TestCase):

    def setUp(self):
        self.client = app.test_client()

    def test_no_session_redirects_to_dashboard_never_touches_db(self):
        central_calls = MagicMock(side_effect=AssertionError('get_central_db should not be called'))
        db_calls = MagicMock(side_effect=AssertionError('get_db should not be called'))
        with patch.object(appmod, 'get_central_db', central_calls), \
             patch.object(appmod, 'get_db', db_calls):
            resp = self.client.get('/admin/attendance-stack-rank')
        self.assertIn(resp.status_code, (301, 302, 303, 307, 308),
                       'Unauthenticated request must redirect, not render/crash')
        self.assertIn('/dashboard', resp.headers.get('Location', ''))
        central_calls.assert_not_called()
        db_calls.assert_not_called()

    def test_wrong_role_redirects_to_dashboard_never_touches_db(self):
        central_calls = MagicMock(side_effect=AssertionError('get_central_db should not be called'))
        db_calls = MagicMock(side_effect=AssertionError('get_db should not be called'))
        with patch.object(appmod, 'get_central_db', central_calls), \
             patch.object(appmod, 'get_db', db_calls):
            with self.client.session_transaction() as sess:
                admin_session(sess, is_admin=False, can_absences=False)
            resp = self.client.get('/admin/attendance-stack-rank')
        self.assertIn(resp.status_code, (301, 302, 303, 307, 308))
        self.assertIn('/dashboard', resp.headers.get('Location', ''))
        central_calls.assert_not_called()
        db_calls.assert_not_called()
        self.assertNotIn(b'Attendance Stack Rank', resp.data)

    def test_is_admin_true_passes_gate(self):
        cursor_log = []
        table_rows = {'gsheet_employees': base_dropdown(), 'employee_schedules': [],
                       'dailytimerecordsfiltered': [], 'leave4day_requests': []}
        with patch.object(appmod, 'get_central_db', make_db_factory(table_rows, cursor_log)), \
             patch.object(appmod, 'get_db', make_db_factory(table_rows, cursor_log)):
            with self.client.session_transaction() as sess:
                admin_session(sess, is_admin=True, can_absences=False)
            resp = self.client.get('/admin/attendance-stack-rank')
        self.assertEqual(resp.status_code, 200, 'is_admin=True must pass the gate and render')

    def test_can_absences_true_passes_gate_even_without_is_admin(self):
        cursor_log = []
        table_rows = {'gsheet_employees': base_dropdown(), 'employee_schedules': [],
                       'dailytimerecordsfiltered': [], 'leave4day_requests': []}
        with patch.object(appmod, 'get_central_db', make_db_factory(table_rows, cursor_log)), \
             patch.object(appmod, 'get_db', make_db_factory(table_rows, cursor_log)):
            with self.client.session_transaction() as sess:
                admin_session(sess, is_admin=False, can_absences=True)
            resp = self.client.get('/admin/attendance-stack-rank')
        self.assertEqual(resp.status_code, 200)


# ──────────────────────────────────────────────────────────────────
# Shared helper mixin for tests that need the real DB layer mocked +
# render_template capturing.
# ──────────────────────────────────────────────────────────────────
class StackRankTestCase(unittest.TestCase):

    def setUp(self):
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            admin_session(sess, is_admin=True)

    def _get(self, table_rows, query_string=None):
        cursor_log = []
        captured = {}
        orig_render = appmod.render_template

        def capturing(name, **ctx):
            captured[name] = ctx
            return orig_render(name, **ctx)

        with patch.object(appmod, 'get_central_db', make_db_factory(table_rows, cursor_log)), \
             patch.object(appmod, 'get_db', make_db_factory(table_rows, cursor_log)), \
             patch.object(appmod, 'render_template', capturing):
            resp = self.client.get('/admin/attendance-stack-rank', query_string=query_string or {})
        ctx = captured.get('admin/attendance_stack_rank.html', {})
        return resp, cursor_log, ctx

    @staticmethod
    def by_id(employees, employee_id):
        return next(e for e in employees if e['employee_id'] == employee_id)


# ──────────────────────────────────────────────────────────────────
# 1. Divide-by-zero guard
# ──────────────────────────────────────────────────────────────────
class TestDivideByZeroGuard(StackRankTestCase):

    def test_all_rest_days_gives_none_pct_and_none_rank_not_a_crash(self):
        d0 = date(2026, 8, 3)
        days = [d0 + timedelta(days=i) for i in range(5)]
        scheduled = [sched_row('EMP-REST', 'All Rest Employee', 'TL1', 'Group A', 601, d, is_rest=True)
                     for d in days]
        table_rows = {'gsheet_employees': base_dropdown(), 'employee_schedules': scheduled,
                       'dailytimerecordsfiltered': [], 'leave4day_requests': []}
        resp, _, ctx = self._get(table_rows, {'date_from': str(days[0]), 'date_to': str(days[-1])})
        self.assertEqual(resp.status_code, 200, 'all-rest-day employee must not crash the route')
        emp = self.by_id(ctx['employees'], 'EMP-REST')
        self.assertEqual(emp['scheduled_count'], 0)
        self.assertIsNone(emp['attendance_pct'],
                           'zero scheduled days must yield attendance_pct=None, never a fake 0%/100%')
        self.assertIsNone(emp['rank'], 'an employee with 0 scheduled days must not receive a rank')
        html = resp.data.decode()
        self.assertNotIn('0.0%', html)
        self.assertNotIn('100.0%', html)

    def test_all_pl_leave_days_nets_to_zero_scheduled_gives_none(self):
        # Every scheduled day is covered by Parental Leave -> scheduled_count
        # is incremented then decremented right back down on each such day,
        # netting to exactly 0 (never negative) -> must still guard divide.
        d0 = date(2026, 8, 3)
        days = [d0 + timedelta(days=i) for i in range(4)]
        scheduled = [sched_row('EMP-ALLPL', 'All PL Employee', 'TL1', 'Group A', 602, d)
                     for d in days]
        leave = [{'employee_id': 'EMP-ALLPL', 'leave_date': d, 'leave_type_name': 'Parental Leave',
                  'status': 'Approved'} for d in days]
        table_rows = {'gsheet_employees': base_dropdown(), 'employee_schedules': scheduled,
                       'dailytimerecordsfiltered': [], 'leave4day_requests': leave}
        resp, _, ctx = self._get(table_rows, {'date_from': str(days[0]), 'date_to': str(days[-1])})
        self.assertEqual(resp.status_code, 200)
        emp = self.by_id(ctx['employees'], 'EMP-ALLPL')
        self.assertEqual(emp['scheduled_count'], 0,
                          'PL cancels out scheduled_count exactly (never negative)')
        self.assertIsNone(emp['attendance_pct'])
        self.assertIsNone(emp['rank'])

    def test_all_magic_leave_days_also_nets_to_zero(self):
        d0 = date(2026, 8, 3)
        days = [d0 + timedelta(days=i) for i in range(3)]
        scheduled = [sched_row('EMP-ALLML', 'All ML Employee', 'TL1', 'Group A', 603, d)
                     for d in days]
        leave = [{'employee_id': 'EMP-ALLML', 'leave_date': d, 'leave_type_name': 'Magic Leave',
                  'status': 'Approved'} for d in days]
        table_rows = {'gsheet_employees': base_dropdown(), 'employee_schedules': scheduled,
                       'dailytimerecordsfiltered': [], 'leave4day_requests': leave}
        resp, _, ctx = self._get(table_rows, {'date_from': str(days[0]), 'date_to': str(days[-1])})
        self.assertEqual(resp.status_code, 200)
        emp = self.by_id(ctx['employees'], 'EMP-ALLML')
        self.assertEqual(emp['scheduled_count'], 0)
        self.assertIsNone(emp['attendance_pct'])
        self.assertIsNone(emp['rank'])

    def test_divide_by_zero_employee_renders_em_dash_not_zero_or_hundred_percent(self):
        d0 = date(2026, 8, 3)
        days = [d0 + timedelta(days=i) for i in range(3)]
        scheduled = [sched_row('EMP-DASH', 'Dash Employee', 'TL1', 'Group A', 604, d, is_rest=True)
                     for d in days]
        table_rows = {'gsheet_employees': base_dropdown(), 'employee_schedules': scheduled,
                       'dailytimerecordsfiltered': [], 'leave4day_requests': []}
        resp, _, ctx = self._get(table_rows, {'date_from': str(days[0]), 'date_to': str(days[-1])})
        html = resp.data.decode()
        self.assertIn('Dash Employee', html)
        # Locate the row and check its rank/pct cells render as an em-dash.
        self.assertIn('>—<', html)

    def test_zero_scheduled_employee_is_excluded_from_ranked_count_and_avg(self):
        d0 = date(2026, 8, 3)
        days = [d0 + timedelta(days=i) for i in range(3)]
        rest_emp = [sched_row('EMP-RESTONLY', 'Rest Only', 'TL1', 'Group A', 605, d, is_rest=True)
                    for d in days]
        real_emp = [sched_row('EMP-REAL', 'Real Employee', 'TL1', 'Group A', 606, d)
                    for d in days]
        punches = []
        for d in days:
            punches += in_out(606, d)
        table_rows = {'gsheet_employees': base_dropdown(),
                       'employee_schedules': rest_emp + real_emp,
                       'dailytimerecordsfiltered': punches, 'leave4day_requests': []}
        resp, _, ctx = self._get(table_rows, {'date_from': str(days[0]), 'date_to': str(days[-1])})
        self.assertEqual(ctx['summary']['ranked_count'], 1,
                          'only the employee with scheduled_count > 0 should count toward ranked_count')
        self.assertEqual(ctx['summary']['avg_pct'], 100.0)


# ──────────────────────────────────────────────────────────────────
# 2. Ranking correctness
# ──────────────────────────────────────────────────────────────────
class TestRankingCorrectness(StackRankTestCase):

    def test_rank_order_by_pct_desc_then_scheduled_count_desc_then_name_asc(self):
        d0 = date(2026, 8, 3)
        days = [d0 + timedelta(days=i) for i in range(10)]  # 10 scheduled days

        def build(emp_id, name, personid, present_n):
            rows = [sched_row(emp_id, name, 'TL1', 'Group A', personid, d) for d in days]
            punches = []
            for d in days[:present_n]:
                punches += in_out(personid, d)
            return rows, punches

        scheduled, punches = [], []
        # C: 10 scheduled, 10 present -> 100.0%
        r, p = build('EMP-C', 'Charlie', 701, 10); scheduled += r; punches += p
        # D: 5 scheduled, 5 present -> 100.0% (tie on pct, fewer scheduled -> ranks below C)
        d5 = days[:5]
        d_rows = [sched_row('EMP-D', 'Delta', 'TL1', 'Group A', 702, d) for d in d5]
        scheduled += d_rows
        for d in d5:
            punches += in_out(702, d)
        # Alpha: 10 scheduled, 9 present -> 90.0%
        r, p = build('EMP-ALPHA', 'Alpha', 703, 9); scheduled += r; punches += p
        # Zulu: 10 scheduled, 9 present -> 90.0% (tie on pct AND scheduled_count -> name asc)
        r, p = build('EMP-ZULU', 'Zulu', 704, 9); scheduled += r; punches += p
        # B: 10 scheduled, 5 present -> 50.0%
        r, p = build('EMP-B', 'Bravo', 705, 5); scheduled += r; punches += p

        table_rows = {'gsheet_employees': base_dropdown(), 'employee_schedules': scheduled,
                       'dailytimerecordsfiltered': punches, 'leave4day_requests': []}
        resp, _, ctx = self._get(table_rows, {'date_from': str(days[0]), 'date_to': str(days[-1])})
        self.assertEqual(resp.status_code, 200)

        employees = ctx['employees']
        ranked = [e for e in employees if e['rank'] is not None]
        order = [e['employee_id'] for e in ranked]
        self.assertEqual(order, ['EMP-C', 'EMP-D', 'EMP-ALPHA', 'EMP-ZULU', 'EMP-B'],
                          'expected pct desc, tie-break scheduled_count desc, then name asc')
        ranks = [e['rank'] for e in ranked]
        self.assertEqual(ranks, [1, 2, 3, 4, 5], 'ranks must be contiguous starting at 1')

        c = self.by_id(employees, 'EMP-C')
        d = self.by_id(employees, 'EMP-D')
        alpha = self.by_id(employees, 'EMP-ALPHA')
        zulu = self.by_id(employees, 'EMP-ZULU')
        b = self.by_id(employees, 'EMP-B')
        self.assertEqual(c['attendance_pct'], 100.0)
        self.assertEqual(d['attendance_pct'], 100.0)
        self.assertEqual(alpha['attendance_pct'], 90.0)
        self.assertEqual(zulu['attendance_pct'], 90.0)
        self.assertEqual(b['attendance_pct'], 50.0)
        self.assertEqual(c['scheduled_count'], 10)
        self.assertEqual(d['scheduled_count'], 5)

    def test_rank_computed_within_filtered_set_not_globally(self):
        # Simulates the SQL WHERE clause having already narrowed the
        # returned rows to a single group (as the real query would) --
        # rank must restart at 1 for the filtered set rather than carrying
        # over the "global" rank each employee would have had unfiltered.
        d0 = date(2026, 8, 3)
        days = [d0 + timedelta(days=i) for i in range(4)]

        def build(emp_id, name, group, personid, present_n):
            rows = [sched_row(emp_id, name, 'TL1', group, personid, d) for d in days]
            punches = []
            for d in days[:present_n]:
                punches += in_out(personid, d)
            return rows, punches

        # Full (unfiltered) universe: Group A has a 100% and a 25% performer;
        # Group B has a single 50% performer.
        full_scheduled, full_punches = [], []
        r, p = build('EMP-GA-HIGH', 'GA High', 'Group A', 801, 4); full_scheduled += r; full_punches += p
        r, p = build('EMP-GA-LOW', 'GA Low', 'Group A', 802, 1); full_scheduled += r; full_punches += p
        r, p = build('EMP-GB-MID', 'GB Mid', 'Group B', 803, 2); full_scheduled += r; full_punches += p

        table_rows_full = {'gsheet_employees': base_dropdown(), 'employee_schedules': full_scheduled,
                            'dailytimerecordsfiltered': full_punches, 'leave4day_requests': []}
        resp_full, _, ctx_full = self._get(table_rows_full,
                                            {'date_from': str(days[0]), 'date_to': str(days[-1])})
        self.assertEqual(resp_full.status_code, 200)
        full_ranks = {e['employee_id']: e['rank'] for e in ctx_full['employees']}
        # Globally, GB Mid (50%) ranks 2nd behind GA High (100%), ahead of GA Low (25%).
        self.assertEqual(full_ranks['EMP-GA-HIGH'], 1)
        self.assertEqual(full_ranks['EMP-GB-MID'], 2)
        self.assertEqual(full_ranks['EMP-GA-LOW'], 3)

        # Now simulate a group=Group A filter: the SQL would only return
        # GA High and GA Low. GB Mid's rank must not leak into this set.
        ga_scheduled, ga_punches = [], []
        r, p = build('EMP-GA-HIGH', 'GA High', 'Group A', 801, 4); ga_scheduled += r; ga_punches += p
        r, p = build('EMP-GA-LOW', 'GA Low', 'Group A', 802, 1); ga_scheduled += r; ga_punches += p
        table_rows_filtered = {'gsheet_employees': base_dropdown(), 'employee_schedules': ga_scheduled,
                                'dailytimerecordsfiltered': ga_punches, 'leave4day_requests': []}
        resp_filt, _, ctx_filt = self._get(
            table_rows_filtered,
            {'date_from': str(days[0]), 'date_to': str(days[-1]), 'group': 'Group A'})
        self.assertEqual(resp_filt.status_code, 200)
        filt_employees = ctx_filt['employees']
        self.assertEqual(len(filt_employees), 2, 'filtered set must not include GB Mid at all')
        self.assertNotIn('EMP-GB-MID', [e['employee_id'] for e in filt_employees])
        filt_ranks = {e['employee_id']: e['rank'] for e in filt_employees}
        self.assertEqual(filt_ranks['EMP-GA-HIGH'], 1)
        self.assertEqual(filt_ranks['EMP-GA-LOW'], 2,
                          'GA Low must be rank 2 WITHIN the filtered set, not rank 3 carried over '
                          'from the unfiltered universe')
        self.assertEqual(ctx_filt['summary']['ranked_count'], 2)

    def test_group_tl_emp_search_params_are_bound_placeholders(self):
        d0 = date(2026, 8, 3)
        scheduled = [sched_row('EMP-SQL', 'Sql Safety', 'TL1', 'Group A', 810, d0)]
        table_rows = {'gsheet_employees': base_dropdown(), 'employee_schedules': scheduled,
                       'dailytimerecordsfiltered': [], 'leave4day_requests': []}
        resp, cursor_log, _ = self._get(
            table_rows,
            {'date_from': str(d0), 'date_to': str(d0),
             'group': "Group' OR 1=1--", 'tl': 'TL"1', 'emp_search': "o'brien%"})
        self.assertEqual(resp.status_code, 200)
        saw_main_query = False
        for cur in cursor_log:
            query, params = cur.last_query, cur.last_params
            if query is None or 'employee_schedules' not in query.lower():
                continue
            saw_main_query = True
            self.assertNotIn("Group' OR 1=1--", query,
                              'filter value must not be string-interpolated into SQL text')
            self.assertIn('%s', query)
            self.assertIn("Group' OR 1=1--", tuple(params))
            self.assertIn('TL"1', tuple(params))
        self.assertTrue(saw_main_query)


# ──────────────────────────────────────────────────────────────────
# 3. Reconciliation against admin_attendance_grid
# ──────────────────────────────────────────────────────────────────
class TestReconciliationWithAttendanceGrid(unittest.TestCase):
    """
    Runs identical raw fixtures (schedule rows, punches, leave) through
    both admin_attendance_stack_rank() and admin_attendance_grid() over
    an equivalent past-month date range, and checks that the per-employee
    scheduled_count/present_count each route computes agree exactly.
    """

    def setUp(self):
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            admin_session(sess, is_admin=True, can_absences=True)

        self.month_start = date(2026, 8, 1)
        self.month_end = date(2026, 8, 31)

        days = [date(2026, 8, i) for i in (1, 2, 3, 4, 5)]
        self.d1, self.d2, self.d3, self.d4, self.d5 = days

        scheduled = []
        punches = []
        leave = []

        # REC-1: present day, absent day, rest day, PL-leave day (nets to 0),
        # other-leave day (scheduled-but-absent).
        scheduled += [
            sched_row('REC-1', 'Reconcile One', 'TL1', 'G1', 501, self.d1),
            sched_row('REC-1', 'Reconcile One', 'TL1', 'G1', 501, self.d2),
            sched_row('REC-1', 'Reconcile One', 'TL1', 'G1', 501, self.d3, is_rest=True),
            sched_row('REC-1', 'Reconcile One', 'TL1', 'G1', 501, self.d4),
            sched_row('REC-1', 'Reconcile One', 'TL1', 'G1', 501, self.d5),
        ]
        punches += in_out(501, self.d1)  # present
        # d2: no punch -> absent
        leave += [
            {'employee_id': 'REC-1', 'leave_date': self.d4, 'leave_type_name': 'Parental Leave',
             'status': 'Approved'},
            {'employee_id': 'REC-1', 'leave_date': self.d5, 'leave_type_name': 'Vacation Leave',
             'status': 'Approved'},
        ]

        # REC-2: exit_date = d3 -> d4/d5 must be skipped by BOTH routes.
        scheduled += [
            sched_row('REC-2', 'Reconcile Two', 'TL1', 'G1', 502, d, exit_date=self.d3)
            for d in days
        ]
        punches += in_out(502, self.d1)
        punches += in_out(502, self.d2)
        punches += in_out(502, self.d3)

        # REC-3: FTS IN on d1 (lone late 'out', no prior 'in'), FTS OUT on d2
        # (lone 'in', nothing to pair) -- both must count as present in both
        # routes' precedence.
        scheduled += [
            sched_row('REC-3', 'Reconcile Three', 'TL2', 'G2', 503, self.d1),
            sched_row('REC-3', 'Reconcile Three', 'TL2', 'G2', 503, self.d2),
        ]
        punches += [{'personid': 503, 'date': datetime(2026, 8, 1, 14, 0, 0), 'type': 'out'}]
        punches += [{'personid': 503, 'date': datetime(2026, 8, 2, 20, 30, 0), 'type': 'in'}]

        self.table_rows = {
            'gsheet_employees': base_dropdown(),
            'employee_schedules': scheduled,
            'dailytimerecordsfiltered': punches,
            'leave4day_requests': leave,
            'attendance_notes': [],
            'absence_records': [],
            'ohrm_holiday': [],
        }

    def _run_both(self):
        cursor_log = []
        captured = {}
        orig_render = appmod.render_template

        def capturing(name, **ctx):
            captured[name] = ctx
            return orig_render(name, **ctx)

        with patch.object(appmod, 'get_central_db', make_db_factory(self.table_rows, cursor_log)), \
             patch.object(appmod, 'get_db', make_db_factory(self.table_rows, cursor_log)), \
             patch.object(appmod, 'render_template', capturing):
            resp_rank = self.client.get(
                '/admin/attendance-stack-rank',
                query_string={'date_from': str(self.month_start), 'date_to': str(self.month_end)})
            resp_grid = self.client.get(
                '/admin/attendance-grid',
                query_string={'month': '2026-08'})
        return resp_rank, resp_grid, captured

    def test_scheduled_and_present_counts_match_between_routes(self):
        resp_rank, resp_grid, captured = self._run_both()
        self.assertEqual(resp_rank.status_code, 200)
        self.assertEqual(resp_grid.status_code, 200)

        rank_employees = captured['admin/attendance_stack_rank.html']['employees']
        grid_employees = captured['admin/attendance_grid.html']['employees']

        rank_by_id = {e['employee_id']: e for e in rank_employees}
        grid_by_id = {e['employee_id']: e for e in grid_employees}

        for emp_id in ('REC-1', 'REC-2', 'REC-3'):
            self.assertIn(emp_id, rank_by_id, f'{emp_id} missing from stack-rank output')
            self.assertIn(emp_id, grid_by_id, f'{emp_id} missing from grid output')
            r, g = rank_by_id[emp_id], grid_by_id[emp_id]
            self.assertEqual(r['scheduled_count'], g['scheduled_count'],
                              f'{emp_id}: scheduled_count diverged between stack-rank ({r["scheduled_count"]}) '
                              f'and attendance-grid ({g["scheduled_count"]})')
            self.assertEqual(r['present_count'], g['present_count'],
                              f'{emp_id}: present_count diverged between stack-rank ({r["present_count"]}) '
                              f'and attendance-grid ({g["present_count"]})')

        # Pin down expected values explicitly too, so a shared bug in BOTH
        # routes (not just a divergence between them) still gets caught.
        rec1 = rank_by_id['REC-1']
        self.assertEqual(rec1['scheduled_count'], 3, 'd1 present + d2 absent + d5 other-leave = 3 '
                                                       '(rest day excluded, PL day nets to 0)')
        self.assertEqual(rec1['present_count'], 1)

        rec2 = rank_by_id['REC-2']
        self.assertEqual(rec2['scheduled_count'], 3, 'only d1-d3 count; d4/d5 are past exit_date')
        self.assertEqual(rec2['present_count'], 3)

        rec3 = rank_by_id['REC-3']
        self.assertEqual(rec3['scheduled_count'], 2)
        self.assertEqual(rec3['present_count'], 2, 'FTS IN and FTS OUT both count as present')


# ──────────────────────────────────────────────────────────────────
# 5. Basic route smoke tests
# ──────────────────────────────────────────────────────────────────
class TestSmoke(StackRankTestCase):

    def test_get_with_authorized_session_returns_200_no_template_error(self):
        d0 = date(2026, 8, 3)
        days = [d0 + timedelta(days=i) for i in range(3)]
        scheduled = [sched_row('EMP-SMOKE', 'Smoke Employee', 'TL1', 'Group A', 901, d) for d in days]
        punches = []
        for d in days:
            punches += in_out(901, d)
        table_rows = {'gsheet_employees': base_dropdown(), 'employee_schedules': scheduled,
                       'dailytimerecordsfiltered': punches, 'leave4day_requests': []}
        resp, _, ctx = self._get(table_rows, {'date_from': str(days[0]), 'date_to': str(days[-1])})
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'Attendance Stack Rank', resp.data)
        self.assertIn(b'Smoke Employee', resp.data)

    def test_get_with_group_tl_emp_search_params_returns_200(self):
        d0 = date(2026, 8, 3)
        scheduled = [sched_row('EMP-FQ', 'Filter Query Employee', 'TL9', 'Group Q', 902, d0)]
        table_rows = {'gsheet_employees': base_dropdown(), 'employee_schedules': scheduled,
                       'dailytimerecordsfiltered': [], 'leave4day_requests': []}
        resp, _, ctx = self._get(
            table_rows,
            {'date_from': str(d0), 'date_to': str(d0),
             'group': 'Group Q', 'tl': 'TL9', 'emp_search': 'Filter'})
        self.assertEqual(resp.status_code, 200)

    def test_empty_scheduled_set_renders_empty_state_no_crash(self):
        d0 = date(2026, 8, 3)
        table_rows = {'gsheet_employees': base_dropdown(), 'employee_schedules': [],
                       'dailytimerecordsfiltered': [], 'leave4day_requests': []}
        resp, _, ctx = self._get(table_rows, {'date_from': str(d0), 'date_to': str(d0)})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(ctx['employees'], [])
        self.assertEqual(ctx['summary'], {'ranked_count': 0, 'avg_pct': None})
        self.assertIn(b'No scheduled employees found', resp.data)

    def test_malformed_date_params_fall_back_safely(self):
        table_rows = {'gsheet_employees': base_dropdown(), 'employee_schedules': [],
                       'dailytimerecordsfiltered': [], 'leave4day_requests': []}
        resp, _, _ = self._get(table_rows, {'date_from': "2026-08-01'; DROP TABLE x--",
                                             'date_to': 'not-a-date'})
        self.assertEqual(resp.status_code, 200)

    def test_special_characters_in_names_render_safely(self):
        d0 = date(2026, 8, 3)
        scheduled = [sched_row("EMP-<script>", 'O\'Brien <script>alert(1)</script> Ünïcödé',
                                'TL "Quoted"', "Group % Wild'card", 903, d0)]
        punches = in_out(903, d0)
        table_rows = {'gsheet_employees': base_dropdown(), 'employee_schedules': scheduled,
                       'dailytimerecordsfiltered': punches, 'leave4day_requests': []}
        resp, _, _ = self._get(table_rows, {'date_from': str(d0), 'date_to': str(d0)})
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode()
        self.assertNotIn('<script>alert(1)</script>', html)
        self.assertIn('Ünïcödé', html)


# ──────────────────────────────────────────────────────────────────
# Jinja syntax sanity
# ──────────────────────────────────────────────────────────────────
class TestTemplateSyntax(unittest.TestCase):
    def test_attendance_stack_rank_template_compiles(self):
        appmod.app.jinja_env.get_template('admin/attendance_stack_rank.html')


if __name__ == '__main__':
    unittest.main(verbosity=2)
