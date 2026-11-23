"""Numa Stats: sheet parsing, the sync's wipe guard, access rules and the data
API. Fully mocked: no Google or database calls."""

import os
import sys
import unittest
from datetime import date
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask

from helpers import numa_stats as ns
from modules import numa_stats as mod

MONTH_HDR = ['Username', 'Created Month', 'Unique Conversation Count', 'Avg Csat',
             'Email Message SLA Percentage', 'Avg Response Time', 'Median Response Time',
             'P90 Response Time', 'Average First Reply Time', 'Median First Reply Time',
             'CSAT - Whatsapp', 'Average Q1 scores', 'Average Q2 scores', 'Average Q3 scores', 'Month']
WEEK_HDR = [''] + MONTH_HDR[:1] + ['Created Week'] + MONTH_HDR[2:-1] + ['Week']


class ValueParsingTests(unittest.TestCase):
    def test_numbers(self):
        self.assertEqual(ns.parse_number('1,099'), 1099.0)
        self.assertEqual(ns.parse_number('74.15%'), 74.15)
        self.assertEqual(ns.parse_number(' 4.67 '), 4.67)
        self.assertIsNone(ns.parse_number(''))
        self.assertIsNone(ns.parse_number(None))
        self.assertIsNone(ns.parse_number('#N/A'))

    def test_durations(self):
        self.assertEqual(ns.parse_duration('0:18:36'), 1116)
        self.assertEqual(ns.parse_duration('22:57:48'), 22 * 3600 + 57 * 60 + 48)
        self.assertEqual(ns.parse_duration('1095:02:10'), 1095 * 3600 + 130)
        self.assertIsNone(ns.parse_duration(''))
        self.assertIsNone(ns.parse_duration('1:99:00'))

    def test_periods(self):
        self.assertEqual(ns.parse_period('month', '2026-01'), date(2026, 1, 1))
        self.assertEqual(ns.parse_period('month', '2026-2'), date(2026, 2, 1))
        self.assertIsNone(ns.parse_period('month', '2026-13'))
        self.assertEqual(ns.parse_period('week', '2026-11-09'), date(2026, 11, 9))
        self.assertEqual(ns.parse_period('day', '11/18/2026'), date(2026, 11, 18))
        self.assertIsNone(ns.parse_period('week', 'week 46'))


class ParseTabTests(unittest.TestCase):
    def test_month_tab(self):
        rows, skipped = ns.parse_tab('month', [
            MONTH_HDR,
            ['Nina_DelCastillo', '2026-01', '565', '5', '74.15%', '0:18:36', '0:04:03', '0:16:59',
             '0:08:01', '0:04:53', '67%', '67%', '100%', '100%', 'January'],
            ['', '', '', '', '', '', '', '', '', '', '', '', '', '', 'December'],   # filler row
        ])
        self.assertEqual(skipped, 0)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r['username'], 'nina_delcastillo')
        self.assertEqual(r['period_start'], date(2026, 1, 1))
        self.assertEqual(r['conversations'], 565)
        self.assertEqual(r['email_sla_pct'], 74.15)
        self.assertEqual(r['whatsapp_csat_pct'], 67.0)
        self.assertEqual(r['median_response_sec'], 243)

    def test_week_tab_blank_first_column_and_missing_cells(self):
        rows, _ = ns.parse_tab('week', [WEEK_HDR, ['', 'jessa_barcastigue', '2026-01-05', '211', '5', '97.70%']])
        self.assertEqual(rows[0]['period_start'], date(2026, 1, 5))
        self.assertEqual(rows[0]['conversations'], 211)
        self.assertIsNone(rows[0]['whatsapp_csat_pct'])
        self.assertIsNone(rows[0]['avg_response_sec'])

    def test_duplicate_key_last_wins_and_bad_rows_counted(self):
        rows, skipped = ns.parse_tab('month', [
            MONTH_HDR, ['a_b', '2026-03', '1'], ['a_b', '2026-3', '2'], ['c_d', 'March', '9']])
        self.assertEqual([(r['username'], r['conversations']) for r in rows], [('a_b', 2)])
        self.assertEqual(skipped, 1)

    def test_missing_headers_raise(self):
        with self.assertRaises(ns.SheetFormatError):
            ns.parse_tab('month', [['Name', 'Month', 'Count']])
        with self.assertRaises(ns.SheetFormatError):
            ns.parse_tab('month', [])


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.conn.sql.append(sql.strip().split()[0].upper())
        self._one = {'n': self.conn.have}

    def executemany(self, sql, seq):
        self.conn.inserted = list(seq)

    def fetchone(self):
        return self._one


