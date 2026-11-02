"""Balance check per leave year (helpers/leave_credits). Fully mocked: a fake
cursor answers the entitlement and used-hours queries."""

import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helpers import leave_credits as lc


class FakeCursor:
    """entitlements: {year: no_of_days}; used: {year: hours}."""

    def __init__(self, entitlements, used):
        self.entitlements, self.used, self._row = entitlements, used, None

    def execute(self, sql, params):
        if 'ohrm_leave_entitlement' in sql:
            year = params[2]
            days = self.entitlements.get(year)
            self._row = {'id': year, 'no_of_days': days, 'days_used': 0} if days is not None else None
        else:
            self._row = {'used': self.used.get(params[2], 0)}

    def fetchone(self):
        return self._row


def ded(y, m, d, hours=8.0):
    return {'date': date(y, m, d), 'hours': hours, 'days': hours / 8, 'is_off_day': hours == 0}


class AvailableByYearTests(unittest.TestCase):
    def test_loaded_year_uses_its_own_balance(self):
        c = FakeCursor({2026: 12, 2027: 12}, {2026: 90, 2027: 16})
        a = lc.available_by_year(c, 1, 1, [2026, 2027], 2026)
        self.assertEqual(a[2026], {'available': 6.0, 'loaded': True})
        self.assertEqual(a[2027], {'available': 80.0, 'loaded': True})

    def test_unloaded_year_estimates_from_current_allowance(self):
        c = FakeCursor({2026: 12}, {2026: 96, 2027: 24})
        a = lc.available_by_year(c, 1, 1, [2027], 2026)
        self.assertEqual(a[2027], {'available': 72.0, 'loaded': False})

    def test_nothing_loaded_means_zero(self):
        c = FakeCursor({}, {})
        self.assertEqual(lc.available_by_year(c, 1, 1, [2027], 2026)[2027]['available'], 0.0)

    def test_never_negative(self):
        c = FakeCursor({2026: 1}, {2026: 40})
        self.assertEqual(lc.available_by_year(c, 1, 1, [2026], 2026)[2026]['available'], 0.0)


class ShortfallTests(unittest.TestCase):
    def test_next_year_filing_ok_even_when_this_year_is_used_up(self):
        avail = {2027: {'available': 96.0, 'loaded': True}}
        self.assertEqual(lc.year_shortfalls([ded(2027, 3, 1), ded(2027, 3, 2)], avail), [])

    def test_split_across_new_year(self):
        avail = {2026: {'available': 8.0, 'loaded': True}, 2027: {'available': 96.0, 'loaded': True}}
        deds = [ded(2026, 12, 30), ded(2026, 12, 31), ded(2027, 1, 1)]
        self.assertEqual(lc.year_shortfalls(deds, avail), [(2026, 8.0, 16.0, True)])

    def test_exact_balance_is_enough(self):
        avail = {2026: {'available': 16.0, 'loaded': True}}
        self.assertEqual(lc.year_shortfalls([ded(2026, 5, 4), ded(2026, 5, 5)], avail), [])

    def test_off_days_do_not_count(self):
        avail = {2026: {'available': 8.0, 'loaded': True}}
        self.assertEqual(lc.year_shortfalls([ded(2026, 5, 2, 0), ded(2026, 5, 4)], avail), [])

    def test_message_names_year_and_estimate(self):
        msg = lc.shortfall_message('Vacation Leave', [(2027, 4.0, 16.0, False)])
        self.assertIn('2027 (not loaded yet', msg)
        self.assertIn('available 4.0 hrs, required 16.0 hrs', msg)


if __name__ == '__main__':
    unittest.main()
