"""Tests for modules/pim_360.py (PIM profile "Employee 360" tabs).

Mocked: a fake connection answers by table name, and the feature access helpers
are patched. Run: ./venv/bin/python -m unittest tests.test_pim_360 -v
"""
import os
import sys
import unittest
from contextlib import ExitStack
from datetime import date, datetime
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as appmod
from modules import pim_360 as p3


class FakeCursor:
    def __init__(self, tables, log):
        self.tables, self.log, self.rows = tables, log, []

    def execute(self, sql, params=None):
        self.log.append((sql, params))
        s = sql.lower()
        for name in sorted(self.tables, key=len, reverse=True):
            if name in s:
                self.rows = list(self.tables[name])
                return
        self.rows = []

    def fetchall(self):
        return list(self.rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, tables):
        self.tables, self.log, self.closed = tables, [], False

    def cursor(self, *a):
        return FakeCursor(self.tables, self.log)

    def close(self):
        self.closed = True


class DataTests(unittest.TestCase):

    def test_irs_use_the_viewers_scope_and_label_status(self):
        conn = FakeConn({'incident_reports': [
            {'report_number': 'IR-1', 'incident_date': date(2026, 11, 3), 'summary': 'Late', 'status': 'waived',
             'memo_category': None, 'disciplinary_action': None, 'submitted_by_name': 'TL', 'created_at': None},
            {'report_number': 'IR-2', 'incident_date': date(2026, 10, 3), 'summary': 'X', 'status': 'odd_new_state',
             'memo_category': None, 'disciplinary_action': None, 'submitted_by_name': 'TL', 'created_at': None}]})
        rows = p3.incident_reports_for(conn, 'E1', ['1=1', 'ir.submitted_by_id = %s'], ['ME'])
        sql, params = conn.log[0]
        self.assertIn('ir.employee_id = %s AND 1=1 AND ir.submitted_by_id = %s', ' '.join(sql.split()))
        self.assertEqual(params, ['E1', 'ME'])
        self.assertEqual((rows[0]['status_label'], rows[0]['status_tone']), ('Waived', 'muted'))
        self.assertEqual(rows[1]['status_label'], 'Odd New State')     # unknown status still readable

    def test_undertime_periods(self):
        conn = FakeConn({'undertime_cycle_state': [
            {'period_start': date(2026, 11, 8), 'total_count_in_cycle': 4, 'total_minutes_in_cycle': 120,
             'count_triggers_sent': 1, 'minutes_triggers_sent': 1,
             'count_breakdown': '[{"date": "2026-11-20", "minutes": 30}]',
             'minutes_breakdown': '[{"date": "2026-11-19"}, {"date": "2026-11-20"}]'},
            {'period_start': date(2026, 10, 23), 'total_count_in_cycle': 1, 'total_minutes_in_cycle': 5,
             'count_triggers_sent': 0, 'minutes_triggers_sent': 0, 'count_breakdown': 'not json',
             'minutes_breakdown': None}]})
        out = p3.undertime_periods_for(conn, 'E1')
        self.assertEqual((out[0]['end'], out[0]['memos'], out[0]['recent_days']),
                         (date(2026, 11, 22), 2, ['2026-11-19', '2026-11-20']))
        self.assertEqual((out[1]['end'], out[1]['recent_days']), (date(2026, 11, 7), []))

    def test_documents_only_at_visible_levels(self):
        conn = FakeConn({'hr_documents': []})
        p3.documents_for(conn, 'E1', ('everyone',))
        sql, params = conn.log[0]
        self.assertIn('visibility IN (%s)', sql)
        self.assertIn('deleted_at IS NULL', sql)
        self.assertEqual(params, ['E1', 'everyone'])

    def test_coaching_scope_is_appended(self):
        conn = FakeConn({'coaching_sessions': [
            {'id': 5, 'session_date': date(2026, 11, 1), 'coaching_type': 'performance', 'topic': 'AHT',
             'status': 'for_followup', 'follow_up_date': None, 'supervisor_id': 'TL1', 'coach': 'Tee'}]})
        rows = p3.coaching_for(conn, 'E1', ' AND cs.supervisor_id = %s ', ['ME'])
        sql, params = conn.log[0]
        self.assertTrue(' '.join(sql.split()).endswith('AND cs.supervisor_id = %s ORDER BY cs.session_date DESC, cs.id DESC'))
        self.assertEqual(params, ['E1', 'ME'])
        self.assertEqual(rows[0]['status_label'], 'For follow-up')


