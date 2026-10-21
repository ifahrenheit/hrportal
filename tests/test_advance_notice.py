"""VL / Carry-Over advance notice counts 7 working days (policy 3.3.3). Fully mocked."""
import os
import sys
import unittest
from datetime import date, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as A  # noqa: E402


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, sql, params=()):
        pass

    def fetchall(self):
        return self.rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeDB:
    def __init__(self, rows):
        self.rows = rows

    def cursor(self):
        return FakeCursor(self.rows)

    def close(self):
        pass


THU = date(2026, 10, 22)   # a Thursday


def schedule(start, days, rest_weekdays):
    """Schedule rows from start for `days` days, rest on the given weekday numbers (Mon=0)."""
    return [{'schedule_date': start + timedelta(days=i), 'is_rest_day': (start + timedelta(days=i)).weekday() in rest_weekdays}
            for i in range(days)]


class AdvanceNoticeTests(unittest.TestCase):
    def min_date(self, rows, holidays=(), emp=7):
        with mock.patch.object(A, 'get_db', return_value=FakeDB(rows)), \
             mock.patch.object(A, 'is_holiday', side_effect=lambda d: d in holidays):
            return A.advance_notice_min_date(emp, today=THU)

    def test_no_schedule_skips_weekends(self):
        # Fri 23, Mon 26 .. Thu 29, Fri 30, Mon Nov 2 = 7 working days
        self.assertEqual(self.min_date([]), date(2026, 11, 2))

    def test_four_day_week_skips_rest_days(self):
        # works Mon-Thu, rest Fri-Sun: Oct 26-29, Nov 2-4 = 7th working day is Wed Nov 4
        rows = schedule(THU + timedelta(days=1), 30, {4, 5, 6})
        self.assertEqual(self.min_date(rows), date(2026, 11, 4))

    def test_holiday_is_not_a_working_day(self):
        # Nov 2 is a holiday, so the 7th working day moves to Nov 3
        self.assertEqual(self.min_date([], holidays={date(2026, 11, 2)}), date(2026, 11, 3))

    def test_check_only_vl_and_carry_over(self):
        with mock.patch.object(A, 'advance_notice_min_date', return_value=date(2026, 11, 2)):
            self.assertEqual(A.check_advance_notice('Vacation Leave', [date(2026, 10, 30), date(2026, 11, 2)], 7),
                             [date(2026, 10, 30)])
            self.assertEqual(A.check_advance_notice('Sick Leave', [date(2026, 10, 23)], 7), [])


if __name__ == '__main__':
    unittest.main()
