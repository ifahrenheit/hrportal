"""
Tests for modules/file_for_employee.py (/admin/file-request).

Run with:
    ./venv/bin/python -m unittest tests.test_file_for_employee -v
from the repo root with .env loaded.

* POST tests are fully mocked: the blueprint's injected helpers (_h) are
  swapped for fake DB connections that route results by table name and
  record every INSERT, so nothing is written to the real databases and no
  email is sent.
* RenderTests are read-only GETs against the real databases (employee
  list, PIM request history), the way pim_profile() is checked by hand.
"""
import sys
import unittest
from datetime import date
from unittest.mock import MagicMock

sys.path.insert(0, '/var/www/html/leavesystem')

import app as app_module  # noqa: E402
import modules.file_for_employee as ffe  # noqa: E402

ACTIVE = {'employee_id': 'ZZTEST-01', 'schedule_name': 'Zz Tester', 'status': 'Active',
          'exit_date': None, 'email': 'zz.tester@example.invalid', 'approver': None,
          'tl': 'TL', 'group_name': 'CS'}
SEPARATED = dict(ACTIVE, employee_id='ZZTEST-02', schedule_name='Zz Gone', status='Separated',
                 exit_date=date(2026, 9, 30))


class FakeCursor:
    def __init__(self, db):
        self.db = db
        self._rows = []
        self.lastrowid = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def close(self):
        pass

    def execute(self, sql, params=None):
        flat = ' '.join(sql.split())
        self.db.queries.append((flat, params))
        if flat.startswith('INSERT'):
            self.db.next_id += 1
            self.lastrowid = self.db.next_id
            self.db.inserts.append((flat, params))
            self._rows = []
        elif 'FROM gsheet_employees WHERE employee_id' in flat:
            self._rows = [e for e in self.db.employees if e['employee_id'] == params[0]]
        elif 'FROM gsheet_employees' in flat:
            self._rows = list(self.db.employees)
        elif 'FROM hs_hr_employee' in flat:
            self._rows = [{'emp_number': 999}]
        elif self.db.duplicate and ('SELECT id' in flat):
            self._rows = [{'id': 77, 'status': 'Pending'}]
        else:
            self._rows = []

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeConn:
    def __init__(self, db):
        self.db = db

    def cursor(self, *a, **k):
        return FakeCursor(self.db)

    def commit(self):
        self.db.commits += 1

    def rollback(self):
        self.db.rollbacks += 1

    def close(self):
        pass


class FakeDB:
    def __init__(self, employees, duplicate=False):
        self.employees = employees
        self.duplicate = duplicate
        self.queries, self.inserts = [], []
        self.next_id = 1000
        self.commits = self.rollbacks = 0


