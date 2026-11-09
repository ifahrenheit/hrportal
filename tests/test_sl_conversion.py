"""Tests for modules/sl_conversion.py -- pure rules, build_report with fake
cursors, and the route on a throwaway Flask app. No real database is touched."""
import csv
import io
import unittest
from datetime import date, datetime
from decimal import Decimal
from unittest import mock

from flask import Flask

import modules.sl_conversion as slc
from modules.sl_conversion import (dedupe_sick, evaluate, filter_rows, is_projection,
                                   sick_match, sort_key, summarize, tier_for)

TODAY = date(2027, 2, 1)


def emp(**kw):
    base = {'unused_days': 12, 'has_master': True, 'status': 'Active',
            'operational_date': date(2024, 1, 1), 'exit_date': None}
    base.update(kw)
    return base


class TierTests(unittest.TestCase):
    def test_tiers(self):
        self.assertEqual(tier_for(96), 100)
        self.assertEqual(tier_for(95.99), 100)   # tolerance
        self.assertEqual(tier_for(95.9), 50)
        self.assertEqual(tier_for(88), 50)
        self.assertEqual(tier_for(87.5), 25)
        self.assertEqual(tier_for(80), 25)
        self.assertEqual(tier_for(79.9), 0)
        self.assertEqual(tier_for(None), 0)


class SickMatchTests(unittest.TestCase):
    def test_matches(self):
        for t in ('fever', 'Sickness recovery', 'advised to rest til 25', 'LBM', 'check-up',
                  'Checkup', 'tooth extraction', 'stomach pain', 'not feeling well'):
            self.assertTrue(sick_match(t), t)

    def test_non_matches(self):
        for t in ('Travel to Negros', 'rest day swap', 'restday', 'will attend wedding',
                  'operations meeting', '', None, 'Billing issue'):
            self.assertIsNone(sick_match(t), t)


class EvaluateTests(unittest.TestCase):
    def test_qualified(self):
        r = evaluate(emp(), [], [], 2026, TODAY)
        self.assertEqual((r['status'], r['tier'], r['converted_days']), ('qualified', 100, 12))

    def test_half_tier_conversion(self):
        r = evaluate(emp(unused_days=11), [], [], 2026, TODAY)
        self.assertEqual((r['tier'], r['converted_hours'], r['converted_days']), (50, 44, 5.5))

    def test_not_regular_by_jan1(self):
        r = evaluate(emp(operational_date=date(2025, 7, 2)), [], [], 2026, TODAY)
        self.assertEqual(r['status'], 'not_qualified')
        self.assertIn('Not regular by Jan 1 (reg 2026-01-02)', r['reasons'])
        r = evaluate(emp(operational_date=date(2025, 7, 1)), [], [], 2026, TODAY)
        self.assertEqual(r['status'], 'qualified')

    def test_awol(self):
        r = evaluate(emp(), [date(2026, 3, 3), date(2026, 6, 9)], [], 2026, TODAY)
        self.assertEqual(r['status'], 'not_qualified')
        self.assertIn('AWOL ×2: Mar 3, Jun 9', r['reasons'])

    def test_separated_and_exit(self):
        r = evaluate(emp(status='Separated', exit_date=date(2026, 12, 1)), [], [], 2026, TODAY)
        self.assertEqual(r['reasons'], ['Separated', 'Exit date 2026-12-01'])

    def test_future_exit_date_is_on_notice(self):
        r = evaluate(emp(exit_date=date(2027, 6, 1)), [], [], 2026, TODAY)
        self.assertEqual(r['status'], 'not_qualified')

    def test_training_not_eligible(self):
        self.assertEqual(evaluate(emp(status='Training'), [], [], 2026, TODAY)['status'],
                         'not_qualified')

    def test_sick_reason_is_review(self):
        hit = {'date': date(2026, 4, 2), 'code': 'VL', 'reason': 'fever', 'keyword': 'fever'}
        r = evaluate(emp(), [], [hit], 2026, TODAY)
        self.assertEqual(r['status'], 'review')
        self.assertEqual(r['review'], ["Sick reason as VL: Apr 2 'fever'"])

    def test_below_80_not_convertible_wins(self):
        r = evaluate(emp(unused_days=9, status='Separated'), [], [], 2026, TODAY)
        self.assertEqual((r['status'], r['converted_hours']), ('not_convertible', 0))

    def test_no_master(self):
        r = evaluate({'unused_days': 12, 'has_master': False}, [], [], 2026, TODAY)
        self.assertEqual(r['status'], 'not_qualified')


