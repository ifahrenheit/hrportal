"""Tests for modules/leave_recalc.plan_changes (pure; no database)."""
import unittest
from datetime import date

from modules.leave_recalc import plan_changes

TUE, WED, SAT = date(2026, 11, 3), date(2026, 11, 4), date(2026, 11, 7)


def row(d, hours, days, off=0, shift=None, type_id=6, pending=0):
    return {'id': d.day, 'leave_date': d, 'hours_deducted': hours, 'days_deducted': days,
            'is_off_day': off, 'shift_half': shift, 'leave_type_id': type_id,
            'credit_pending': pending, 'leave_request_id': 1, 'leave_type_name': 'X'}


def five_day(rest_days=()):
    """calculate_deduction stand-in for a 5-day employee."""
    def calc(d, duration):
        if d in rest_days:
            return []
        hrs = 4 if duration in ('first_half', 'second_half') else 8
        return [{'hours': hrs, 'days': hrs / 8, 'is_off_day': False}]
    return calc


def four_day(rest_days=()):
    def calc(d, duration):
        if d in rest_days:
            return [{'hours': 0, 'days': 0.0, 'is_off_day': True}]
        hrs = 5 if duration in ('first_half', 'second_half') else 10
        return [{'hours': hrs, 'days': hrs / 8, 'is_off_day': False}]
    return calc


no_holiday = lambda d: False


class PlanChangesTests(unittest.TestCase):
    def test_four_to_five_day_full_and_off_day(self):
        # The Cogonon case: filed on 4-day, Tue was a rest day, now 5-day and Tue is working.
        changes, skipped = plan_changes([row(TUE, 0, 0, off=1), row(WED, 10, 1.25)],
                                        five_day(), no_holiday)
        self.assertEqual(skipped, [])
        by_date = {c['leave_date']: c for c in changes}
        self.assertEqual((by_date[TUE]['new_hours'], by_date[TUE]['new_days'],
                          by_date[TUE]['new_is_off'], by_date[TUE]['delta_days']), (8, 1.0, 0, 1.0))
        self.assertEqual((by_date[WED]['new_hours'], by_date[WED]['delta_days']), (8, -0.25))

    def test_unchanged_rows_are_not_listed(self):
        changes, skipped = plan_changes([row(WED, 8, 1.0)], five_day(), no_holiday)
        self.assertEqual((changes, skipped), ([], []))

    def test_five_day_rest_day_becomes_zero(self):
        changes, _ = plan_changes([row(SAT, 8, 1.0)], five_day(rest_days={SAT}), no_holiday)
        self.assertEqual((changes[0]['new_hours'], changes[0]['new_is_off'],
                          changes[0]['delta_days']), (0.0, 1, -1.0))

    def test_five_to_four_day_half_shift(self):
        changes, _ = plan_changes([row(WED, 4, 0.5, shift='1st Half')], four_day(), no_holiday)
        self.assertEqual((changes[0]['new_hours'], changes[0]['new_days']), (5, 0.625))

    def test_skips_holiday_custom_and_awol(self):
        rows = [row(TUE, 10, 1.25), row(WED, 3, 0.375), row(SAT, 8, 1.0, type_id=99)]
        changes, skipped = plan_changes(rows, five_day(), lambda d: d == TUE, awol_type_id=99)
        self.assertEqual(changes, [])
        self.assertEqual([s['reason'].split(' ')[0] for s in skipped], ['Holiday', 'Custom', 'AWOL'])


if __name__ == '__main__':
    unittest.main()