class PostTests(unittest.TestCase):
    def setUp(self):
        self._saved = dict(ffe._h)
        self.db = FakeDB([ACTIVE, SEPARATED])
        self.send_email = MagicMock()
        self.sup_email = MagicMock(return_value='sup@example.invalid')
        ffe._h.update(get_central_db=lambda: FakeConn(self.db), get_db=lambda: FakeConn(self.db),
                      send_email=self.send_email, get_supervisor_email=self.sup_email,
                      fetch_requests=lambda *a: [])
        self.client = app_module.app.test_client()
        self.login(admin=True)

    def tearDown(self):
        ffe._h.clear()
        ffe._h.update(self._saved)

    def login(self, admin):
        with self.client.session_transaction() as s:
            s['user'] = {'emp_number': 1, 'email': 'filer@example.invalid', 'name': 'Filer Person'}
            s['is_admin'] = admin
            s['permissions'] = {} if admin else {'can_file_for_emp': True}
            s['_csrf_token'] = 'tok'

    def post(self, data):
        data = dict(data, csrf_token='tok')
        return self.client.post('/admin/file-request', data=data)

    def request_inserts(self):
        return [i for i in self.db.inserts if 'employee_audit_log' not in i[0]]

    def audit_inserts(self):
        return [i for i in self.db.inserts if 'employee_audit_log' in i[0]]

    # ── FTS ──
    def test_fts_pending(self):
        r = self.post({'employee_id': 'ZZTEST-01', 'type': 'fts', 'fts_date': '2026-09-29',
                       'fts_time': '21:00', 'fts_type': 'OUT'})
        self.assertEqual(r.status_code, 302)
        (sql, params), = self.request_inserts()
        self.assertIn('INSERT INTO fts_requests', sql)
        self.assertEqual(params[:5], ('ZZTEST-01', 'Zz Tester', date(2026, 9, 29), '21:00', 'OUT'))
        self.assertEqual(params[5:], ('Pending', None, None))
        self.assertEqual(len(self.audit_inserts()), 1)
        self.assertIn('[Pending]', self.audit_inserts()[0][1][1])
        self.assertEqual(self.db.commits, 1)
        # employee notice + approver notice
        self.assertEqual(self.send_email.call_count, 2)
        self.assertEqual(self.send_email.call_args_list[1][0][0], 'sup@example.invalid')

    def test_fts_approved_by_admin(self):
        self.post({'employee_id': 'ZZTEST-02', 'type': 'fts', 'fts_date': '2026-09-29',
                   'fts_time': '21:00', 'fts_type': 'OUT', 'file_approved': '1'})
        (sql, params), = self.request_inserts()
        self.assertEqual(params[5], 'Approved')
        self.assertEqual(params[6], 'Filer Person')
        self.assertIsNotNone(params[7])
        # Separated + approved: no employee email, no approver email
        self.send_email.assert_not_called()

    def test_non_admin_cannot_file_approved(self):
        self.login(admin=False)
        self.post({'employee_id': 'ZZTEST-01', 'type': 'fts', 'fts_date': '2026-09-29',
                   'fts_time': '21:00', 'fts_type': 'OUT', 'file_approved': '1'})
        (sql, params), = self.request_inserts()
        self.assertEqual(params[5], 'Pending')

    def test_no_permission_redirects(self):
        with self.client.session_transaction() as s:
            s['is_admin'] = False
            s['permissions'] = {}
        r = self.post({'employee_id': 'ZZTEST-01', 'type': 'fts', 'fts_date': '2026-09-29',
                       'fts_time': '21:00', 'fts_type': 'OUT'})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.db.inserts, [])

    def test_bad_csrf_rejected(self):
        r = self.client.post('/admin/file-request', data={
            'employee_id': 'ZZTEST-01', 'type': 'fts', 'fts_date': '2026-09-29',
            'fts_time': '21:00', 'fts_type': 'OUT', 'csrf_token': 'wrong'})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.db.inserts, [])

    def test_after_exit_date_blocked(self):
        r = self.post({'employee_id': 'ZZTEST-02', 'type': 'fts', 'fts_date': '2026-10-01',
                       'fts_time': '21:00', 'fts_type': 'OUT'})
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'separated on Sep 30, 2026', r.data)
        self.assertEqual(self.db.inserts, [])

    def test_fts_duplicate_blocked(self):
        self.db.duplicate = True
        r = self.post({'employee_id': 'ZZTEST-01', 'type': 'fts', 'fts_date': '2026-09-29',
                       'fts_time': '21:00', 'fts_type': 'OUT'})
        self.assertIn(b'already exists (request #77)', r.data)
        self.assertEqual(self.db.inserts, [])
        self.assertEqual(self.db.commits, 0)

    def test_fts_invalid_type(self):
        r = self.post({'employee_id': 'ZZTEST-01', 'type': 'fts', 'fts_date': '2026-09-29',
                       'fts_time': '21:00', 'fts_type': 'SIDEWAYS'})
        self.assertIn(b'FTS type must be IN or OUT', r.data)
        self.assertEqual(self.db.inserts, [])

    def test_unknown_employee(self):
        r = self.post({'employee_id': 'NOPE', 'type': 'fts'})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.db.inserts, [])

    # ── CWS ──
    def test_cws_pending(self):
        self.post({'employee_id': 'ZZTEST-01', 'type': 'cws', 'original_date': '2026-09-29',
                   'original_time': '10am-9pm', 'new_date': '2026-09-30', 'new_time': '10am-9pm',
                   'reason': 'swap'})
        (sql, params), = self.request_inserts()
        self.assertIn('INSERT INTO cws_requests', sql)
        self.assertEqual(params[6:], ('Pending', None, None))

    def test_cws_missing_reason(self):
        r = self.post({'employee_id': 'ZZTEST-01', 'type': 'cws', 'original_date': '2026-09-29',
                       'original_time': '10am-9pm', 'new_date': '2026-09-30', 'new_time': '10am-9pm'})
        self.assertIn(b'All CWS fields are required', r.data)
        self.assertEqual(self.db.inserts, [])

    # ── OT / RDW ──
    def test_ot_pre_requires_tickets(self):
        r = self.post({'employee_id': 'ZZTEST-01', 'type': 'ot', 'work_day_type': 'REGULAR',
                       'ot_date': '2026-09-29', 'start_time': '08:00:00', 'end_time': '10:00:00',
                       'ot_type': 'PRE', 'regular_rate': 'No'})
        self.assertIn(b'Ticket numbers are required', r.data)
        self.assertEqual(self.db.inserts, [])

    def test_ot_with_tickets(self):
        self.post({'employee_id': 'ZZTEST-01', 'type': 'ot', 'work_day_type': 'REGULAR',
                   'ot_date': '2026-09-29', 'start_time': '08:00:00', 'end_time': '10:00:00',
                   'ot_type': 'PRE', 'regular_rate': 'No', 'ticket_numbers': 'ABC12345, 778899',
                   'file_approved': '1'})
        reqs = self.request_inserts()
        self.assertIn('INSERT INTO ot_requests', reqs[0][0])
        p = reqs[0][1]
        self.assertEqual(p[6], 'Approved')      # status
        self.assertEqual(p[7], 1)               # tickets_submitted
        self.assertEqual(p[8], 'Filer Person')  # approver_name
        tickets = [r for r in reqs if 'ot_tickets' in r[0]]
        self.assertEqual(len(tickets), 2)
        self.assertIn('ot_request_id', tickets[0][0])
        self.assertEqual(tickets[0][1][5], 1.0)  # 2h split over 2 tickets

    def test_rdw_overhead_no_tickets(self):
        self.post({'employee_id': 'ZZTEST-01', 'type': 'ot', 'work_day_type': 'REST_DAY',
                   'ot_date': '2026-09-28', 'start_time': '22:00:00', 'end_time': '02:00:00',
                   'work_category': 'OVERHEAD_OT', 'took_break': 'N'})
        reqs = self.request_inserts()
        self.assertEqual(len(reqs), 1)
        self.assertIn('INSERT INTO rd_requests', reqs[0][0])
        self.assertEqual(reqs[0][1][6:], ('Pending', None, None))

    def test_rdw_requires_break_answer(self):
        r = self.post({'employee_id': 'ZZTEST-01', 'type': 'ot', 'work_day_type': 'REST_DAY',
                       'ot_date': '2026-09-28', 'start_time': '08:00:00', 'end_time': '10:00:00',
                       'work_category': 'OVERHEAD_OT'})
        self.assertIn(b'1-hour break', r.data)
        self.assertEqual(self.db.inserts, [])

    def test_ot_rejects_unlisted_time(self):
        r = self.post({'employee_id': 'ZZTEST-01', 'type': 'ot', 'work_day_type': 'REGULAR',
                       'ot_date': '2026-09-29', 'start_time': '08:15:00', 'end_time': '10:00:00',
                       'ot_type': 'DAILY_OT', 'regular_rate': 'No'})
        self.assertIn(b'Start and end time are required', r.data)
        self.assertEqual(self.db.inserts, [])