class MiscTests(unittest.TestCase):
    def test_projection(self):
        self.assertTrue(is_projection(2026, date(2026, 12, 31)))
        self.assertFalse(is_projection(2026, date(2027, 1, 1)))

    def test_dedupe_and_summary(self):
        d = date(2026, 5, 1)
        hits = [{'date': d, 'code': 'LWOP', 'reason': 'a'}, {'date': d, 'code': 'LWOP', 'reason': 'b'}]
        self.assertEqual(len(dedupe_sick(hits)), 1)
        rows = [dict(evaluate(emp(), [], [], 2026, TODAY), name='A', employee_id='1', group_name='CS'),
                dict(evaluate(emp(unused_days=5), [], [], 2026, TODAY), name='B', employee_id='2', group_name='L2')]
        s = summarize(rows)
        self.assertEqual((s['t100'], s['t0'], s['qualified'], s['qualified_days']), (1, 1, 1, 12))
        scoped, shown = filter_rows(rows, group='cs', status='all', q='')
        self.assertEqual([r['name'] for r in shown], ['A'])
        scoped, shown = filter_rows(rows, status='not_convertible')
        self.assertEqual((len(scoped), [r['name'] for r in shown]), (2, ['B']))



# ── Added coverage ───────────────────────────────────────────────────────────

class TierBoundaryTests(unittest.TestCase):
    def test_each_boundary_and_just_below(self):
        cases = [(96, 100), (95.99, 100), (95.98, 100), (95.97, 50), (88, 50), (87.99, 50),
                 (87.97, 25), (80, 25), (79.99, 25), (79.97, 0), (0, 0), (-8, 0), (200, 100)]
        for hours, pct in cases:
            self.assertEqual(tier_for(hours), pct, hours)

    def test_decimal_and_string_input(self):
        self.assertEqual(tier_for(Decimal('96.000')), 100)
        self.assertEqual(tier_for('88'), 50)

    def test_days_times_eight(self):
        # 12 d = 96 h, 11 d = 88 h, 10 d = 80 h, 9.99 d = 79.92 h
        for days, pct in ((12, 100), (11, 50), (10, 25), (Decimal('9.99'), 0),
                          (Decimal('11.9988'), 100)):
            self.assertEqual(evaluate(emp(unused_days=days), [], [], 2026, TODAY)['tier'], pct, days)

    def test_quarter_tier_conversion_amount(self):
        r = evaluate(emp(unused_days=10), [], [], 2026, TODAY)
        self.assertEqual((r['unused_hours'], r['converted_hours'], r['converted_days']), (80, 20, 2.5))


class SickWordBoundaryTests(unittest.TestCase):
    def test_schedule_and_substring_words_do_not_match(self):
        for t in ('rest day', 'Rest Days', 'rest-day', 'REST - DAY', 'operations', 'Operations team',
                  'restaurant', 'arrested', 'interest', 'skill test', "I'll be away", 'Spain trip',
                  'drive', 'pained'):
            self.assertIsNone(sick_match(t), t)

    def test_sick_words_still_match_around_punctuation(self):
        for t in ('needed rest.', 'REST', 'had an operation', 'went to Dr. Cruz', 'flu,', 'ill',
                  'with fever/cough', 'Sick'):
            self.assertTrue(sick_match(t), t)

    def test_returns_matched_text(self):
        self.assertEqual(sick_match('high FEVER since monday'), 'FEVER')


class QualifyRuleTests(unittest.TestCase):
    def test_reg_exactly_jan1_is_regular(self):
        r = evaluate(emp(operational_date=date(2025, 7, 1)), [], [], 2026, TODAY)
        self.assertEqual(r['reg_date'], date(2026, 1, 1))
        self.assertEqual(r['status'], 'qualified')

    def test_no_operational_date(self):
        r = evaluate(emp(operational_date=None), [], [], 2026, TODAY)
        self.assertEqual(r['status'], 'not_qualified')
        self.assertIn('No operational date (regularization unknown)', r['reasons'])

    def test_status_active_only_case_insensitive(self):
        self.assertEqual(evaluate(emp(status=' active '), [], [], 2026, TODAY)['status'], 'qualified')
        for st, why in (('', 'Status blank'), (None, 'Status blank'), ('Resigned', 'Status Resigned'),
                        ('separated', 'Separated')):
            r = evaluate(emp(status=st), [], [], 2026, TODAY)
            self.assertEqual(r['status'], 'not_qualified', st)
            self.assertIn(why, r['reasons'])

    def test_any_exit_date_disqualifies_even_if_active(self):
        for d in (date(2020, 1, 1), date(2026, 6, 30), date(2030, 1, 1)):
            r = evaluate(emp(exit_date=d), [], [], 2026, TODAY)
            self.assertEqual(r['status'], 'not_qualified', d)
            self.assertIn(f'Exit date {d.isoformat()}', r['reasons'])

    def test_single_awol_disqualifies(self):
        r = evaluate(emp(), [date(2026, 12, 31)], [], 2026, TODAY)
        self.assertEqual(r['reasons'], ['AWOL ×1: Dec 31'])