class FakeConn:
    def __init__(self, have):
        self.have, self.sql, self.inserted = have, [], None
        self.committed = self.rolled_back = False

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


def mkrows(n):
    return [{'period_start': date(2026, 1, 1), 'username': f'u{i}', **{m: None for m in ns.METRICS}}
            for i in range(n)]


class WriteGuardTests(unittest.TestCase):
    def test_replaces_rows(self):
        conn = FakeConn(have=10)
        res = ns.write_rows(conn, 'month', mkrows(12), None)
        self.assertIn('12 rows (was 10)', res)
        self.assertIn('DELETE', conn.sql)
        self.assertEqual(len(conn.inserted), 12)
        self.assertTrue(conn.committed)

    def test_empty_sheet_does_not_wipe(self):
        conn = FakeConn(have=10)
        self.assertIn('SKIPPED', ns.write_rows(conn, 'month', [], None))
        self.assertNotIn('DELETE', conn.sql)

    def test_big_shrink_blocked_unless_forced(self):
        conn = FakeConn(have=100)
        self.assertIn('SKIPPED', ns.write_rows(conn, 'week', mkrows(40), None))
        self.assertNotIn('DELETE', conn.sql)
        conn = FakeConn(have=100)
        self.assertNotIn('SKIPPED', ns.write_rows(conn, 'week', mkrows(40), None, force=True))
        self.assertIn('DELETE', conn.sql)

    def test_first_load_allowed(self):
        conn = FakeConn(have=0)
        self.assertIn('3 rows (was 0)', ns.write_rows(conn, 'day', mkrows(3), None))


class HelperTests(unittest.TestCase):
    def test_username_from_email(self):
        self.assertEqual(mod.username_from_email('Nina.DelCastillo@cohere.ph'), 'nina_delcastillo')
        self.assertEqual(mod.username_from_email(''), '')

    def test_period_labels(self):
        self.assertEqual(mod.period_label('month', date(2026, 11, 1)), 'November 2026')
        self.assertEqual(mod.period_label('week', date(2026, 11, 9)), 'Week 46 · Nov 9 – Nov 15, 2026')
        self.assertEqual(mod.period_label('week', date(2025, 12, 29)), 'Week 1 · Dec 29 – Jan 4, 2026')

    def test_team_value_total_weighted_median(self):
        rows = [{'conversations': 1, 'whatsapp_csat_pct': 100, 'email_sla_pct': 5},
                {'conversations': None, 'whatsapp_csat_pct': None, 'email_sla_pct': None},
                {'conversations': 3, 'whatsapp_csat_pct': 67, 'email_sla_pct': 9},
                {'conversations': 10, 'whatsapp_csat_pct': 83, 'email_sla_pct': 20}]
        self.assertEqual(mod.team_value(rows, 'conversations'), 14)                 # total
        # (100*1 + 67*3 + 83*10) / 14 = 80.79 -- weighted by conversations
        self.assertEqual(mod.team_value(rows, 'whatsapp_csat_pct'), 80.79)
        self.assertEqual(mod.team_value(rows, 'email_sla_pct'), 9)                  # median
        # CSAT without a conversation count can't be weighted, so it's left out
        self.assertEqual(mod.team_value([{'email_csat': 5, 'conversations': 300},
                                         {'email_csat': 1, 'conversations': None}], 'email_csat'), 5)
        self.assertIsNone(mod.team_value([{'email_csat': None, 'conversations': 4}], 'email_csat'))
        self.assertIsNone(mod.team_value([{'conversations': None}], 'conversations'))


def make_app():
    app = Flask(__name__, template_folder=os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), 'templates'))
    app.secret_key = 'test'
    app.testing = True
    app.register_blueprint(mod.numa_stats_bp)
    app.add_url_rule('/login', 'login', lambda: 'login')
    app.add_url_rule('/dashboard', 'dashboard', lambda: 'dash')
    return app


