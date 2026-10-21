"""VL / Carry-Over advance notice: 7 calendar days (HR's choice over policy 3.3.3's working days)."""
import os
import sys
import unittest
from datetime import date
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as A  # noqa: E402

THU = date(2026, 10, 22)


class AdvanceNoticeTests(unittest.TestCase):
    def test_seven_calendar_days(self):
        # weekends and holidays (Nov 1-2) don't matter
        self.assertEqual(A.advance_notice_min_date(7, today=THU), date(2026, 10, 29))

    def test_check_only_vl_and_carry_over(self):
        with mock.patch.object(A, 'advance_notice_min_date', return_value=date(2026, 10, 29)):
            self.assertEqual(A.check_advance_notice('Vacation Leave', [date(2026, 10, 28), date(2026, 10, 29)], 7),
                             [date(2026, 10, 28)])
            self.assertEqual(A.check_advance_notice('Carry-Over Leave', [date(2026, 10, 24)], 7), [date(2026, 10, 24)])
            self.assertEqual(A.check_advance_notice('Sick Leave', [date(2026, 10, 23)], 7), [])


if __name__ == '__main__':
    unittest.main()