class StatusPrecedenceTests(unittest.TestCase):
    HIT = {'date': date(2026, 4, 2), 'code': 'LWOP', 'reason': 'fever', 'keyword': 'fever'}

    def test_not_convertible_beats_everything(self):
        r = evaluate(emp(unused_days=9.9, status='Separated'), [date(2026, 1, 5)], [self.HIT],
                     2026, TODAY)
        self.assertEqual(r['status'], 'not_convertible')
        self.assertEqual(r['status_label'], 'Not convertible')

    def test_not_qualified_beats_review(self):
        r = evaluate(emp(), [date(2026, 1, 5)], [self.HIT], 2026, TODAY)
        self.assertEqual(r['status'], 'not_qualified')
        self.assertEqual(len(r['review']), 1)   # note kept alongside the failure

    def test_long_reason_truncated(self):
        hit = dict(self.HIT, reason='fever ' + 'x' * 200)
        note = evaluate(emp(), [], [hit], 2026, TODAY)['review'][0]
        self.assertTrue(note.endswith("...'"))
        self.assertLess(len(note), 120)

    def test_sort_order(self):
        rows = [dict(evaluate(emp(unused_days=d, **kw), [], [], 2026, TODAY), name=n)
                for n, d, kw in (('Zed', 5, {}), ('Bob', 11, {}), ('amy', 12, {}),
                                 ('Cat', 12, {'status': 'Training'}), ('Al', 12, {}))]
        rows.append(dict(evaluate(emp(), [], [self.HIT], 2026, TODAY), name='Rev'))
        rows.sort(key=sort_key)
        self.assertEqual([r['name'] for r in rows], ['Al', 'amy', 'Bob', 'Rev', 'Cat', 'Zed'])


# ── build_report with fake cursors ───────────────────────────────────────────

class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        self.conn.calls.append((sql, params))
        for key, rows in self.conn.routes:
            if key in sql:
                self._rows = rows
                return
        raise AssertionError('unexpected SQL: ' + sql[:80])

    def fetchall(self):
        return list(self._rows)


class FakeConn:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.closed = True


LEAVE_TYPES = [{'id': 1, 'name': 'Vacation Leave'}, {'id': 2, 'name': 'Sick Leave'},
               {'id': 3, 'name': 'Leave Without Pay (LWOP)'}, {'id': 7, 'name': 'AWOL'}]


def master(eid, **kw):
    m = {'employee_id': eid, 'zd_name': 'ZD ' + eid, 'email': f'{eid.lower()}@x.ph',
         'status': 'Active', 'group_name': 'CS', 'account': 'Acme',
         'operational_date': date(2024, 1, 1), 'exit_date': None}
    m.update(kw)
    return m


def ent(num, eid, days, name=None):
    return {'emp_number': num, 'employee_id': eid,
            'ohrm_name': f'Emp {eid}' if name is None else name,
            'unused_days': Decimal(str(days))}


