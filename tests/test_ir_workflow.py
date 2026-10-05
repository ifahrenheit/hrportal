"""IR workflow permissions — fully mocked, no DB."""
import unittest
from unittest import mock

from modules import incident_reports as ir


def _vals(opts):
    return [v for v, _ in opts]


class FakeCursor:
    def __init__(self, results):
        self._results = list(results)
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None): pass
    def fetchone(self): return self._results.pop(0)


class FakeConn:
    def __init__(self, results):
        self._cur = FakeCursor(results)
    def cursor(self): return self._cur


class StatusOptionsTests(unittest.TestCase):
    def test_no_role_gets_nothing(self):
        for status in ['pending', 'rwe_for_signature', 'forwarded']:
            self.assertEqual(ir.status_options(None, status), [])

    def test_admin_gets_everything(self):
        self.assertEqual(len(ir.status_options('admin', 'pending')), len(ir.ALL_ACTIONS))

    def test_tl_steps(self):
        self.assertEqual(_vals(ir.status_options('tl', 'pending')),
                         ['resolved', 'waived', 'rwe_request'])
        self.assertEqual(_vals(ir.status_options('tl', 'rwe_for_signature')), ['rwe_for_service'])
        self.assertEqual(_vals(ir.status_options('tl', 'forwarded')), ['for_memo', 'waived'])
        for waiting in ['rwe_request', 'rwe_for_service', 'awaiting_explanation', 'for_memo', 'reviewed']:
            self.assertEqual(ir.status_options('tl', waiting), [], waiting)

    def test_hr_steps(self):
        self.assertEqual(_vals(ir.status_options('hr', 'rwe_request')), ['rwe_for_signature', 'reviewed'])
        self.assertEqual(_vals(ir.status_options('hr', 'rwe_for_service')), ['awaiting_explanation'])
        self.assertEqual(_vals(ir.status_options('hr', 'awaiting_explanation')), ['forwarded'])
        self.assertEqual(_vals(ir.status_options('hr', 'for_memo')), ['resolved'])
        # HR must not take the TL's decision step
        self.assertNotIn('for_memo', _vals(ir.status_options('hr', 'forwarded')))
        self.assertNotIn('rwe_for_service', _vals(ir.status_options('hr', 'rwe_for_signature')))


class RoleTests(unittest.TestCase):
    def _u(self, **kw):
        base = {'email': 'x@cohere.ph', 'is_admin': False, 'is_hr': False, 'is_sga': False}
        base.update(kw)
        return base

    def test_admin_and_hr(self):
        self.assertEqual(ir.ir_role(self._u(is_admin=True), {'employee_id': '1'}, None), 'admin')
        self.assertEqual(ir.ir_role(self._u(is_hr=True), {'employee_id': '1'}, None), 'hr')
        self.assertEqual(ir.ir_role(self._u(is_sga=True), {'employee_id': '1'}, None), 'hr')

    def test_tl_group_members_are_leads(self):
        for grp in ['TL', 'BO TL', 'L2 TL']:
            conn = FakeConn([{'group_name': grp}])
            self.assertTrue(ir.is_agent_lead('x@cohere.ph', '1', conn), grp)

    def test_qa_is_not_lead_unless_mapped(self):
        conn = FakeConn([{'group_name': 'QA'}, None])
        self.assertFalse(ir.is_agent_lead('qa@cohere.ph', '1', conn))
        conn = FakeConn([{'group_name': 'QA'}, {'1': 1}])  # mapped supervisor / approver
        self.assertTrue(ir.is_agent_lead('qa@cohere.ph', '1', conn))

    def test_l2_group_is_not_tl(self):
        conn = FakeConn([{'group_name': 'L2'}, None])
        self.assertFalse(ir.is_agent_lead('l2@cohere.ph', '1', conn))


if __name__ == '__main__':
    unittest.main()
