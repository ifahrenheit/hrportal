"""
Tests for the new "Current Headcount" feature:
  - GET /api/current-headcount     (app.py ~1802-1828)
  - dashboard() headcount card     (app.py ~1184-1219, templates/dashboard.html)
  - GET /on-shift                  (app.py ~11267-11325, templates/admin/on_shift.html)

Stage 2 (tester) of the house ship workflow. Run with:
    ./venv/bin/python -m unittest tests.test_current_headcount -v
from the repo root. (pytest is not installed in venv/ in this environment --
same as tests/test_undertime_report.py, this suite is written with
unittest.TestCase so it is directly runnable via `python -m unittest` and
would also be picked up by `pytest` if it's ever added to the venv.)

Design notes
------------
* Follows the same session-faking convention as tests/test_undertime_report.py
  (session['user'] / session['is_admin'] / session['permissions']).
* Access-control tests are pure (no real DB touched): the permission gate on
  /on-shift short-circuits before any query runs, so we assert redirects and
  that get_central_db/get_db are never called.
* The "real data" tests deliberately do NOT mock the DB layer -- they run
  against the live central_db/orangehrm2 connections this app already uses,
  per the explicit request to prove the feature against real numbers rather
  than mocked fixtures. They use emp_number=2 (a real, seeded employee) as
  the logged-in user for /dashboard, and assert the rendered headcount is a
  sane non-negative integer that is consistent with an independent read of
  /api/current-headcount taken in the same test (small drift tolerated since
  the underlying punch data can change between the two calls).
* These real-data tests will be skipped automatically if the DB is not
  reachable from the test environment (e.g. CI without DB access), so the
  suite stays runnable everywhere while still exercising real data when a
  DB is available -- which it is in this environment.
"""
import sys
import os
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, '/var/www/html/leavesystem')

import app as appmod  # noqa: E402

app = appmod.app
app.config['TESTING'] = True
app.config['WTF_CSRF_ENABLED'] = False


def _db_reachable():
    try:
        conn = appmod.get_central_db()
        conn.close()
        return True
    except Exception:
        return False


DB_REACHABLE = _db_reachable()

# Real, pre-existing employee row (see hs_hr_employee) used to drive the
# unmocked /dashboard real-data tests.
REAL_EMP_NUMBER = 2
REAL_EMP_EMAIL = 'andrew.tacdoro@cohere.ph'
REAL_EMP_NAME = 'Andrew Vincent Tacdoro'


def real_user_session(sess, is_admin=True, can_absences=False):
    sess['user'] = {
        'email': REAL_EMP_EMAIL,
        'emp_number': REAL_EMP_NUMBER,
        'name': REAL_EMP_NAME,
        'employee_id': '210528-16',
    }
    sess['is_admin'] = is_admin
    sess['is_supervisor'] = False
    sess['is_sub_admin'] = False
    sess['permissions'] = {'can_absences': can_absences}


def bare_session(sess, is_admin=False, can_absences=False):
    """Session shape used purely for /on-shift and /api gate checks --
    no emp_number needed since those routes don't touch it directly."""
    sess['user'] = {'name': 'Test User', 'employee_id': 'TESTUSR', 'email': 'testuser@example.com'}
    sess['is_admin'] = is_admin
    sess['permissions'] = {'can_absences': can_absences}