class TabAccessTests(unittest.TestCase):

    def _tabs(self, sess, coaching=False, docs=False, coe=False):
        with appmod.app.test_request_context():
            from flask import session
            session.update(sess)
            with patch.object(p3, '_can_coaching', return_value=coaching), \
                 patch.object(p3, '_can_documents', return_value=docs), \
                 patch.object(p3, '_can_coe', return_value=coe):
                return [t[0] for t in p3.pim360_tabs()]

    def test_no_pim_access_no_tabs(self):
        self.assertEqual(self._tabs({'permissions': {'can_coaching': True}}, coaching=True), [])

    def test_pim_only_gets_discipline(self):
        self.assertEqual(self._tabs({'permissions': {'can_pim': True}}), ['discipline'])

    def test_tabs_follow_feature_access(self):
        self.assertEqual(self._tabs({'permissions': {'can_pim': True}}, coaching=True, coe=True),
                         ['discipline', 'coaching', 'documents'])
        self.assertEqual(self._tabs({'is_admin': True}, docs=True), ['discipline', 'documents'])


class RouteTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = appmod.app
        cls.app.config['TESTING'] = True

    def _client(self, **sess):
        c = self.app.test_client()
        with c.session_transaction() as s:
            s['user'] = {'email': 'zztest-pim@cohere.ph', 'name': 'T', 'employee_id': '000000-00',
                         'emp_number': 9999}
            s.update(sess)
        return c

    def _get(self, client, path, tables, coaching=False, docs=False, coe=False, levels=('hr', 'everyone')):
        conn = FakeConn(tables)
        with ExitStack() as st:
            st.enter_context(patch.dict(p3._h, {'get_central_db': lambda: conn}))
            st.enter_context(patch.object(p3, '_can_coaching', return_value=coaching))
            st.enter_context(patch.object(p3, '_can_documents', return_value=docs))
            st.enter_context(patch.object(p3, '_can_coe', return_value=coe))
            st.enter_context(patch('modules.incident_reports.ir_user', return_value={}))
            st.enter_context(patch('modules.incident_reports.report_scope_conds', return_value=(['1=1'], [])))
            st.enter_context(patch('modules.documents._visible_levels', return_value=levels))
            return client.get(path), conn

    EMP = {'gsheet_employees': [{'1': 1}]}

    def test_without_pim_access_is_forbidden(self):
        c = self._client(permissions={'can_coaching': True})
        r, _ = self._get(c, '/pim/E1/tab/coaching', self.EMP, coaching=True)
        self.assertEqual(r.status_code, 403)

    def test_tab_the_viewer_cannot_use_is_forbidden(self):
        c = self._client(permissions={'can_pim': True})
        r, _ = self._get(c, '/pim/E1/tab/coaching', self.EMP, coaching=False)
        self.assertEqual(r.status_code, 403)

    def test_unknown_employee_404_and_connection_closed(self):
        c = self._client(is_admin=True)
        r, conn = self._get(c, '/pim/E1/tab/discipline', {})
        self.assertEqual(r.status_code, 404)
        self.assertTrue(conn.closed)

    def test_discipline_renders(self):
        c = self._client(permissions={'can_pim': True})
        tables = dict(self.EMP, incident_reports=[
            {'report_number': 'IR-77', 'incident_date': date(2026, 11, 3), 'summary': 'Late 3x',
             'status': 'resolved', 'memo_category': 'Attendance Policy', 'disciplinary_action': None,
             'submitted_by_name': 'TL One', 'created_at': datetime(2026, 11, 4)}],
            undertime_cycle_state=[])
        r, _ = self._get(c, '/pim/E1/tab/discipline', tables)
        self.assertEqual(r.status_code, 200)
        html = r.data.decode()
        self.assertIn('IR-77', html)
        self.assertIn('/incident-reports/IR-77', html)
        self.assertIn('No undertime recorded', html)

    def test_documents_without_hr_level_says_so(self):
        c = self._client(is_admin=True)
        tables = dict(self.EMP, hr_documents=[
            {'id': 9, 'title': 'Contract', 'category': 'Contract / Agreement', 'doc_date': date(2026, 1, 5),
             'visibility': 'everyone', 'original_name': 'c.pdf', 'file_size': 2048, 'uploaded_at': None}])
        r, _ = self._get(c, '/pim/E1/tab/documents', tables, docs=True, levels=('everyone',))
        html = r.data.decode()
        self.assertIn('Contract', html)
        self.assertIn('HR-only files are hidden', html)
        self.assertNotIn('Certificate of Employment', html)   # no CoE access

    def test_coe_only(self):
        c = self._client(is_admin=True)
        tables = dict(self.EMP, coe_requests=[
            {'id': 1, 'purpose': 'Bank loan', 'purpose_other': None, 'addressee': 'BDO', 'status': 'released',
             'requested_at': datetime(2026, 9, 1), 'processed_by_name': 'HR', 'released_at': datetime(2026, 9, 3),
             'released_via': 'email'}])
        r, _ = self._get(c, '/pim/E1/tab/documents', tables, coe=True)
        html = r.data.decode()
        self.assertIn('Bank loan', html)
        self.assertNotIn('Documents library', html)


if __name__ == '__main__':
    unittest.main()
