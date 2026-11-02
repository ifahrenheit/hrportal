"""Leave filed for an employee goes to their approver like a self-filed one.
Mocked: no email is sent and no database is touched."""

import os
import sys
import unittest
from datetime import date
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as portal  # noqa: E402


def fake_cdb(row):
    cur = MagicMock()
    cur.fetchone.return_value = row
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    return conn


DEDS = [{'date': date(2026, 11, 20), 'hours': 8.0, 'days': 1.0, 'is_off_day': False}]


class NotifyTests(unittest.TestCase):
    def run_notify(self, leave_type_id, emp_row, sup='tl@cohere.ph', verifiers=('hr@cohere.ph',)):
        with portal.app.test_request_context(), \
             patch.object(portal, 'get_central_db', return_value=fake_cdb(emp_row)), \
             patch.object(portal, 'get_supervisor_email', return_value=sup), \
             patch.object(portal, 'get_sl_verifier_emails', return_value=list(verifiers)), \
             patch.object(portal, 'send_email') as send:
            portal.session['user'] = {'name': 'HR <Admin>'}
            portal._notify_leave_filed_for_employee(7, '260101-01', leave_type_id, 'Vacation Leave', DEDS, 8.0)
        return [c.args[0] for c in send.call_args_list], send

    def test_vacation_leave_reaches_employee_and_approver(self):
        to, send = self.run_notify(1, {'schedule_name': 'Doe, Jane', 'email': 'jane@cohere.ph'})
        self.assertEqual(to, ['jane@cohere.ph', 'tl@cohere.ph'])
        self.assertIn('awaiting your approval', send.call_args_list[1].args[2])
        self.assertIn('HR &lt;Admin&gt;', send.call_args_list[1].args[2])  # filer name escaped

    def test_sick_leave_also_reaches_hr_verifiers(self):
        to, _ = self.run_notify(2, {'schedule_name': 'Doe, Jane', 'email': 'jane@cohere.ph'})
        self.assertEqual(to, ['jane@cohere.ph', 'tl@cohere.ph', 'hr@cohere.ph'])

    def test_no_email_or_approver_on_file(self):
        to, _ = self.run_notify(1, {'schedule_name': 'Doe, Jane', 'email': None}, sup=None)
        self.assertEqual(to, [])


if __name__ == '__main__':
    unittest.main()
