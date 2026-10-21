"""Work mode group rule (Numa/SGA/TL = 5-day) and unlimited LWOP when filing.
Fully mocked; importing app needs .env loaded but nothing is written."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as A  # noqa: E402


class FakeCursor:
    def __init__(self, answers):
        self.answers = answers   # list of rows returned by successive fetchone() calls
        self.sql = []

    def execute(self, sql, params=()):
        self.sql.append(' '.join(sql.split()))

    def fetchone(self):
        return self.answers.pop(0) if self.answers else None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeDB:
    def __init__(self, answers):
        self.cur = FakeCursor(answers)

    def cursor(self):
        return self.cur

    def close(self):
        pass


class FiveDayGroupTests(unittest.TestCase):
    def test_groups(self):
        for account, group in [('Numa', 'Numa CS'), ('Numa', 'Numa BO'), ('General', 'SGA'),
                               ('GYG', 'TL'), ('GYG', 'L2 TL'), ('GYG', 'BO TL')]:
            self.assertTrue(A.is_five_day_group(account, group), (account, group))
        for account, group in [('GYG', 'CS'), ('GYG', 'L2'), ('GYG', 'BO'), ('General', 'Finest'),
                               ('Arctic', 'Arctic CS'), (None, None)]:
            self.assertFalse(A.is_five_day_group(account, group), (account, group))


class GetWorkModeTests(unittest.TestCase):
    def mode(self, answers, emp=7):
        with mock.patch.object(A, 'get_db', return_value=FakeDB(list(answers))):
            return A.get_work_mode(emp)

    def test_individual_setting_wins(self):
        # Numa agent explicitly set to 4-day keeps 4-day
        self.assertEqual(self.mode([{'work_mode': '4day'}]), '4day')

    def test_group_rule_for_new_numa_hire(self):
        self.assertEqual(self.mode([None, {'account': 'Numa', 'group_name': 'Numa CS'}]), '5day')

    def test_other_groups_use_global(self):
        self.assertEqual(self.mode([None, {'account': 'GYG', 'group_name': 'CS'},
                                    {'setting_value': '4day'}]), '4day')

    def test_no_employee_uses_global(self):
        with mock.patch.object(A, 'get_db', return_value=FakeDB([{'setting_value': '5day'}])):
            self.assertEqual(A.get_work_mode(), '5day')


class UnlimitedLwopTests(unittest.TestCase):
    LWOP = {'id': 3, 'name': 'Leave Without Pay (LWOP)'}

    def test_added_when_missing(self):
        with mock.patch.object(A, '_get_lwop_type', return_value=self.LWOP):
            out = A.with_unlimited_lwop([{'leave_type_id': 1, 'leave_type_name': 'Vacation Leave',
                                          'remaining_hours': 0.0, 'remaining_days': 0.0}])
        self.assertEqual([b['leave_type_id'] for b in out], [1, 3])
        self.assertTrue(out[1]['unlimited'])
        self.assertNotIn('unlimited', out[0])

    def test_existing_row_marked_unlimited(self):
        with mock.patch.object(A, '_get_lwop_type', return_value=self.LWOP):
            out = A.with_unlimited_lwop([{'leave_type_id': 3, 'leave_type_name': 'Leave Without Pay (LWOP)',
                                          'remaining_hours': 0.0, 'remaining_days': 0.0}])
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0]['unlimited'])


if __name__ == '__main__':
    unittest.main()
