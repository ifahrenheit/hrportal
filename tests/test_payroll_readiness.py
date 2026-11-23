"""Tests for modules/payroll_readiness.py (Analytics > Payroll Readiness).

Fully mocked: a fake connection answers each query by the table it names.
Run: ./venv/bin/python -m unittest tests.test_payroll_readiness -v
"""
import os
import sys
import unittest
from datetime import date, datetime
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules import payroll_readiness as pr


class FakeCursor:
    def __init__(self, tables, log):
        self.tables, self.log, self.rows = tables, log, []

    def execute(self, sql, params=None):
        self.log.append((sql, params))
        s = sql.lower()
        # longest table name first, so 'fts_requests' doesn't hide in 'fts_pathb_incidents'
        for name in sorted(self.tables, key=len, reverse=True):
            if name in s:
                val = self.tables[name]
                self.rows = val(sql, params) if callable(val) else list(val)
                return
        self.rows = []

    def fetchall(self):
        return list(self.rows)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, tables, log=None):
        self.tables, self.log, self.closed = tables, log if log is not None else [], False

    def cursor(self):
        return FakeCursor(self.tables, self.log)

    def close(self):
        self.closed = True


S, E = date(2026, 11, 8), date(2026, 11, 22)


class PeriodTests(unittest.TestCase):

    def test_default_is_last_closed_period(self):
        self.assertEqual(pr.resolve_period(None, date(2026, 11, 24)), (S, E))
        self.assertEqual(pr.resolve_period('bad', date(2026, 11, 24)), (S, E))

    def test_start_picks_the_period_containing_it(self):
        self.assertEqual(pr.resolve_period('2026-11-30'), (date(2026, 11, 23), date(2026, 12, 7)))
        self.assertEqual(pr.resolve_period('2026-01-05'), (date(2025, 12, 23), date(2026, 1, 7)))

    def test_state_text(self):
        self.assertEqual(pr.period_state(S, E, date(2026, 11, 24)), ('closed', 'Closed Nov 22 (2 days ago)'))
        self.assertEqual(pr.period_state(S, E, date(2026, 11, 22))[1], 'Cutoff is today')
        self.assertEqual(pr.period_state(S, E, date(2026, 11, 20))[1], 'In progress, cutoff Nov 22 (in 2 days)')
        self.assertEqual(pr.period_state(S, E, date(2026, 11, 1))[0], 'upcoming')


class MissingPunchTests(unittest.TestCase):

    def _incident(self, eid, d, flag, shift='8pm-5am'):
        return {'employee_id': eid, 'name': eid, 'tl': 'TL', 'schedule_date': d, 'shift_time': shift,
                'flag_type': flag, 'candidate_note': None, 'created_at': datetime(2026, 11, 12)}

    def test_filed_fts_covers_by_engine_rules(self):
        filed = {('A', 'IN'): {date(2026, 11, 10)}, ('A', 'OUT'): {date(2026, 11, 12)}}
        self.assertTrue(pr._covered(filed, 'A', date(2026, 11, 10), 'IN'))
        self.assertFalse(pr._covered(filed, 'A', date(2026, 11, 9), 'IN'))    # IN must be same day
        self.assertTrue(pr._covered(filed, 'A', date(2026, 11, 11), 'OUT'))   # overnight: next day
        self.assertTrue(pr._covered(filed, 'A', date(2026, 11, 12), 'OUT'))
        self.assertFalse(pr._covered(filed, 'A', date(2026, 11, 10), 'OUT'))

    def test_covered_incidents_are_left_out(self):
        conn = FakeConn({
            'fts_pathb_incidents': [
                self._incident('A', date(2026, 11, 8), 'missing_out'),    # OUT filed on the 9th -> covered
                self._incident('B', date(2026, 11, 10), 'missing_in'),    # nothing filed -> listed
                self._incident('C', date(2026, 11, 11), 'missing_both'),  # only IN filed -> OUT listed
            ],
            'fts_requests': [
                {'employee_id': 'A', 'fts_date': date(2026, 11, 9), 'fts_type': 'OUT'},
                {'employee_id': 'C', 'fts_date': date(2026, 11, 11), 'fts_type': 'IN'},
            ],
        })
        rows = pr.missing_punches(conn, S, E)
        self.assertEqual([r['employee_id'] for r in rows], ['B', 'C'])
        self.assertIn('No IN punch', rows[0]['details'])
        self.assertIn('No OUT punch', rows[1]['details'])
        self.assertEqual(rows[0]['waiting_on'], 'Employee: file an FTS')

    def test_fts_lookup_reaches_one_day_past_period(self):
        log = []
        pr._filed_fts(FakeConn({'fts_requests': []}, log), S, E)
        self.assertEqual(log[0][1], (S, date(2026, 11, 23)))


