"""
Mocked/offline tests for audit #19: tardiness_notify.py and overbreak_notify.py
commit each employee/row on its own BEFORE any of its emails go out, so a
failure part-way through a run can't roll back IRs that were already emailed
(which made the next run file and email them again).

Run with:
    ./venv/bin/python -m unittest tests.test_notify_commit -v
from the repo root.

No real DB, no SMTP. Every DB, IR and email boundary is patched and records
into one shared `events` list, so the tests can assert on ordering.
Nothing here runs --live against real data.
"""
import sys
import unittest
from datetime import date, datetime
from unittest.mock import MagicMock, patch

sys.path.insert(0, '/var/www/html/leavesystem')

import tardiness_notify as tn  # noqa: E402
import overbreak_notify as ob  # noqa: E402
import undertime_notify as un  # noqa: E402


class FakeConn:
    def __init__(self, events, fail_on_commit=None):
        self.events = events
        self.cur = MagicMock()
        self.cur.execute.side_effect = lambda q, p=None: events.append(('sql', q.split()[0].upper(), p))

    def cursor(self):
        return self.cur

    def commit(self):
        self.events.append(('commit',))

    def rollback(self):
        self.events.append(('rollback',))

    def close(self):
        pass


D = date(2026, 10, 14)
PS = date(2026, 10, 8)


def late(personid, companyid, minutes=5, name='A'):
    return {'personid': personid, 'companyid': companyid, 'fname': name, 'lname': 'Test',
            'status': 'LATE', 'minutes_late': minutes, 'time_in': datetime(2026, 10, 14, 13, minutes)}


def tstate(personid, **over):
    s = {'personid': personid, 'period_start': PS, 'count_since_reset': 2, 'minutes_since_reset': 0,
         'total_count_in_cycle': 2, 'total_minutes_in_cycle': 0, 'last_processed_date': date(2026, 10, 13),
         'count_triggers_sent': 0, 'minutes_triggers_sent': 0,
         'count_breakdown': [{'date': date(2026, 10, 9), 'minutes': 1, 'time_in': None},
                             {'date': date(2026, 10, 13), 'minutes': 1, 'time_in': None}],
         'minutes_breakdown': []}
    s.update(over)
    return s