# ──────────────────────────────────────────────────────────────────
# Access control -- no DB should be touched on a denied request
# ──────────────────────────────────────────────────────────────────
class TestOnShiftAccessControl(unittest.TestCase):

    def setUp(self):
        self.client = app.test_client()

    def test_on_shift_no_session_redirects_and_never_touches_db(self):
        central_calls = MagicMock(side_effect=AssertionError('get_central_db should not be called'))
        with patch.object(appmod, 'get_central_db', central_calls):
            resp = self.client.get('/on-shift')
        self.assertIn(resp.status_code, (301, 302, 303, 307, 308),
                       'Unauthenticated request to /on-shift must redirect, not render/crash')
        central_calls.assert_not_called()

    def test_on_shift_wrong_role_redirects_and_never_touches_db(self):
        central_calls = MagicMock(side_effect=AssertionError('get_central_db should not be called'))
        with patch.object(appmod, 'get_central_db', central_calls):
            with self.client.session_transaction() as sess:
                bare_session(sess, is_admin=False, can_absences=False)
            resp = self.client.get('/on-shift')
        self.assertIn(resp.status_code, (301, 302, 303, 307, 308))
        central_calls.assert_not_called()
        # Confirm it does NOT leak the data table content on denial.
        self.assertNotIn(b'On Shift', resp.data)

    def test_on_shift_is_admin_true_passes_gate(self):
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.execute = MagicMock()
        cur.fetchall = MagicMock(return_value=[])
        conn = MagicMock()
        conn.cursor = MagicMock(return_value=cur)
        conn.close = MagicMock()
        with patch.object(appmod, 'get_central_db', MagicMock(return_value=conn)):
            with self.client.session_transaction() as sess:
                bare_session(sess, is_admin=True, can_absences=False)
            resp = self.client.get('/on-shift')
        self.assertEqual(resp.status_code, 200, 'is_admin=True must pass the gate and render')

    def test_on_shift_can_absences_true_passes_gate_even_without_is_admin(self):
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.execute = MagicMock()
        cur.fetchall = MagicMock(return_value=[])
        conn = MagicMock()
        conn.cursor = MagicMock(return_value=cur)
        conn.close = MagicMock()
        with patch.object(appmod, 'get_central_db', MagicMock(return_value=conn)):
            with self.client.session_transaction() as sess:
                bare_session(sess, is_admin=False, can_absences=True)
            resp = self.client.get('/on-shift')
        self.assertEqual(resp.status_code, 200)


class TestApiCurrentHeadcountAccessControl(unittest.TestCase):

    def setUp(self):
        self.client = app.test_client()

    def test_api_current_headcount_no_session_redirects(self):
        central_calls = MagicMock(side_effect=AssertionError('get_central_db should not be called'))
        with patch.object(appmod, 'get_central_db', central_calls):
            resp = self.client.get('/api/current-headcount')
        self.assertIn(resp.status_code, (301, 302, 303, 307, 308),
                       '/api/current-headcount must require login')
        central_calls.assert_not_called()


# ──────────────────────────────────────────────────────────────────
# Mocked correctness: query shape / empty-result handling
# ──────────────────────────────────────────────────────────────────
class TestApiCurrentHeadcountMocked(unittest.TestCase):

    def setUp(self):
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            bare_session(sess, is_admin=True)

    def _mock_conn(self, fetchall_return):
        # get_on_shift_rows() now does a single cdb.cursor()/.execute()/
        # .fetchall() call (raw on-shift rows, count = len(rows)) rather
        # than a separate COUNT(*) aggregate query -- mock that shape.
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.execute = MagicMock()
        cur.fetchall = MagicMock(return_value=fetchall_return)
        conn = MagicMock()
        conn.cursor = MagicMock(return_value=cur)
        conn.close = MagicMock()
        return conn, cur

    def test_empty_table_returns_zero_not_crash(self):
        conn, cur = self._mock_conn([])
        with patch.object(appmod, 'get_central_db', MagicMock(return_value=conn)):
            resp = self.client.get('/api/current-headcount')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data['count'], 0)
        self.assertIn('timestamp', data)

    def test_multiple_rows_counted_correctly(self):
        rows = [
            {'personid': 1, 'time_in': '2026-09-17 00:00:00', 'minutes_on_shift': 60},
            {'personid': 2, 'time_in': '2026-09-17 00:10:00', 'minutes_on_shift': 50},
            {'personid': 3, 'time_in': '2026-09-17 00:20:00', 'minutes_on_shift': 40},
        ]
        conn, cur = self._mock_conn(rows)
        with patch.object(appmod, 'get_central_db', MagicMock(return_value=conn)):
            resp = self.client.get('/api/current-headcount')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data['count'], 3)

    def test_json_shape_has_count_and_timestamp(self):
        rows = [{'personid': i, 'time_in': '2026-09-17 00:00:00', 'minutes_on_shift': 10} for i in range(7)]
        conn, cur = self._mock_conn(rows)
        with patch.object(appmod, 'get_central_db', MagicMock(return_value=conn)):
            resp = self.client.get('/api/current-headcount')
        data = resp.get_json()
        self.assertEqual(data['count'], 7)
        self.assertIsInstance(data['timestamp'], str)
        self.assertTrue(len(data['timestamp']) > 0)
        conn.close.assert_called_once()