class BuildReportTests(unittest.TestCase):
    def run_report(self, ents, portal=(), masters=(), absences=(), types=LEAVE_TYPES):
        self.ohrm = FakeConn([('ohrm_leave_type', types), ('ohrm_leave_entitlement', ents),
                              ('leave4day_requests', list(portal))])
        self.central = FakeConn([('gsheet_employees', list(masters)),
                                 ('absence_records', list(absences))])
        with mock.patch.dict(slc._h, get_db=lambda: self.ohrm,
                             get_central_db=lambda: self.central):
            rows = slc.build_report(2026, TODAY)
        return {r['employee_id']: r for r in rows}, rows

    def test_happy_path_and_params(self):
        by, _ = self.run_report([ent(10, 'E1', 12)], masters=[master('E1')])
        self.assertEqual(by['E1']['status'], 'qualified')
        self.assertEqual(by['E1']['name'], 'Emp E1')
        self.assertTrue(self.ohrm.closed and self.central.closed)
        sql, params = next(c for c in self.ohrm.calls if 'ohrm_leave_entitlement' in c[0])
        self.assertEqual(params, (2, date(2026, 1, 1), date(2027, 1, 1)))
        sql, params = next(c for c in self.ohrm.calls if 'leave4day_requests' in c[0])
        self.assertIn("status IN (%s,%s,%s)", sql)
        self.assertIn('deleted_at IS NULL', sql)
        self.assertEqual(params, (1, 3, 7, date(2026, 1, 1), date(2026, 12, 31),
                                  'pending', 'approved', 'scheduled'))
        sql, params = next(c for c in self.central.calls if 'absence_records' in c[0])
        self.assertIn("NOT IN ('Disapproved', 'Exempted')", sql)
        self.assertEqual(params, (date(2026, 1, 1), date(2026, 12, 31), 'VL', 'LWOP', 'AWOL'))

    def test_no_sl_type_returns_empty(self):
        _, rows = self.run_report([], types=[{'id': 1, 'name': 'Vacation Leave'}])
        self.assertEqual(rows, [])

    def test_empty_entitlements(self):
        _, rows = self.run_report([], masters=[master('E1')])
        self.assertEqual(rows, [])

    def test_awol_from_both_sources_deduplicated(self):
        d1, d2 = date(2026, 3, 3), date(2026, 5, 5)
        portal = [{'emp_number': 10, 'employee_id': 'E1', 'leave_type_id': 7,
                   'leave_date': d1, 'notes': 'no call no show'}]
        absences = [{'employee_id': 'E1', 'email': None, 'absence_date': d1, 'code': 'AWOL', 'reason': ''},
                    {'employee_id': 'E1', 'email': None, 'absence_date': d1, 'code': 'awol ', 'reason': None},
                    {'employee_id': 'E1', 'email': None, 'absence_date': d2, 'code': 'AWOL', 'reason': ''}]
        by, _ = self.run_report([ent(10, 'E1', 12)], portal, [master('E1')], absences)
        self.assertEqual(by['E1']['reasons'], ['AWOL ×2: Mar 3, May 5'])

    def test_awol_matched_by_email_and_emp_number(self):
        portal = [{'emp_number': 11, 'employee_id': '', 'leave_type_id': 7,
                   'leave_date': date(2026, 2, 2), 'notes': None}]
        absences = [{'employee_id': '', 'email': ' E1@X.PH ', 'absence_date': date(2026, 4, 4),
                     'code': 'AWOL', 'reason': 'x'}]
        by, _ = self.run_report([ent(10, 'E1', 12), ent(11, 'E2', 12)], portal,
                                [master('E1'), master('E2')], absences)
        self.assertEqual(by['E1']['status'], 'not_qualified')
        self.assertEqual(by['E2']['reasons'], ['AWOL ×1: Feb 2'])

    def test_vl_lwop_not_awol(self):
        portal = [{'emp_number': 10, 'employee_id': 'E1', 'leave_type_id': 1,
                   'leave_date': date(2026, 2, 2), 'notes': 'family trip'}]
        absences = [{'employee_id': 'E1', 'email': None, 'absence_date': date(2026, 4, 4),
                     'code': 'LWOP', 'reason': 'rest day swap'}]
        by, _ = self.run_report([ent(10, 'E1', 12)], portal, [master('E1')], absences)
        self.assertEqual((by['E1']['status'], by['E1']['review']), ('qualified', []))

    def test_sick_reasons_need_review_and_dedupe(self):
        d = date(2026, 6, 1)
        portal = [{'emp_number': 10, 'employee_id': 'E1', 'leave_type_id': 1,
                   'leave_date': d, 'notes': 'Fever'}]
        absences = [{'employee_id': 'E1', 'email': None, 'absence_date': d, 'code': 'VL', 'reason': 'fever again'},
                    {'employee_id': 'E1', 'email': None, 'absence_date': d, 'code': 'LWOP', 'reason': 'flu'}]
        by, _ = self.run_report([ent(10, 'E1', 12)], portal, [master('E1')], absences)
        self.assertEqual(by['E1']['status'], 'review')
        self.assertEqual(len(by['E1']['review']), 2)   # one VL + one LWOP for the same day

    def test_no_master_row_and_name_fallbacks(self):
        by, _ = self.run_report([ent(10, 'E1', 12, name=' '), ent(11, 'E2', 12, name='')],
                                masters=[master('E2')])
        self.assertEqual(by['E1']['status'], 'not_qualified')
        self.assertEqual(by['E1']['name'], 'E1')
        self.assertEqual(by['E2']['name'], 'ZD E2')

    def test_rows_sorted(self):
        _, rows = self.run_report([ent(1, 'A', 5), ent(2, 'B', 11), ent(3, 'C', 12)],
                                  masters=[master('A'), master('B'), master('C')])
        self.assertEqual([r['employee_id'] for r in rows], ['C', 'B', 'A'])


