"""helpers/leave_policy.py: VL/SL start dates and pro-rating (policy 3.2 / 3.3). Pure functions."""
import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helpers.leave_policy import add_months, vl_start, vl_deployment, sl_start, credit_date, prorated_days  # noqa: E402


class LeavePolicyTests(unittest.TestCase):
    def test_starts(self):
        self.assertEqual(vl_start(date(2025, 3, 24)), date(2026, 3, 24))
        self.assertEqual(sl_start(date(2025, 9, 15)), date(2026, 3, 15))
        self.assertEqual(sl_start(date(2025, 9, 15), date(2026, 4, 2)), date(2026, 4, 2))
        self.assertEqual(add_months(date(2025, 8, 31), 6), date(2026, 2, 28))

    def test_numa_move_restarts_vl(self):
        self.assertEqual(vl_deployment(date(2025, 6, 9), date(2025, 7, 8)), date(2025, 7, 8))
        self.assertEqual(prorated_days(vl_start(vl_deployment(date(2025, 6, 9), date(2025, 7, 8)))), 6)
        self.assertEqual(vl_deployment(date(2025, 6, 9), None), date(2025, 6, 9))
        self.assertEqual(vl_deployment(date(2025, 6, 9), date(2025, 1, 1)), date(2025, 6, 9))  # earlier move ignored

    def test_cutoff_15th(self):
        self.assertEqual(credit_date(date(2026, 3, 15)), date(2026, 3, 15))
        self.assertEqual(credit_date(date(2026, 3, 16)), date(2026, 4, 1))
        self.assertEqual(credit_date(date(2026, 12, 20)), date(2027, 1, 1))

    def test_prorated_matches_2026_grants(self):
        # deployment anniversaries seen in 2026 data
        self.assertEqual(prorated_days(date(2026, 2, 10)), 11)
        self.assertEqual(prorated_days(date(2026, 3, 3)), 10)
        self.assertEqual(prorated_days(date(2026, 3, 24)), 9)
        self.assertEqual(prorated_days(date(2026, 9, 15)), 4)
        self.assertEqual(prorated_days(date(2026, 10, 27)), 2)
        self.assertEqual(prorated_days(date(2026, 1, 5)), 12)
        self.assertEqual(prorated_days(date(2026, 12, 15)), 1)
        self.assertEqual(prorated_days(date(2026, 12, 16)), 0)   # credited Jan 1 as a full year instead


if __name__ == '__main__':
    unittest.main()