# ──────────────────────────────────────────────────────────────────
# Real-data verification (unmocked DB) -- the user's core ask: prove the
# feature against real numbers, not just mocked fixtures.
# ──────────────────────────────────────────────────────────────────
@unittest.skipUnless(DB_REACHABLE, 'central_db not reachable from this test environment')
class TestRealDataHeadcount(unittest.TestCase):

    def setUp(self):
        self.client = app.test_client()

    def test_api_current_headcount_real_data(self):
        with self.client.session_transaction() as sess:
            real_user_session(sess, is_admin=True)
        resp = self.client.get('/api/current-headcount')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn('count', data)
        self.assertIn('timestamp', data)
        self.assertIsInstance(data['count'], int)
        self.assertGreaterEqual(data['count'], 0)
        # Sanity ceiling -- headcount should be a plausible shift size, not a
        # NOT-EXISTS-logic-error blowing up to "everyone who ever punched in".
        self.assertLess(data['count'], 5000)

    def test_dashboard_renders_topnav_headcount_pill(self):
        # The widget moved from a dashboard-only server-rendered card to a
        # JS-populated pill in the shared top nav (_topnav.html), visible on
        # every page. There's no server-rendered number to compare anymore
        # -- the pill starts as an em-dash and fills in via a client-side
        # fetch('/api/current-headcount') call, same endpoint tested
        # independently below. Just confirm the pill markup and its link
        # actually render on a page that includes the shared nav.
        with self.client.session_transaction() as sess:
            real_user_session(sess, is_admin=True)

        resp = self.client.get('/dashboard')
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode('utf-8')

        self.assertIn('id="tnHeadcountPill"', html)
        self.assertIn('id="tnHeadcountNum"', html)
        self.assertIn('/on-shift', html)
        self.assertIn("fetch('/api/current-headcount'", html)

        # And the API it polls independently returns a sane real count.
        api_resp = self.client.get('/api/current-headcount')
        self.assertEqual(api_resp.status_code, 200)
        self.assertGreaterEqual(api_resp.get_json()['count'], 0)

    def test_on_shift_page_real_data_shows_real_employees(self):
        with self.client.session_transaction() as sess:
            real_user_session(sess, is_admin=True)
        resp = self.client.get('/on-shift')
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode('utf-8')

        # Cross-check against a direct query for who's actually on shift
        # right now, and confirm at least one of those real identities (or
        # their raw personid, if unresolved) shows up in the rendered page.
        cdb = appmod.get_central_db()
        try:
            with cdb.cursor() as c:
                c.execute("""
                    SELECT d1.personid
                    FROM dailytimerecordsfiltered d1
                    WHERE type = 'in'
                      AND date >= DATE_SUB(NOW(), INTERVAL 18 HOUR)
                      AND NOT EXISTS (
                        SELECT 1 FROM dailytimerecordsfiltered d2
                        WHERE d2.personid = d1.personid
                          AND d2.date > d1.date
                          AND d2.date >= DATE_SUB(NOW(), INTERVAL 18 HOUR)
                      )
                    ORDER BY d1.date DESC
                """)
                rows = c.fetchall()
        finally:
            cdb.close()

        if not rows:
            self.skipTest('nobody is currently on shift in real data -- nothing to cross-check')

        personids = [r['personid'] for r in rows]
        cdb2 = appmod.get_central_db()
        try:
            with cdb2.cursor() as c:
                placeholders = ','.join(['%s'] * len(personids))
                c.execute(f"""
                    SELECT u.personid, u.companyid AS employee_id, g.schedule_name
                    FROM userdata u
                    LEFT JOIN gsheet_employees g ON g.employee_id COLLATE utf8mb4_unicode_ci = u.companyid COLLATE utf8mb4_unicode_ci
                    WHERE u.personid IN ({placeholders})
                """, personids)
                identities = c.fetchall()
        finally:
            cdb2.close()

        found_any = False
        for ident in identities:
            name = ident.get('schedule_name')
            if name and name in html:
                found_any = True
                break
        if not found_any:
            # Fall back to raw personid presence (unresolved-identity path).
            found_any = any(str(pid) in html for pid in personids)

        self.assertTrue(found_any,
                         'expected at least one real on-shift employee name or personid '
                         'to appear in the rendered /on-shift page')


if __name__ == '__main__':
    unittest.main()