# ── Route ────────────────────────────────────────────────────────────────────

def make_app():
    app = Flask(__name__, template_folder='../templates')
    app.secret_key = 'test'
    app.add_url_rule('/login', 'login', lambda: 'login')
    app.add_url_rule('/dashboard', 'dashboard', lambda: 'dash')
    app.register_blueprint(slc.sl_conversion_bp)
    return app


def sample_rows():
    rows = [dict(evaluate(emp(), [], [], 2026, TODAY), name='Ann Q', employee_id='E1'),
            dict(evaluate(emp(unused_days=5), [], [], 2026, TODAY), name='Ben Low', employee_id='E2'),
            dict(evaluate(emp(status='Training'), [], [], 2026, TODAY), name='Cy Train', employee_id='E3')]
    for r, g in zip(rows, ('CS', 'CS', 'L2')):
        r.update(group_name=g, account='Acme', emp_status='Active', emp_number=1)
    return rows


class RouteTests(unittest.TestCase):
    URL = '/admin/reports/sl-conversion'

    def setUp(self):
        self.app = make_app()
        self.client = self.app.test_client()
        p1 = mock.patch.object(slc, 'build_report', side_effect=lambda y, t: sample_rows())
        p2 = mock.patch.object(slc, 'available_years', return_value=[2026, 2025])
        p3 = mock.patch.object(slc, 'render_template', side_effect=lambda tpl, **ctx: (
            self.ctx.update(ctx) or 'OK'))
        self.ctx = {}
        self.build = p1.start()
        p2.start()
        p3.start()
        self.addCleanup(mock.patch.stopall)

    def login(self, **sess):
        with self.client.session_transaction() as s:
            s['user'] = {'email': 'u@x.ph'}
            s.update(sess)

    def test_no_session_redirects_to_login(self):
        r = self.client.get(self.URL)
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers['Location'].endswith('/login'))
        self.build.assert_not_called()

    def test_no_permission_redirects_to_dashboard(self):
        for sess in ({}, {'is_admin': False, 'permissions': {}},
                     {'permissions': {'can_leave': True}}, {'permissions': None}):
            self.client = self.app.test_client()
            self.login(**sess)
            r = self.client.get(self.URL)
            self.assertEqual(r.status_code, 302, sess)
            self.assertTrue(r.headers['Location'].endswith('/dashboard'), sess)
        self.build.assert_not_called()

    def test_allowed_roles(self):
        for sess in ({'is_admin': True}, {'permissions': {'can_entitlements': True}},
                     {'permissions': {'can_reports': True}}):
            self.client = self.app.test_client()
            self.login(**sess)
            self.assertEqual(self.client.get(self.URL).status_code, 200, sess)

    def test_year_param_validation(self):
        this = date.today().year
        self.login(is_admin=True)
        for raw, want in (('2025', 2025), (str(this + 1), this + 1), ('abc', this), ('', this),
                          ('1999', this), (str(this + 2), this), ('2026.5', this), ('-1', this)):
            self.client.get(self.URL, query_string={'year': raw})
            self.assertEqual(self.build.call_args[0][0], want, raw)
            self.assertEqual(self.ctx['year'], want, raw)

    def test_bad_status_falls_back_to_all(self):
        self.login(is_admin=True)
        self.client.get(self.URL, query_string={'status': "x' OR 1=1"})
        self.assertEqual(self.ctx['status'], 'all')
        self.assertEqual(len(self.ctx['rows']), 3)

    def test_filters_and_summary_scope(self):
        self.login(is_admin=True)
        self.client.get(self.URL, query_string={'group': 'cs', 'status': 'qualified'})
        self.assertEqual([r['name'] for r in self.ctx['rows']], ['Ann Q'])
        self.assertEqual(self.ctx['summary']['total'], 2)   # summary = group scope
        self.assertEqual(self.ctx['total'], 3)
        self.assertEqual(self.ctx['groups'], ['CS', 'L2'])

    def test_search_special_chars_no_crash(self):
        self.login(is_admin=True)
        for q in ("'; DROP", '100%', 'Ñoño', 'x' * 500):
            self.assertEqual(self.client.get(self.URL, query_string={'q': q}).status_code, 200)
            self.assertLessEqual(len(self.ctx['q']), 100)

    def test_year_added_to_dropdown_when_missing(self):
        self.login(is_admin=True)
        self.client.get(self.URL, query_string={'year': '2024'})
        self.assertEqual(self.ctx['years'], [2026, 2025, 2024])

    def read_csv(self, **qs):
        self.login(is_admin=True)
        r = self.client.get(self.URL, query_string=dict(qs, format='csv'))
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.mimetype.startswith('text/csv'))
        return r, list(csv.reader(io.StringIO(r.get_data(as_text=True))))

    def test_csv_all(self):
        r, data = self.read_csv(year='2026')
        self.assertIn('sl_conversion_2026.csv', r.headers['Content-Disposition'])
        self.assertEqual(data[0][0], 'Employee ID')
        self.assertEqual(len(data), 4)
        ann = next(row for row in data if row[0] == 'E1')
        self.assertEqual(ann[6:12], ['96.00', '12.00', '100', '96.00', '12.00', 'Qualified'])

    def test_csv_honours_filters(self):
        _, data = self.read_csv(status='qualified')
        self.assertEqual([row[0] for row in data[1:]], ['E1'])
        _, data = self.read_csv(group='L2')
        self.assertEqual([row[0] for row in data[1:]], ['E3'])
        _, data = self.read_csv(q='ben')
        self.assertEqual([row[0] for row in data[1:]], ['E2'])

    def test_csv_requires_permission(self):
        r = self.client.get(self.URL, query_string={'format': 'csv'})
        self.assertEqual(r.status_code, 302)

    def test_empty_report_renders(self):
        self.build.side_effect = lambda y, t: []
        self.login(is_admin=True)
        self.assertEqual(self.client.get(self.URL).status_code, 200)
        self.assertEqual((self.ctx['rows'], self.ctx['total'], self.ctx['summary']['total']), ([], 0, 0))


