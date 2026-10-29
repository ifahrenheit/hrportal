"""Sheet webhooks moved from the old employee-sync service. Fully mocked: the
blueprint runs on a bare Flask app with a fake DB connection."""

import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask

from modules import sheet_webhooks as sw

KEY = 'test-secret'
HDR = {'X-Secret-Key': KEY}


def fake_conn(fetchall=None, rowcount=0):
    cur = MagicMock()
    cur.fetchall.return_value = fetchall or []
    cur.rowcount = rowcount
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    return conn, cur


class Base(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.register_blueprint(sw.sheet_webhooks_bp)
        self.client = app.test_client()
        self.env = patch.dict(os.environ, {'WEBHOOK_SECRET_KEY': KEY})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def with_db(self, **kw):
        conn, cur = fake_conn(**kw)
        p = patch.object(sw, 'get_db_connection', return_value=conn)
        p.start()
        self.addCleanup(p.stop)
        return conn, cur


class AuthTests(Base):
    def test_missing_or_wrong_key_rejected(self):
        for headers in ({}, {'X-Secret-Key': 'nope'}):
            for path in ('/webhook/employee-sync', '/webhook/schedule-sync', '/webhook/break-autofix'):
                self.assertEqual(self.client.post(path, json={'x': 1}, headers=headers).status_code, 401)

    def test_unset_secret_rejects_everything(self):
        with patch.dict(os.environ, {'WEBHOOK_SECRET_KEY': ''}):
            r = self.client.post('/webhook/employee-sync', json={'x': 1}, headers={'X-Secret-Key': ''})
        self.assertEqual(r.status_code, 401)


class EmployeeSyncTests(Base):
    def test_check_sync_needed(self):
        now = datetime.now()
        self.with_db(fetchall=[{'employee_id': 'A', 'last_synced_at': now},
                               {'employee_id': 'B', 'last_synced_at': now - timedelta(hours=7)},
                               {'employee_id': 'C', 'last_synced_at': None}])
        r = self.client.post('/webhook/employee-sync', headers=HDR,
                             json={'action': 'check_sync_needed', 'employee_ids': ['A', 'B', 'C', 'D']})
        self.assertEqual(r.get_json(), {'status': 'success', 'employees_to_sync': ['B', 'C', 'D']})

    def test_batch_sync_counts_and_values(self):
        conn, cur = self.with_db()
        emp = {'Employee ID': '260101-01', 'Schedule Name': 'Doe, J', 'Email': 'j@x', 'Status': 'Active',
               'WFH': True, 'task_force': '', 'Approver': '', 'Mailing List': ' gygcc3@cohere.ph '}
        r = self.client.post('/webhook/employee-sync', headers=HDR,
                             json={'action': 'batch_sync', 'employees': [emp, None, 'bad']})
        self.assertEqual(r.get_json(), {'status': 'success', 'synced': 1, 'errors': 2})
        values = cur.execute.call_args[0][1]
        self.assertEqual(values[0], '260101-01')
        self.assertEqual(values[12:17], (1, 0, 0, None, 'gygcc3@cohere.ph'))
        conn.commit.assert_called_once()

    def test_single_row(self):
        conn, cur = self.with_db()
        r = self.client.post('/webhook/employee-sync', headers=HDR,
                             json={'row': 5, 'data': {'Employee ID': '260101-01', 'phone': '09'}})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(cur.execute.call_args[0][1][:4], ('260101-01', None, None, '09'))

    def test_single_row_without_data_is_rejected(self):
        conn, cur = self.with_db()
        r = self.client.post('/webhook/employee-sync', headers=HDR, json={'row': 5})
        self.assertEqual(r.status_code, 400)
        cur.execute.assert_not_called()

    def test_db_error_rolls_back(self):
        conn, cur = self.with_db()
        conn.commit.side_effect = RuntimeError('boom')
        r = self.client.post('/webhook/employee-sync', headers=HDR,
                             json={'action': 'batch_sync', 'employees': [{'Employee ID': 'X'}]})
        self.assertEqual(r.status_code, 500)
        conn.rollback.assert_called_once()
        conn.close.assert_called_once()


class ScheduleSyncTests(Base):
    def test_replaces_pairs_and_inserts(self):
        conn, cur = self.with_db(rowcount=1)
        scheds = [{'employee_id': 'A', 'schedule_date': '2026-10-30', 'shift_time': '9pm-6am', 'is_rest_day': False},
                  {'employee_id': 'B', 'schedule_date': None, 'shift_time': 'x', 'is_rest_day': 0}]
        r = self.client.post('/webhook/schedule-sync', headers=HDR, json={'schedules': scheds})
        body = r.get_json()
        self.assertEqual((body['success_count'], body['error_count']), (1, 1))
        cur.executemany.assert_called_once()
        self.assertEqual(cur.executemany.call_args[0][1], [('A', '2026-10-30')])
        self.assertEqual(cur.execute.call_args[0][1], ('A', '2026-10-30', '9pm-6am', False, '2026-10'))


class BreakAutofixTests(Base):
    def test_shift_minutes(self):
        self.assertEqual(sw._shift_minutes('5am-2pm'), (300, 840))
        self.assertEqual(sw._shift_minutes('9pm-6am'), (1260, 360))
        self.assertEqual(sw._shift_minutes('12mn-9am'), (0, 540))
        self.assertIsNone(sw._shift_minutes('VL'))

    def test_only_overnight_breaks_are_moved(self):
        rows = [
            {'id': 1, 'break_start': datetime(2026, 10, 30, 2, 0), 'shift_time': '9pm-6am', 'is_rest_day': 0},
            {'id': 2, 'break_start': datetime(2026, 10, 30, 7, 0), 'shift_time': '5am-2pm', 'is_rest_day': 0},
            {'id': 3, 'break_start': datetime(2026, 10, 30, 3, 0), 'shift_time': None, 'is_rest_day': None},
        ]
        conn, cur = self.with_db(fetchall=rows, rowcount=2)
        r = self.client.post('/webhook/break-autofix', headers=HDR, json={'action': 'auto_fix_breaks'})
        self.assertEqual(r.get_json()['fixed_count'], 2)
        self.assertEqual(cur.execute.call_args[0][1], [1, 3])

    def test_wrong_action(self):
        r = self.client.post('/webhook/break-autofix', headers=HDR, json={'action': 'x'})
        self.assertEqual(r.status_code, 400)


class BiometricTests(Base):
    def test_pending_needs_no_key(self):
        self.with_db(fetchall=[{'id': 1, 'employee_id': 'A'}])
        r = self.client.get('/api/biometric/pending')
        self.assertEqual(r.get_json(), {'records': [{'id': 1, 'employee_id': 'A'}]})

    def test_complete(self):
        conn, cur = self.with_db()
        r = self.client.post('/api/biometric/complete',
                             json={'completed_ids': [1, 2], 'failed': [{'id': 3, 'error': 'offline'}]})
        self.assertEqual(r.get_json(), {'success': True})
        self.assertEqual(cur.execute.call_count, 3)
        conn.commit.assert_called_once()


if __name__ == '__main__':
    unittest.main()