class TardinessCase(unittest.TestCase):

    def run_day(self, records, states, live=True, fail_ir_for=None, process_date=D):
        events = []
        conn = FakeConn(events)
        counter = iter(range(1, 100))

        def fake_file(cur, companyid, name, d, summary, dry_run=False, log_prefix='', notify=True):
            self.assertFalse(notify, 'IR email must be deferred until after commit')
            if companyid == fail_ir_for:
                raise RuntimeError('db hiccup')
            number = f'IR-{companyid}-{next(counter)}'
            events.append(('file', companyid, dry_run))
            return number

        patches = [
            patch.object(tn, 'get_tardiness_for_date', MagicMock(return_value=records)),
            patch.object(tn, 'get_db_connection', MagicMock(return_value=conn)),
            patch.object(tn, 'get_or_create_state', MagicMock(side_effect=lambda cur, pid, ps: states[pid])),
            patch.object(tn, 'save_state', MagicMock(side_effect=lambda cur, s: events.append(('save', s['personid'])))),
            patch.object(tn, 'build_recipient_list', MagicMock(return_value=('tl@x.com', ['hr@x.com'], 'tl@x.com'))),
            patch.object(tn, 'is_ir_excluded', MagicMock(return_value=False)),
            patch.object(tn, 'file_incident_report', MagicMock(side_effect=fake_file)),
            patch.object(tn, 'send_ir_notification',
                         MagicMock(side_effect=lambda cur, n, *a, **k: events.append(('ir_email', n)))),
            patch.object(tn, 'send_email',
                         MagicMock(side_effect=lambda to, subj, body, cc_addresses=None: events.append(('memo', to)))),
        ]
        for pt in patches:
            pt.start()
            self.addCleanup(pt.stop)
        tn.process_day(process_date, live=live)
        return events

    def test_each_employee_committed_before_its_emails(self):
        events = self.run_day([late(1, 'E1'), late(2, 'E2')], {1: tstate(1), 2: tstate(2)})
        kinds = [e[0] for e in events if e[0] in ('file', 'save', 'commit', 'ir_email', 'memo')]
        self.assertEqual(kinds, ['file', 'save', 'commit', 'ir_email', 'memo',
                                 'file', 'save', 'commit', 'ir_email', 'memo'])

    def test_failure_on_second_employee_keeps_first(self):
        events = self.run_day([late(1, 'E1'), late(2, 'E2'), late(3, 'E3')],
                              {1: tstate(1), 2: tstate(2), 3: tstate(3)}, fail_ir_for='E2')
        kinds = [e[0] for e in events if e[0] in ('file', 'save', 'commit', 'rollback', 'ir_email', 'memo')]
        self.assertEqual(kinds, ['file', 'save', 'commit', 'ir_email', 'memo',   # E1 kept and emailed
                                 'rollback',                                     # E2 undone, no email
                                 'file', 'save', 'commit', 'ir_email', 'memo'])  # E3 still processed

    def test_failed_memo_email_is_logged_not_raised(self):
        with patch.object(tn, 'send_email', MagicMock(side_effect=OSError('smtp down'))):
            tn._send_memo('tl@x.com', 'subject', 'body', [])  # must not raise

    def test_dry_run_writes_nothing_and_sends_nothing(self):
        events = self.run_day([late(1, 'E1')], {1: tstate(1)}, live=False)
        kinds = [e[0] for e in events]
        self.assertNotIn('commit', kinds)
        self.assertNotIn('save', kinds)
        self.assertNotIn('ir_email', kinds)
        self.assertNotIn('memo', kinds)
        self.assertEqual([e for e in events if e[0] == 'file'], [('file', 'E1', True)])
        self.assertEqual(kinds[-1], 'rollback')

    def test_same_or_earlier_day_is_skipped(self):
        for last in (D, date(2026, 10, 15)):
            events = self.run_day([late(1, 'E1')], {1: tstate(1, last_processed_date=last)})
            self.assertEqual([e for e in events if e[0] in ('file', 'save', 'commit')], [])

    def test_below_threshold_saves_state_without_ir(self):
        events = self.run_day([late(1, 'E1')], {1: tstate(1, count_since_reset=0, count_breakdown=[])})
        kinds = [e[0] for e in events if e[0] in ('file', 'save', 'commit', 'ir_email', 'memo')]
        self.assertEqual(kinds, ['save', 'commit'])

    def test_both_rules_same_day_file_one_ir(self):
        events = self.run_day([late(1, 'E1', minutes=40)], {1: tstate(1)})
        kinds = [e[0] for e in events if e[0] in ('file', 'commit', 'ir_email', 'memo')]
        self.assertEqual(kinds, ['file', 'commit', 'ir_email', 'memo', 'memo'])

    def test_period_is_the_one_containing_process_date(self):
        # Oct 7 belongs to the Sep 23 - Oct 7 cycle, not the cycle "today" is in.
        self.run_day([late(1, 'E1')], {1: tstate(1, last_processed_date=None)}, process_date=date(2026, 10, 7))
        self.assertEqual(tn.get_or_create_state.call_args[0][2], date(2026, 9, 23))