class RequestAndLeaveTests(unittest.TestCase):

    def test_pending_requests_are_normalised(self):
        conn = FakeConn({
            'ot_requests': [{'id': 1, 'employee_id': 'A', 'd': date(2026, 11, 16), 'ot_type': 'POST',
                             'start_time': '18:00:00', 'end_time': '22:00:00',
                             'filed_at': datetime(2026, 11, 17, 10, 0)}],
            'fts_requests': [{'id': 2, 'employee_id': 'B', 'employee_name': 'Bee', 'd': date(2026, 11, 9),
                              'fts_time': '05:00:00', 'fts_type': 'OUT', 'created_at': datetime(2026, 11, 9)}],
            'cws_requests': [], 'rd_requests': [], 'magic_cws_requests': [],
        })
        rows = pr.pending_requests(conn, S, E)
        self.assertEqual([(r['type'], r['details']) for r in rows],
                         [('OT', 'POST 18:00-22:00'), ('FTS', 'OUT 05:00')])

    def test_time_formatting(self):
        from datetime import timedelta
        self.assertEqual(pr._fmt_time(timedelta(hours=26, minutes=30)), '02:30')   # TIME > 24h
        self.assertEqual(pr._fmt_time(None), '')
        self.assertEqual(pr._range(None, None), '')

    def test_leave_kinds(self):
        rows = [
            {'employee_id': 'A', 'leave_date': date(2026, 11, 10), 'status': 'pending', 'sl_hr_stage': None,
             'credit_pending': 0, 'days_deducted': 1, 'filed_at': datetime(2026, 11, 1),
             'leave_type': 'Vacation Leave', 'name': 'A A'},
            {'employee_id': 'B', 'leave_date': date(2026, 11, 11), 'status': 'approved', 'sl_hr_stage': 'pending_hr',
             'credit_pending': 0, 'days_deducted': 1, 'filed_at': datetime(2026, 11, 1),
             'leave_type': 'Sick Leave', 'name': 'B B'},
            {'employee_id': 'C', 'leave_date': date(2026, 11, 12), 'status': 'scheduled', 'sl_hr_stage': None,
             'credit_pending': 1, 'days_deducted': 0.5, 'filed_at': datetime(2026, 11, 1),
             'leave_type': 'Vacation Leave', 'name': 'C C'},
        ]
        log = []
        out = pr.leave_items(FakeConn({'leave4day_requests': rows}, log), S, E)
        self.assertEqual([r['type'] for r in out],
                         ['Leave pending approval', 'SL waiting for HR', 'Leave not charged yet'])
        self.assertIsNone(out[0]['waiting_on'])          # approver looked up later
        self.assertEqual(out[1]['waiting_on'], 'HR: SL verification')
        self.assertEqual(out[2]['details'], 'Vacation Leave, 0.5 day')
        self.assertEqual(log[0][1], (S, E, 'cancelled', 'rejected', 'deleted'))


class AttachPeopleTests(unittest.TestCase):

    def test_names_tl_approver_and_age(self):
        conn = FakeConn({'gsheet_employees': [{'employee_id': 'A', 'schedule_name': 'Alice', 'tl': 'Ana'}]})
        approvers = {'A': {'name': 'Som One', 'email': 's@x', 'via_tl': False},
                     'B': {'name': 'Som Two', 'email': 't@x', 'via_tl': True}}
        rows = [{'type': 'OT', 'employee_id': 'A', 'date': date(2026, 11, 16), 'details': '',
                 'filed_at': datetime(2026, 11, 17, 10, 0)},
                {'type': 'FTS', 'employee_id': 'B', 'name': 'From FTS', 'date': date(2026, 11, 9), 'details': '',
                 'filed_at': datetime(2026, 11, 9)},
                {'type': 'Missing punch', 'employee_id': 'C', 'name': 'Cee', 'tl': 'Jo', 'date': date(2026, 11, 8),
                 'details': '', 'filed_at': None, 'waiting_on': 'Employee: file an FTS'}]
        with patch.dict(pr._h, {'get_approver_names': lambda ids: {i: approvers[i] for i in ids if i in approvers}}):
            out = pr._attach_people(conn, rows, date(2026, 11, 24))
        by = {r['employee_id']: r for r in out}
        self.assertEqual((by['A']['name'], by['A']['tl'], by['A']['waiting_on'], by['A']['age_days']),
                         ('Alice', 'Ana', 'Som One', 7))
        self.assertEqual(by['B']['name'], 'From FTS')                 # not in sheet: keeps its own name
        self.assertEqual(by['B']['waiting_on'], "Som Two (TL's SOM)")
        self.assertEqual((by['C']['waiting_on'], by['C']['age_days']), ('Employee: file an FTS', None))
        self.assertEqual([r['employee_id'] for r in out], ['C', 'B', 'A'])   # sorted by date


class RouteTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import app as appmod
        cls.app = appmod.app
        cls.app.config['TESTING'] = True

    def _client(self, **sess):
        c = self.app.test_client()
        with c.session_transaction() as s:
            s['user'] = {'email': 'zztest-payroll@cohere.ph', 'name': 'T', 'employee_id': '000000-00',
                         'emp_number': 9999}
            s.update(sess)
        return c

    def _data(self):
        row = {'type': 'OT', 'employee_id': 'A', 'name': 'Alice', 'tl': 'Ana', 'date': date(2026, 11, 16),
               'details': 'POST 18:00-22:00', 'waiting_on': 'Som One', 'filed_at': datetime(2026, 11, 17, 10, 0),
               'age_days': 7}
        return {'sections': [{'key': 'requests', 'title': 'File requests still pending', 'icon': 'x',
                              'rows': [row], 'empty': '', 'hint': ''},
                             {'key': 'punches', 'title': 'Missing punches with no FTS filed', 'icon': 'x',
                              'rows': [], 'empty': 'none here', 'hint': ''},
                             {'key': 'leave', 'title': 'Leave not settled', 'icon': 'x', 'rows': [],
                              'empty': '', 'hint': ''}],
                'total': 1, 'by_type': {'OT': 1}, 'earlier_pending': 2, 'people': 1}

    def test_anonymous_redirects(self):
        self.assertEqual(self.app.test_client().get('/admin/payroll-readiness').status_code, 302)

    def test_sub_admin_without_permission_is_refused(self):
        c = self._client(is_admin=False, permissions={'can_settings': True})
        with patch.object(pr, 'collect') as col:
            self.assertEqual(c.get('/admin/payroll-readiness').status_code, 302)
            self.assertEqual(c.get('/admin/payroll-readiness.csv').status_code, 302)
        col.assert_not_called()

    def test_ot_hours_sub_admin_sees_page(self):
        c = self._client(is_admin=False, permissions={'can_ot_hours': True})
        with patch.object(pr, 'collect', return_value=self._data()):
            r = c.get('/admin/payroll-readiness?start=2026-11-10')
        self.assertEqual(r.status_code, 200)
        html = r.data.decode()
        self.assertIn('Nov 08 – Nov 22, 2026', html)
        self.assertIn('Alice', html)
        self.assertIn('2</b> requests from earlier periods', html)
        self.assertIn('none here', html)

    def test_ready_banner_when_nothing_open(self):
        c = self._client(is_admin=True)
        empty = self._data()
        for s in empty['sections']:
            s['rows'] = []
        empty.update(total=0, earlier_pending=0, people=0)
        with patch.object(pr, 'collect', return_value=empty):
            html = c.get('/admin/payroll-readiness').data.decode()
        self.assertIn("It's ready for payroll", html)

    def test_csv(self):
        c = self._client(is_admin=False, permissions={'can_reports': True})
        with patch.object(pr, 'collect', return_value=self._data()):
            r = c.get('/admin/payroll-readiness.csv?start=2026-11-10')
        self.assertEqual(r.status_code, 200)
        self.assertIn('payroll-readiness_20261108-20261122.csv', r.headers['Content-Disposition'])
        lines = r.data.decode().strip().splitlines()
        self.assertTrue(lines[0].startswith('Section,Type,Employee ID'))
        self.assertIn('Alice', lines[1])
        self.assertIn(',7', lines[1])


if __name__ == '__main__':
    unittest.main()