class AccessTests(unittest.TestCase):
    def setUp(self):
        self.app = make_app()
        self.client = self.app.test_client()

    def login(self, email, admin=False):
        with self.client.session_transaction() as s:
            s['user'] = {'email': email}
            s['is_admin'] = admin

    def test_anonymous_redirected_to_login(self):
        r = self.client.get('/numa-stats/api/data')
        self.assertEqual(r.status_code, 302)

    def test_non_numa_forbidden_and_cached(self):
        self.login('someone@cohere.ph')
        with mock.patch.object(mod, '_lookup_access', return_value=False) as look:
            self.assertEqual(self.client.get('/numa-stats/api/data').status_code, 403)
            self.assertEqual(self.client.get('/numa-stats/api/trend').status_code, 403)
            self.assertEqual(look.call_count, 1)          # second call served from session cache
        self.assertEqual(self.client.get('/numa-stats/').status_code, 302)

    def test_admin_skips_lookup(self):
        self.login('boss@cohere.ph', admin=True)
        with mock.patch.object(mod, '_lookup_access') as look, \
             mock.patch.object(mod, 'get_db_connection', side_effect=RuntimeError('db')):
            with self.assertRaises(RuntimeError):
                self.client.get('/numa-stats/api/data')
            look.assert_not_called()

    def test_sync_admin_only_and_needs_csrf(self):
        self.login('agent@cohere.ph')
        self.assertEqual(self.client.post('/numa-stats/api/sync').status_code, 403)
        self.login('boss@cohere.ph', admin=True)
        with mock.patch.object(mod.ns, 'sync') as sync, \
             mock.patch.object(mod, 'get_db_connection'):
            self.assertEqual(self.client.post('/numa-stats/api/sync').status_code, 400)
            sync.assert_not_called()
            with self.client.session_transaction() as s:
                s['_csrf_token'] = 'tok'
            sync.return_value = (True, ['month: 1 rows (was 0)'])
            r = self.client.post('/numa-stats/api/sync', headers={'X-CSRF-Token': 'tok'})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(sync.call_args.kwargs['triggered_by'], 'boss@cohere.ph')


    def test_sync_list_can_sync_but_not_edit_teams(self):
        self.login('tl@cohere.ph')
        with mock.patch.dict(mod.os.environ, {'NUMA_STATS_SYNC_EMAILS': 'TL@cohere.ph, qa@cohere.ph'}), \
             mock.patch.object(mod.ns, 'sync', return_value=(True, ['ok'])) as sync, \
             mock.patch.object(mod, 'get_db_connection'), \
             mock.patch.object(mod, '_lookup_access', return_value=False):
            with self.client.session_transaction() as s:
                s['_csrf_token'] = 'tok'
            r = self.client.post('/numa-stats/api/sync', headers={'X-CSRF-Token': 'tok'})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(sync.call_args.kwargs['triggered_by'], 'tl@cohere.ph')
            self.assertEqual(self.client.get('/numa-stats/api/teams').status_code, 403)
            with self.app.test_request_context():
                from flask import session
                session['user'] = {'email': 'qa@cohere.ph'}
                self.assertTrue(mod.numa_stats_can_view())
                session['user'] = {'email': 'agent@cohere.ph'}
                self.assertFalse(mod.numa_stats_can_sync())