class OverbreakCase(unittest.TestCase):

    def row(self, i, eid, minutes_str='2:30:00'):
        return {'id': i, 'row_uid': f'u{i}', 'record_date': date(2026, 10, 14), 'agent_name': f'Agent {eid}',
                'employee_id': eid, 'break_duration': minutes_str, 'payroll_month': 'October',
                'payroll_cycle': '1st', 'record_year': 2026}

    def run_rows(self, rows, fail_for=None, dry_run=False):
        events = []
        conn = FakeConn(events)
        conn.cur.fetchall.return_value = rows

        def state(cur, eid, key):
            if eid == fail_for:
                raise RuntimeError('db hiccup')
            return {'employee_id': eid, 'period_key': key, 'rule1_count_since_reset': 0,
                    'rule1_total_in_period': 0, 'rule1_triggers_sent': 0, 'rule1_breakdown': [],
                    'rule2_pair_since_reset': 0, 'rule2_triggers_sent': 0, 'rule2_breakdown': [],
                    'rule3_triggers_sent': 0, 'last_processed_row_uid': None}

        sent = []
        patches = [
            patch.object(ob, 'DRY_RUN', dry_run),
            patch.object(ob, 'get_db_connection', MagicMock(return_value=conn)),
            patch.object(ob, 'get_or_create_state', MagicMock(side_effect=state)),
            patch.object(ob, 'save_state', MagicMock(side_effect=lambda cur, s: events.append(('save', s['employee_id'])))),
            patch.object(ob, 'build_recipient_list', MagicMock(return_value=('tl@x.com', [], 'tl@x.com'))),
            patch.object(ob, 'get_agent_full_name', MagicMock(return_value=None)),
            patch.object(ob, 'is_ir_excluded', MagicMock(return_value=False)),
            patch.object(ob, 'send_email', MagicMock(side_effect=lambda to, s, b, cc_addresses=None: events.append(('memo', to)))),
            patch('helpers.ir_autofile._send_ir_notification',
                  MagicMock(side_effect=lambda cur, n, *a: events.append(('ir_email', n)))),
        ]
        for pt in patches:
            pt.start()
            self.addCleanup(pt.stop)
        ob.process_pending()
        return events

    def kinds(self, events):
        out = []
        for e in events:
            if e[0] == 'sql' and e[1] == 'INSERT':
                out.append('ir')
            elif e[0] == 'sql' and e[1] == 'UPDATE':
                out.append('processed')
            elif e[0] in ('save', 'commit', 'rollback', 'ir_email', 'memo'):
                out.append(e[0])
        return out

    def test_row_committed_before_emails_and_failure_isolated(self):
        # 2h30m breaks fire rule 3 (single break over 2 hours): one IR per row.
        events = self.run_rows([self.row(1, 'E1'), self.row(2, 'E2'), self.row(3, 'E3')], fail_for='E2')
        self.assertEqual(self.kinds(events), [
            'ir', 'save', 'processed', 'commit', 'ir_email', 'memo',   # E1
            'rollback',                                                # E2 undone, stays unprocessed
            'ir', 'save', 'processed', 'commit', 'ir_email', 'memo',   # E3
            'commit',                                                  # final no-op commit
        ])

    def test_dry_run_files_and_marks_nothing(self):
        events = self.run_rows([self.row(1, 'E1')], dry_run=True)
        k = self.kinds(events)
        self.assertNotIn('ir', k)
        self.assertNotIn('processed', k)
        self.assertNotIn('commit', k)
        self.assertNotIn('ir_email', k)
        self.assertEqual(k[-1], 'rollback')


