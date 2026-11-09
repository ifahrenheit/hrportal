"""Leave filing hold (modules/leave_hold.py) -- pure logic, no database."""
import json
import unittest

from modules.leave_hold import (parse_config, held_type_ids_for_group, DEFAULT_MESSAGE)

TYPES = [{'id': 1, 'name': 'Vacation Leave'}, {'id': 2, 'name': 'Sick Leave'},
         {'id': 3, 'name': 'Leave Without Pay (LWOP)'}, {'id': 4, 'name': 'Carry-Over Leave'}]


def cfg(enabled='1', groups=('CS', 'BO', 'L2'), types=(1, 2, 4), **extra):
    rows = {'leave_hold_enabled': enabled, 'leave_hold_groups': json.dumps(list(groups)),
            'leave_hold_types': json.dumps(list(types))}
    rows.update(extra)
    return parse_config(rows, TYPES)


class LeaveHoldTests(unittest.TestCase):
    def test_held_group_gets_held_types(self):
        self.assertEqual(held_type_ids_for_group(cfg(), 'CS'), {1, 2, 4})

    def test_group_match_ignores_case_and_spaces(self):
        self.assertEqual(held_type_ids_for_group(cfg(), '  cs '), {1, 2, 4})

    def test_exempt_groups_not_held(self):
        for g in ('Numa CS', 'Numa BO', 'SGA', 'Finest'):
            self.assertEqual(held_type_ids_for_group(cfg(), g), set(), g)

    def test_off_holds_nobody(self):
        self.assertEqual(held_type_ids_for_group(cfg(enabled='0'), 'CS'), set())

    def test_no_group_not_held(self):
        self.assertEqual(held_type_ids_for_group(cfg(), None), set())
        self.assertEqual(held_type_ids_for_group(cfg(), ''), set())

    def test_default_types_when_never_saved(self):
        c = parse_config({'leave_hold_enabled': '1'}, TYPES)
        self.assertEqual(sorted(c['type_ids']), [1, 2, 4])
        self.assertEqual(c['message'], DEFAULT_MESSAGE)
        self.assertEqual(c['groups'], [])

    def test_bad_json_is_empty(self):
        c = parse_config({'leave_hold_enabled': '1', 'leave_hold_groups': 'not json',
                          'leave_hold_types': '{"a":1}'}, TYPES)
        self.assertEqual(c['groups'], [])
        self.assertEqual(c['type_ids'], [])


if __name__ == '__main__':
    unittest.main()