class ReviewFixTests(unittest.TestCase):
    """Fixes from the reviewer/security pass."""
    BASE = {'unused_days': 12, 'has_master': True, 'status': 'Active',
            'operational_date': date(2024, 1, 1)}

    def test_exit_before_promotion_is_internal_move(self):
        emp = dict(self.BASE, exit_date=date(2025, 6, 1), promotion_date=date(2025, 6, 2))
        self.assertEqual(evaluate(emp, [], [], 2026, date(2026, 11, 9))['status'], 'qualified')

    def test_exit_after_promotion_still_fails(self):
        emp = dict(self.BASE, exit_date=date(2026, 12, 1), promotion_date=date(2025, 6, 2))
        self.assertEqual(evaluate(emp, [], [], 2026, date(2026, 11, 9))['status'], 'not_qualified')

    def test_zero_dates_do_not_crash(self):
        emp = dict(self.BASE, operational_date='0000-00-00', exit_date='0000-00-00')
        r = evaluate(emp, [], [], 2026, date(2026, 11, 9))
        self.assertEqual(r['status'], 'not_qualified')       # no operational date
        self.assertTrue(any('Unreadable exit date' in x for x in r['review']))

    def test_datetime_operational_date(self):
        emp = dict(self.BASE, operational_date=datetime(2024, 1, 1, 0, 0))
        self.assertEqual(evaluate(emp, [], [], 2026, date(2026, 11, 9))['status'], 'qualified')

    def test_csv_formula_cells_escaped(self):
        self.assertEqual(slc._cell('=HYPERLINK("x")'), '\'=HYPERLINK("x")')
        self.assertEqual(slc._cell('@SUM(1)'), "'@SUM(1)")
        self.assertEqual(slc._cell('-1+2'), "'-1+2")
        self.assertEqual(slc._cell('Juan'), 'Juan')
        self.assertEqual(slc._cell(None), '')


if __name__ == '__main__':
    unittest.main()