class UndertimeCase(unittest.TestCase):
    """#20: undertime_notify sends the IR email and memos only after the commit."""

    def test_emails_after_commit_and_none_for_failed_employee(self):
        events = []
        conn = FakeConn(events)

        def rec(eid):
            return {'companyid': eid, 'fname': 'A', 'lname': 'Test', 'status': 'UNDERTIME',
                    'minutes_early': 40, 'time_in': datetime(2026, 10, 14, 9),
                    'time_out': datetime(2026, 10, 14, 17, 20), 'shift_end': datetime(2026, 10, 14, 18),
                    'out_source': 'punch', 'shift_time_raw': '9am-6pm'}

        def fake_file(cur, companyid, *a, **kw):
            self.assertFalse(kw['notify'])
            if companyid == 'E2':
                raise RuntimeError('db hiccup')
            events.append(('file', companyid))
            return f'IR-{companyid}'

        patches = [
            patch.object(un, 'LIVE_START_DATE', date.min),
            patch.object(un, 'get_undertime_for_date', MagicMock(return_value=[rec('E1'), rec('E2'), rec('E3')])),
            patch.object(un, 'get_db_connection', MagicMock(return_value=conn)),
            patch.object(un, 'get_or_create_state', MagicMock(side_effect=lambda cur, eid, ps: un._new_state(eid, ps))),
            patch.object(un, 'save_state', MagicMock(side_effect=lambda cur, st: events.append(('save', st['employee_id'])))),
            patch.object(un, 'build_recipient_list', MagicMock(return_value=(['tl@x.com'], 'tl@x.com'))),
            patch.object(un, 'is_ir_excluded', MagicMock(return_value=False)),
            patch.object(un, 'file_incident_report', MagicMock(side_effect=fake_file)),
            patch.object(un, 'send_ir_notification',
                         MagicMock(side_effect=lambda cur, n, *a, **k: events.append(('ir_email', n)))),
            patch.object(un, 'send_email', MagicMock(side_effect=lambda to, subj, body: events.append(('memo', to)))),
        ]
        for pt in patches:
            pt.start()
            self.addCleanup(pt.stop)
        un.process_day(D, live=True)
        kinds = [e[0] for e in events if e[0] in ('file', 'save', 'commit', 'rollback', 'ir_email', 'memo')]
        self.assertEqual(kinds, ['file', 'save', 'commit', 'ir_email', 'memo',   # E1
                                 'rollback',                                     # E2: no email
                                 'file', 'save', 'commit', 'ir_email', 'memo'])  # E3


class BreakLogReviewCase(unittest.TestCase):
    """#20: a Break Log "Create IR" emails the IR only after the review is saved."""

    @classmethod
    def setUpClass(cls):
        import app as app_module
        import break_log.routes as br
        cls.app_module, cls.br = app_module, br
        cls.client = app_module.app.test_client()

    def post(self, insert_error=None):
        events = []

        class Cur:
            def __init__(self):
                self.rows = []

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, q, p=None):
                ql = q.lower()
                if 'from break_logs' in ql:
                    self.rows = [{'employee_name': 'Agent', 'account_type': 'Numa', 'total_min': 120}]
                elif 'from incident_reports' in ql:
                    self.rows = [{'id': 7}]
                elif 'insert into overbreak_reviews' in ql:
                    if insert_error:
                        raise insert_error
                    events.append(('review_insert',))

            def fetchone(self):
                return self.rows.pop(0) if self.rows else None

        class DB:
            def cursor(self):
                return Cur()

            def commit(self):
                events.append(('commit',))

            def rollback(self):
                events.append(('rollback',))

            def close(self):
                pass

        import helpers.ir_autofile as ira
        patches = [
            patch.object(self.app_module, 'get_central_db', MagicMock(return_value=DB())),
            patch.object(self.br, '_has_supervisor_access', MagicMock(return_value=True)),
            patch.object(self.br, '_get_employee_id', MagicMock(return_value={'employee_id': 'SUP1'})),
            patch.object(ira, 'file_incident_report',
                         MagicMock(side_effect=lambda *a, **k: events.append(('file', k['notify'])) or 'IR-X')),
            patch.object(ira, 'send_ir_notification',
                         MagicMock(side_effect=lambda cur, n, *a, **k: events.append(('ir_email', n)))),
        ]
        for pt in patches:
            pt.start()
            self.addCleanup(pt.stop)
        with self.client.session_transaction() as sess:
            sess['user'] = {'email': 'sup@x.com', 'name': 'Sup'}
            sess['_csrf_token'] = 'tok'
        resp = self.client.post('/api/supervisor/overbreaks/review',
                                json={'employee_id': 'ZZTEST-1', 'shift_date': '2026-10-14', 'action': 'create_ir'},
                                headers={'X-CSRF-Token': 'tok'})
        return resp, events

    def test_ir_emailed_after_commit(self):
        resp, events = self.post()
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertEqual(events, [('file', False), ('review_insert',), ('commit',), ('ir_email', 'IR-X')])

    def test_duplicate_review_rolls_back_and_sends_no_email(self):
        resp, events = self.post(insert_error=Exception("(1062, \"Duplicate entry 'x' for key\")"))
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(events, [('file', False), ('rollback',)])


if __name__ == '__main__':
    unittest.main()
