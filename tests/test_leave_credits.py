"""helpers/leave_credits.py: leave filed before its year's balance exists.
Fully mocked; no database."""
import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helpers.leave_credits import (apply_pending_credits, charged_items,  # noqa: E402
                                   find_entitlement, mark_credit_pending)


class FakeCursor:
    """Answers SELECTs from canned data and records every UPDATE."""

    def __init__(self, pending=(), l4d=None, ohrm=None):
        self.pending = list(pending)
        self.l4d = l4d or {}      # (emp, type, year) -> row
        self.ohrm = ohrm or {}
        self.updates = []
        self._result = []

    def execute(self, sql, params=()):
        s = ' '.join(sql.split())
        params = list(params)
        if s.startswith('UPDATE'):
            self.updates.append((s, params))
        elif 'FROM leave4day_requests r' in s:
            self._result = self.pending
        elif 'FROM leave4day_entitlements' in s:
            r = self.l4d.get(tuple(params[:3]))
            self._result = [r] if r else []
        elif 'FROM ohrm_leave_entitlement' in s:
            r = self.ohrm.get(tuple(params[:3]))
            self._result = [r] if r else []
        else:
            raise AssertionError('unexpected SQL: ' + s)

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return self._result


def pending_row(id_, d, days, emp=93, lt=3):
    return {'id': id_, 'emp_number': emp, 'leave_type_id': lt, 'leave_date': d,
            'days_deducted': days, 'emp_name': 'ZZTEST Agent', 'leave_type': 'Vacation Leave'}


class FindEntitlementTests(unittest.TestCase):
    def test_uses_ohrm_row_only(self):
        c = FakeCursor(l4d={(93, 3, 2027): {'id': 1, 'no_of_days': 5, 'days_used': 0}},
                       ohrm={(93, 3, 2027): {'id': 9, 'no_of_days': 12, 'days_used': 0}})
        self.assertEqual(find_entitlement(c, 93, 3, 2027), ('ohrm_leave_entitlement', c.ohrm[(93, 3, 2027)]))

    def test_none_when_year_missing(self):
        c = FakeCursor(l4d={(93, 3, 2027): {'id': 1, 'no_of_days': 5, 'days_used': 0}})
        self.assertEqual(find_entitlement(c, 93, 3, 2027), (None, None))


class ChargedItemsTests(unittest.TestCase):
    def test_skips_never_charged_rows(self):
        rows = [{'leave_date': date(2026, 12, 31), 'days_deducted': 1, 'credit_pending': 0},
                {'leave_date': date(2027, 1, 1), 'days_deducted': 1, 'credit_pending': 1},
                {'leave_date': date(2026, 12, 30), 'days_deducted': 1}]  # pre-migration row
        self.assertEqual(charged_items(rows), [(date(2026, 12, 31), 1), (date(2026, 12, 30), 1)])


class MarkPendingTests(unittest.TestCase):
    def test_no_years_no_query(self):
        c = FakeCursor()
        mark_credit_pending(c, 55, [])
        self.assertEqual(c.updates, [])

    def test_flags_only_missing_years(self):
        c = FakeCursor()
        mark_credit_pending(c, 55, [2027])
        sql, params = c.updates[0]
        self.assertIn('SET credit_pending = 1', sql)
        self.assertEqual(params, [55, 2027])


class ApplyPendingTests(unittest.TestCase):
    def test_waits_when_year_has_no_balance(self):
        c = FakeCursor(pending=[pending_row(1, date(2027, 3, 11), 1.25)])
        res = apply_pending_credits(c)
        self.assertFalse(res[0]['applied'])
        self.assertEqual(c.updates, [])

    def test_charges_once_balance_exists_and_clears_flag(self):
        c = FakeCursor(pending=[pending_row(1, date(2027, 3, 11), 1.25),
                                pending_row(2, date(2027, 3, 12), 1.25)],
                       ohrm={(93, 3, 2027): {'id': 9, 'no_of_days': 12, 'days_used': 0}})
        res = apply_pending_credits(c)
        self.assertTrue(res[0]['applied'])
        self.assertEqual(res[0]['days'], 2.5)
        self.assertFalse(res[0]['over'])
        (charge_sql, charge_p), (clear_sql, clear_p) = c.updates
        self.assertIn('UPDATE ohrm_leave_entitlement SET days_used = days_used + %s', charge_sql)
        self.assertEqual(charge_p, [2.5, 9])
        self.assertIn('SET credit_pending = 0', clear_sql)
        self.assertEqual(clear_p, [1, 2])

    def test_dry_run_writes_nothing(self):
        c = FakeCursor(pending=[pending_row(1, date(2027, 3, 11), 1.25)],
                       ohrm={(93, 3, 2027): {'id': 9, 'no_of_days': 12, 'days_used': 0}})
        res = apply_pending_credits(c, live=False)
        self.assertTrue(res[0]['applied'])
        self.assertEqual(c.updates, [])

    def test_flags_over_allowance(self):
        c = FakeCursor(pending=[pending_row(1, date(2027, 1, 4), 4)],
                       ohrm={(93, 3, 2027): {'id': 9, 'no_of_days': 3, 'days_used': 0}})
        self.assertTrue(apply_pending_credits(c)[0]['over'])

    def test_groups_by_year(self):
        c = FakeCursor(pending=[pending_row(1, date(2027, 12, 31), 1), pending_row(2, date(2028, 1, 1), 1)],
                       ohrm={(93, 3, 2027): {'id': 9, 'no_of_days': 12, 'days_used': 0}})
        res = {g['year']: g['applied'] for g in apply_pending_credits(c)}
        self.assertEqual(res, {2027: True, 2028: False})


if __name__ == '__main__':
    unittest.main()
