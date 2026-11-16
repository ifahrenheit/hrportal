"""Break Log access rules (break_log/access.py), no database: rules are injected."""
import time
import unittest

from break_log import access as A


class BreakLogAccessTests(unittest.TestCase):
    def setUp(self):
        self._saved = dict(A._cache)
        A._cache['rules'] = {
            ('GYG', 'CS'):     {'can_log': True,  'can_monitor': False, 'overbreak_review': False},
            ('GYG', 'QA'):     {'can_log': True,  'can_monitor': True,  'overbreak_review': False},
            ('Numa', 'Numa CS'): {'can_log': True, 'can_monitor': False, 'overbreak_review': True},
            ('', ''):          {'can_log': False, 'can_monitor': False, 'overbreak_review': False},
        }
        A._cache['at'] = time.time()

    def tearDown(self):
        A._cache.update(self._saved)

    def test_allowed_by_pair(self):
        self.assertTrue(A.allowed({'account': 'GYG', 'group_name': 'CS'}, 'can_log'))
        self.assertFalse(A.allowed({'account': 'GYG', 'group_name': 'CS'}, 'can_monitor'))
        self.assertTrue(A.allowed({'account': 'GYG', 'group_name': 'QA'}, 'can_monitor'))

    def test_unknown_group_is_off(self):
        self.assertFalse(A.allowed({'account': 'GYG', 'group_name': 'Brand New'}, 'can_log'))

    def test_none_and_blank(self):
        self.assertFalse(A.allowed(None, 'can_log'))
        self.assertFalse(A.allowed({'account': None, 'group_name': None}, 'can_log'))

    def test_whitespace_trimmed(self):
        self.assertTrue(A.allowed({'account': ' GYG ', 'group_name': 'CS '}, 'can_log'))

    def test_pairs_with(self):
        self.assertEqual(A.pairs_with('overbreak_review'), [('Numa', 'Numa CS')])
        self.assertEqual(A.pairs_with('can_monitor'), [('GYG', 'QA')])

    def test_invalidate_forces_reload(self):
        A.invalidate()
        self.assertEqual(A._cache['at'], 0.0)



class ExistingIrMatchTests(unittest.TestCase):
    """Overbreak Review: map IRs already on file (sheet auto IR, manual) to rows."""

    def run_match(self, rows, irs, sheet=()):
        from unittest.mock import MagicMock
        from break_log.routes import _attach_existing_irs
        c = MagicMock()
        c.fetchall.side_effect = [irs, [{'employee_id': e, 'record_date': d} for e, d in sheet]]
        _attach_existing_irs(c, rows)
        return rows

    def ir(self, num, eid, d, summary, by='SYSTEM'):
        return {'report_number': num, 'employee_id': eid, 'incident_date': d, 'status': 'pending',
                'submitted_by_id': by, 'submitted_by_name': 'System', 'summary': summary}

    def test_rule1_ir_maps_to_every_listed_date(self):
        from datetime import date
        summ = ("It was observed ... overbreak\n\nDate Agent Name ...\n"
                "10/24/2026 A 1:32 Valid October 2nd\n10/30/2026 A 1:32 Valid October 2nd\n"
                "10/31/2026 A 1:31 Valid October 2nd\n\n\nAdherence")
        rows = [{'employee_id': 'E1', 'shift_date': date(2026, 10, d)} for d in (18, 24, 30, 31)]
        self.run_match(rows, [self.ir('IR-1', 'E1', date(2026, 10, 31), summ)],
                       sheet=[('E1', date(2026, 10, 24))])
        self.assertEqual([[i['report_number'] for i in r['existing_irs']] for r in rows],
                         [[], ['IR-1'], ['IR-1'], ['IR-1']])
        self.assertTrue(rows[0]['existing_irs'] == [] and rows[1]['in_sheet'] and not rows[0]['in_sheet'])
        self.assertTrue(rows[1]['existing_irs'][0]['auto'])

    def test_same_day_non_break_ir_ignored(self):
        from datetime import date
        d = date(2026, 10, 24)
        rows = [{'employee_id': 'E1', 'shift_date': d}]
        self.run_match(rows, [self.ir('IR-T', 'E1', d, 'Tardiness: 69 minutes this cycle'),
                              self.ir('IR-M', 'E1', d, 'Agent took a 3 hour break', by='X1')])
        self.assertEqual([i['report_number'] for i in rows[0]['existing_irs']], ['IR-M'])
        self.assertFalse(rows[0]['existing_irs'][0]['auto'])

    def test_other_employee_and_empty(self):
        from datetime import date
        d = date(2026, 10, 24)
        rows = [{'employee_id': 'E2', 'shift_date': d}]
        self.run_match(rows, [self.ir('IR-1', 'E1', d, 'overbreak\n10/24/2026 A')])
        self.assertEqual(rows[0]['existing_irs'], [])
        self.assertEqual(self.run_match([], []), [])


if __name__ == '__main__':
    unittest.main()