class RenderTests(unittest.TestCase):
    """Read-only GETs against the real databases."""

    def setUp(self):
        self.client = app_module.app.test_client()
        with self.client.session_transaction() as s:
            s['user'] = {'emp_number': 0, 'email': 'test@local', 'name': 'Test'}
            s['is_admin'] = True
            s['permissions'] = {}

    def test_picker_lists_separated(self):
        r = self.client.get('/admin/file-request')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'260618-12', r.data)          # Lornelyn Sancover, Separated
        self.assertIn(b'(Separated', r.data)

    def test_each_tab_renders(self):
        for t, marker in [('fts', b'name="fts_type"'), ('cws', b'name="original_time"'),
                          ('ot', b'name="work_day_type"')]:
            r = self.client.get(f'/admin/file-request?employee_id=260618-12&type={t}')
            self.assertEqual(r.status_code, 200, t)
            self.assertIn(marker, r.data)
            self.assertIn(b'max="2026-09-30"', r.data)   # exit date caps the date picker
            self.assertIn(b'File as approved', r.data)
        self.assertIn(b'No approver is on file', r.data)

    def test_sub_admin_has_no_approve_box(self):
        with self.client.session_transaction() as s:
            s['is_admin'] = False
            s['permissions'] = {'can_file_for_emp': True}
        r = self.client.get('/admin/file-request?employee_id=260618-12&type=fts')
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(b'File as approved', r.data)


if __name__ == '__main__':
    unittest.main()