class DataApiTests(unittest.TestCase):
    """api_data with a scripted cursor: roster filter, previous-period deltas, medians."""

    def setUp(self):
        self.app = make_app()
        self.client = self.app.test_client()
        with self.client.session_transaction() as s:
            s['user'] = {'email': 'nina.delcastillo@cohere.ph'}
            s['_numa_stats_access'] = {'email': 'nina.delcastillo@cohere.ph', 'ok': True, 'ts': 9e18}

    def test_rows_and_deltas(self):
        nov, octo = date(2026, 11, 1), date(2026, 10, 1)
        base = {m: None for m in mod.TABLE_METRICS}
        answers = [
            [{'schedule_name': 'Nina Jane Del Castillo', 'email': 'nina.delcastillo@cohere.ph', 'group_name': 'Numa CS'},
             {'schedule_name': 'Ella Marie Sultan', 'email': 'ella.sultan@cohere.ph', 'group_name': 'Numa BO'},
             {'schedule_name': 'Peter Justine Inson', 'email': 'peter.inson@cohere.ph', 'group_name': 'Numa CS'}],
            [{'username': 'peter_inson', 'team': 'GX Night Shift'}, {'username': 'gone_agent', 'team': 'GX Night Shift'}],
            [{'period_start': nov}, {'period_start': octo}],
            [{**base, 'username': 'nina_delcastillo', 'period_start': nov, 'conversations': 356, 'whatsapp_csat_pct': 100},
             {**base, 'username': 'nina_delcastillo', 'period_start': octo, 'conversations': 445, 'whatsapp_csat_pct': 100},
             {**base, 'username': 'peter_inson', 'period_start': octo, 'conversations': 841}],
            [{'username': 'nina_delcastillo'}, {'username': 'peter_inson'}],
            None,   # last sync row
            None,   # last ok
        ]

        class Cur:
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def execute(s, sql, params=None): s.ans = answers.pop(0)
            def fetchall(s): return s.ans
            def fetchone(s): return s.ans

        conn = mock.Mock()
        conn.cursor.side_effect = lambda: Cur()
        with mock.patch.object(mod, 'get_db_connection', return_value=conn):
            j = self.client.get('/numa-stats/api/data?type=month').get_json()
        self.assertEqual(j['period'], '2026-11-01')
        self.assertEqual(j['prev_period_label'], 'October 2026')
        self.assertEqual([r['username'] for r in j['rows']], ['nina_delcastillo', 'peter_inson'])  # BO with no data left out
        nina, peter = j['rows']
        self.assertEqual((nina['conversations'], nina['prev_conversations']), (356, 445))
        self.assertIsNone(peter['conversations'])
        self.assertEqual(peter['prev_conversations'], 841)
        self.assertEqual(j['total_conversations'], 356)
        self.assertEqual(j['summary']['conversations'], 356)
        self.assertEqual(j['prev_summary']['conversations'], 1286)          # total 445 + 841
        self.assertEqual(j['median_conversations'], 356)
        self.assertEqual(j['summary']['whatsapp_csat_pct'], 100)
        self.assertEqual(j['prev_summary']['whatsapp_csat_pct'], 100)
        # team split: unassigned -> GX Team, peter -> night shift; each group has its own figures
        self.assertEqual((nina['team'], peter['team']), ('GX Team', 'GX Night Shift'))
        self.assertEqual([(g['team'], g['agents'], g['total_conversations']) for g in j['groups']],
                         [('GX Team', 1, 356), ('GX Night Shift', 1, 0)])
        self.assertEqual(j['groups'][1]['prev_summary']['conversations'], 841)

    def test_bad_type_or_team(self):
        self.assertEqual(self.client.get('/numa-stats/api/data?type=year').status_code, 400)
        self.assertEqual(self.client.get('/numa-stats/api/data?team=Day%20Shift').status_code, 400)
        self.assertEqual(self.client.get('/numa-stats/api/trend?team=x').status_code, 400)


class TeamsApiTests(unittest.TestCase):
    def setUp(self):
        self.app = make_app()
        self.client = self.app.test_client()

    def login(self, admin):
        with self.client.session_transaction() as s:
            s['user'] = {'email': 'boss@cohere.ph'}
            s['is_admin'] = admin
            s['_csrf_token'] = 'tok'

    def post(self, body, token='tok'):
        return self.client.post('/numa-stats/api/teams', json=body, headers={'X-CSRF-Token': token})

    def test_agents_cannot_edit(self):
        self.login(admin=False)
        self.assertEqual(self.client.get('/numa-stats/api/teams').status_code, 403)
        self.assertEqual(self.post({'username': 'a', 'team': 'GX Team'}).status_code, 403)

    def test_admin_save_validates(self):
        self.login(admin=True)
        conn = mock.MagicMock()
        roster = {'peter_inson': {'name': 'Peter', 'group': 'Numa CS', 'team': 'GX Team'}}
        with mock.patch.object(mod, 'get_db_connection', return_value=conn), \
             mock.patch.object(mod, '_roster', return_value=roster):
            self.assertEqual(self.post({'username': 'peter_inson', 'team': 'GX Team'}, token='bad').status_code, 400)
            self.assertEqual(self.post({'username': 'peter_inson', 'team': 'Day Shift'}).status_code, 400)
            self.assertEqual(self.post({'username': 'someone_else', 'team': 'GX Team'}).status_code, 400)
            r = self.post({'username': 'Peter_Inson', 'team': 'GX Night Shift'})
            self.assertEqual(r.status_code, 200)
            params = conn.cursor.return_value.__enter__.return_value.execute.call_args.args[1]
            self.assertEqual(params, ('peter_inson', 'GX Night Shift', 'boss@cohere.ph'))
            conn.commit.assert_called_once()


if __name__ == '__main__':
    unittest.main()
