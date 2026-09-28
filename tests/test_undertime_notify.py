"""
Mocked/offline tests for the undertime auto-IR cron:
  * modules/undertime.py      -> get_undertime_for_date() (+ helpers)
  * undertime_notify.py       -> rule engine, state, dry-run/live, email fan-out

Run with:
    ./venv/bin/python -m unittest tests.test_undertime_notify -v
from the repo root.

Design notes
------------
* Same spirit as tests/test_undertime_report.py: fake cursors route
  fetchall()/fetchone() results by which table name appears in the executed
  query text. No real DB, no SMTP, no network.
* The fake central_db cursor emulates the WHERE clauses that matter
  (schedule_date range, FTS date range + status) so the tests don't rely on
  rows the real SQL would never return. Raw punches are returned for the
  requested personids WITHOUT the date-range filter, on purpose, so the
  Python-side OUT window / IN window logic is what's actually under test.
* Mocked module boundaries:
    modules.undertime: get_db_connection, get_orangehrm_connection,
                       get_cws_moves_for_range
    undertime_notify:  get_undertime_for_date, get_db_connection,
                       file_incident_report, send_email / smtplib
* undertime_notify loads /var/www/html/leavesystem/.env at import; recipient
  lists are patched per-test so .env contents never influence assertions.
* undertime_notify.py is never executed as __main__ and nothing runs --live.
"""
import sys
import unittest
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch

sys.path.insert(0, '/var/www/html/leavesystem')

import modules.undertime as ut  # noqa: E402
import undertime_notify as un  # noqa: E402


# ──────────────────────────────────────────────────────────────────
# Fakes
# ──────────────────────────────────────────────────────────────────
class FakeCentralCursor:
    """
    Routes by table name in query text. Checked in a fixed order because the
    schedules query also mentions userdata/gsheet_employees.
    """

    def __init__(self, schedules=None, synth_users=None, punches=None, fts=None,
                 state_rows=None, tl_rows=None):
        self.schedules = schedules or []
        self.synth_users = synth_users or []
        self.punches = punches or []
        self.fts = fts or []
        self.state_rows = state_rows or []
        self.tl_rows = tl_rows or []
        self.queries = []
        self._rows = []
        self.closed = False
        self.execute = MagicMock(side_effect=self._execute)

    def _execute(self, query, params=None):
        self.queries.append((query, params))
        q = query.lower()
        params = tuple(params or ())
        if 'insert into undertime_cycle_state' in q:
            self._rows = []
        elif 'undertime_cycle_state' in q:
            eid, ps = params
            self._rows = [dict(r) for r in self.state_rows
                          if r['employee_id'] == eid and r['period_start'] == ps]
        elif 'tl_view_map' in q:
            self._rows = list(self.tl_rows)
        elif 'employee_schedules' in q:
            lo, hi = params
            self._rows = [r for r in self.schedules if lo <= r['schedule_date'] <= hi]
        elif 'dailytimerecord' in q:
            pids = set(params[:-2])
            rows = [r for r in self.punches if r['personid'] in pids]
            self._rows = sorted(rows, key=lambda r: (r['personid'], r['punch_time']))
        elif 'fts_requests' in q:
            n_status = len(ut.FTS_ACTIVE_STATUSES)
            statuses = set(params[-n_status:])
            d_from, d_to = params[-n_status - 2], params[-n_status - 1]
            eids = {e.upper() for e in params[:-n_status - 2]}
            self.fts_date_range = (d_from, d_to)
            self._rows = [r for r in self.fts
                          if r['employee_id'].upper() in eids and d_from <= r['fts_date'] <= d_to
                          and r.get('status', 'Approved') in statuses
                          and r.get('deleted_at') is None]
        elif 'userdata' in q:
            ids = set(params)
            self._rows = [r for r in self.synth_users if r['employee_id'] in ids]
        else:
            self._rows = []

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self):
        self.closed = True


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.commit = MagicMock()
        self.rollback = MagicMock()
        self.closed = False

    def cursor(self):
        return self._cursor

    def close(self):
        self.closed = True


class FakeLeaveCursor:
    """orangehrm2: emulates `WHERE ol.date = %s AND ol.status IN (1,2,3)`."""

    def __init__(self, leaves):
        self.leaves = leaves
        self._rows = []

    def execute(self, query, params=None):
        assert 'ohrm_leave' in query
        (d,) = params
        self._rows = [{'employee_id': r['employee_id']} for r in self.leaves
                      if r['date'] == d and r['status'] in (1, 2, 3)]

    def fetchall(self):
        return list(self._rows)

    def close(self):
        pass


# ──────────────────────────────────────────────────────────────────
# Fixture builders
# ──────────────────────────────────────────────────────────────────
D = date(2026, 9, 20)
D1 = date(2026, 9, 21)
D2 = date(2026, 9, 22)
NOW = datetime(2026, 9, 25, 12, 0)  # well past every shift's settle point


def sched(eid, d, shift_time, personid=None, rest=0):
    return {
        'employee_id': eid, 'schedule_date': d, 'shift_time': shift_time,
        'is_rest_day': rest, 'personid': personid or f'P{eid}',
        'fname': f'F{eid}', 'lname': f'L{eid}', 'team_lead': 'TL One',
        'department': 'Ops', 'batch': 'B1', 'account': 'Acct', 'email': f'{eid}@x.com',
    }


def p(pid, dt, typ):
    return {'personid': pid, 'punch_time': dt, 'type': typ}


def fts(eid, d, hh, mm, typ, status='Approved', deleted_at=None):
    return {'employee_id': eid, 'fts_date': d, 'fts_time': timedelta(hours=hh, minutes=mm),
            'fts_type': typ, 'status': status, 'deleted_at': deleted_at}


def dt(d, h, m=0, s=0):
    return datetime(d.year, d.month, d.day, h, m, s)


class UndertimeCase(unittest.TestCase):
    """Runs get_undertime_for_date() against fakes."""

    def run_ut(self, schedules, punches=(), fts_rows=(), leaves=(), cws=({}, {}),
               synth_users=(), target=D, now=NOW):
        self.cur = FakeCentralCursor(schedules=list(schedules), punches=list(punches),
                                     fts=list(fts_rows), synth_users=list(synth_users))
        self.conn = FakeConn(self.cur)
        leave_conn = MagicMock()
        leave_conn.cursor.return_value = FakeLeaveCursor(list(leaves))
        with patch.object(ut, 'get_db_connection', return_value=self.conn), \
             patch.object(ut, 'get_orangehrm_connection', return_value=leave_conn), \
             patch.object(ut, 'get_cws_moves_for_range', return_value=cws):
            res = ut.get_undertime_for_date(target, now=now)
        return {r['companyid']: r for r in res}


# ──────────────────────────────────────────────────────────────────
# 1. Basic day-shift minute math
# ──────────────────────────────────────────────────────────────────
class TestUndertimeMinuteMath(UndertimeCase):

    def test_get_undertime_early_out_floors_minutes(self):
        # 3pm-12mn: end = 9/21 00:00. OUT 20:31:30 -> 208.5 min -> 208.
        r = self.run_ut([sched('E1', D, '3pm-12mn')],
                        [p('PE1', dt(D, 14, 55), 'in'), p('PE1', dt(D, 20, 31, 30), 'out')])['E1']
        self.assertEqual(r['status'], 'UNDERTIME')
        self.assertEqual(r['minutes_early'], 208)
        self.assertEqual(r['out_source'], 'PUNCH')
        self.assertEqual(r['time_in'], dt(D, 14, 55))
        self.assertEqual(r['time_out'], dt(D, 20, 31, 30))
        self.assertEqual(r['shift_end'], dt(D1, 0, 0))
        self.assertTrue(self.cur.closed and self.conn.closed)

    def test_get_undertime_out_exactly_at_end_is_on_time(self):
        r = self.run_ut([sched('E1', D, '3pm-12mn')],
                        [p('PE1', dt(D, 14, 55), 'in'), p('PE1', dt(D1, 0, 0), 'out')])['E1']
        self.assertEqual(r['status'], 'ON_TIME')
        self.assertEqual(r['minutes_early'], 0)

    def test_get_undertime_out_after_end_is_on_time(self):
        r = self.run_ut([sched('E1', D, '9am-6pm')],
                        [p('PE1', dt(D, 8, 50), 'in'), p('PE1', dt(D, 18, 42), 'out')])['E1']
        self.assertEqual(r['status'], 'ON_TIME')
        self.assertEqual(r['minutes_early'], 0)

    def test_get_undertime_59_seconds_early_is_on_time(self):
        r = self.run_ut([sched('E1', D, '9am-6pm')],
                        [p('PE1', dt(D, 8, 50), 'in'), p('PE1', dt(D, 17, 59, 1), 'out')])['E1']
        self.assertEqual(r['status'], 'ON_TIME')
        self.assertEqual(r['minutes_early'], 0)

    def test_get_undertime_exactly_one_minute_early_is_undertime(self):
        r = self.run_ut([sched('E1', D, '9am-6pm')],
                        [p('PE1', dt(D, 8, 50), 'in'), p('PE1', dt(D, 17, 59, 0), 'out')])['E1']
        self.assertEqual(r['status'], 'UNDERTIME')
        self.assertEqual(r['minutes_early'], 1)

    def test_get_undertime_no_schedules_returns_empty_without_punch_query(self):
        res = self.run_ut([])
        self.assertEqual(res, {})
        self.assertFalse(any('dailytimerecord' in q.lower() for q, _ in self.cur.queries))

    def test_get_undertime_queries_are_parameterized(self):
        self.run_ut([sched('E1', D, '9am-6pm')],
                    [p('PE1', dt(D, 8, 50), 'in'), p('PE1', dt(D, 17, 0), 'out')])
        for q, params in self.cur.queries:
            self.assertNotIn('PE1', q)
            self.assertNotIn("'E1'", q)
        punch_q = [(q, prm) for q, prm in self.cur.queries if 'dailytimerecord' in q.lower()][0]
        self.assertIn('PE1', punch_q[1])


# ──────────────────────────────────────────────────────────────────
# 2. Overnight shift
# ──────────────────────────────────────────────────────────────────
class TestUndertimeOvernight(UndertimeCase):

    def test_overnight_next_morning_out_attributed_to_shift(self):
        r = self.run_ut([sched('E1', D, '8pm-5am')],
                        [p('PE1', dt(D, 19, 55), 'in'), p('PE1', dt(D1, 4, 22), 'out')])['E1']
        self.assertEqual(r['status'], 'UNDERTIME')
        self.assertEqual(r['minutes_early'], 38)
        self.assertEqual(r['time_out'], dt(D1, 4, 22))

    def test_overnight_orphan_midnight_out_before_in_is_ignored(self):
        # 9/20 00:00 OUT belongs to the previous night's shift.
        r = self.run_ut([sched('E1', D, '8pm-5am')],
                        [p('PE1', dt(D, 0, 0), 'out'), p('PE1', dt(D, 19, 58), 'in'),
                         p('PE1', dt(D1, 5, 3), 'out')])['E1']
        self.assertEqual(r['status'], 'ON_TIME')
        self.assertEqual(r['time_out'], dt(D1, 5, 3))

    def test_overnight_orphan_out_only_gives_no_out(self):
        r = self.run_ut([sched('E1', D, '8pm-5am')],
                        [p('PE1', dt(D, 0, 0), 'out'), p('PE1', dt(D, 19, 58), 'in')])['E1']
        self.assertEqual(r['status'], 'NO_OUT')
        self.assertEqual(r['time_in'], dt(D, 19, 58))

    def test_overnight_next_day_shift_ignores_this_mornings_out(self):
        # Evaluating 9/21: the 9/21 04:22 OUT belongs to 9/20's shift.
        scheds = [sched('E1', D, '8pm-5am'), sched('E1', D1, '8pm-5am'), sched('E1', D2, '8pm-5am')]
        punches = [p('PE1', dt(D, 19, 55), 'in'), p('PE1', dt(D1, 4, 22), 'out'),
                   p('PE1', dt(D1, 19, 58), 'in'), p('PE1', dt(D2, 5, 0), 'out')]
        r = self.run_ut(scheds, punches, target=D1)['E1']
        self.assertEqual(r['status'], 'ON_TIME')
        self.assertEqual(r['time_in'], dt(D1, 19, 58))
        self.assertEqual(r['time_out'], dt(D2, 5, 0))


# ──────────────────────────────────────────────────────────────────
# 3. Early-morning same-day shift
# ──────────────────────────────────────────────────────────────────
class TestUndertimeSameDayEarlyShift(UndertimeCase):

    def test_3am_2pm_is_same_day_shift(self):
        r = self.run_ut([sched('E1', D, '3am-2pm')],
                        [p('PE1', dt(D, 2, 50), 'in'), p('PE1', dt(D, 13, 45), 'out')])['E1']
        self.assertEqual(r['shift_start'], dt(D, 3))
        self.assertEqual(r['shift_end'], dt(D, 14))
        self.assertEqual(r['status'], 'UNDERTIME')
        self.assertEqual(r['minutes_early'], 15)


# ──────────────────────────────────────────────────────────────────
# 4. Last OUT wins / window capped at next shift
# ──────────────────────────────────────────────────────────────────
class TestUndertimeOutSelection(UndertimeCase):

    def test_last_out_wins_after_mid_shift_out_in(self):
        r = self.run_ut([sched('E1', D, '9am-6pm')],
                        [p('PE1', dt(D, 8, 55), 'in'), p('PE1', dt(D, 12, 0), 'out'),
                         p('PE1', dt(D, 13, 0), 'in'), p('PE1', dt(D, 17, 50), 'out')])['E1']
        self.assertEqual(r['status'], 'UNDERTIME')
        self.assertEqual(r['minutes_early'], 10)
        self.assertEqual(r['time_in'], dt(D, 8, 55))  # first IN is the anchor
        self.assertEqual(r['time_out'], dt(D, 17, 50))

    def test_double_tap_out_uses_last(self):
        r = self.run_ut([sched('E1', D, '9am-6pm')],
                        [p('PE1', dt(D, 8, 55), 'in'), p('PE1', dt(D, 17, 30), 'out'),
                         p('PE1', dt(D, 17, 30, 20), 'out')])['E1']
        self.assertEqual(r['time_out'], dt(D, 17, 30, 20))
        self.assertEqual(r['minutes_early'], 29)

    def test_out_window_capped_at_next_shift_start(self):
        # 9/20 2pm-10pm; 9/21 1am-3am -> OUT window ends at
        # max(22:00 + 1h, min(04:00, 01:00)) = 23:00.
        # Without the cap the next shift's 03:00 OUT would be "last OUT".
        scheds = [sched('E1', D, '2pm-10pm'), sched('E1', D1, '1am-3am')]
        punches = [p('PE1', dt(D, 13, 55), 'in'), p('PE1', dt(D, 21, 30), 'out'),
                   p('PE1', dt(D1, 0, 55), 'in'), p('PE1', dt(D1, 3, 0), 'out')]
        r = self.run_ut(scheds, punches)['E1']
        self.assertEqual(r['status'], 'UNDERTIME')
        self.assertEqual(r['minutes_early'], 30)
        self.assertEqual(r['time_out'], dt(D, 21, 30))

    def test_out_window_not_capped_when_next_day_is_rest_day(self):
        scheds = [sched('E1', D, '2pm-10pm'), sched('E1', D1, '1am-3am', rest=1)]
        punches = [p('PE1', dt(D, 13, 55), 'in'), p('PE1', dt(D, 21, 30), 'out'),
                   p('PE1', dt(D1, 3, 0), 'out')]
        r = self.run_ut(scheds, punches)['E1']
        self.assertEqual(r['status'], 'ON_TIME')
        self.assertEqual(r['time_out'], dt(D1, 3, 0))

    def test_out_beyond_six_hours_after_end_ignored(self):
        r = self.run_ut([sched('E1', D, '9am-6pm')],
                        [p('PE1', dt(D, 8, 55), 'in'), p('PE1', dt(D, 17, 0), 'out'),
                         p('PE1', dt(D1, 0, 1), 'out')])['E1']
        self.assertEqual(r['time_out'], dt(D, 17, 0))
        self.assertEqual(r['minutes_early'], 60)


# ──────────────────────────────────────────────────────────────────
# 5. FTS handling, NO_IN / NO_OUT
# ──────────────────────────────────────────────────────────────────
class TestUndertimeFts(UndertimeCase):

    def test_fts_out_overrides_punch_out_approved_and_pending(self):
        for status in ('Approved', 'Pending'):
            with self.subTest(status=status):
                r = self.run_ut([sched('E1', D, '9am-6pm')],
                                [p('PE1', dt(D, 8, 55), 'in'), p('PE1', dt(D, 18, 5), 'out')],
                                [fts('E1', D, 17, 40, 'OUT', status=status)])['E1']
                self.assertEqual(r['status'], 'UNDERTIME')
                self.assertEqual(r['minutes_early'], 20)
                self.assertEqual(r['out_source'], 'FTS')
                self.assertEqual(r['time_out'], dt(D, 17, 40))

    def test_fts_out_rejected_or_deleted_does_not_override(self):
        for row in (fts('E1', D, 17, 40, 'OUT', status='Rejected'),
                    fts('E1', D, 17, 40, 'OUT', deleted_at=datetime(2026, 9, 21))):
            with self.subTest(row=row):
                r = self.run_ut([sched('E1', D, '9am-6pm')],
                                [p('PE1', dt(D, 8, 55), 'in'), p('PE1', dt(D, 18, 5), 'out')],
                                [row])['E1']
                self.assertEqual(r['status'], 'ON_TIME')
                self.assertEqual(r['out_source'], 'PUNCH')

    def test_fts_out_overnight_filed_on_next_calendar_day(self):
        r = self.run_ut([sched('E1', D, '8pm-5am')],
                        [p('PE1', dt(D, 19, 55), 'in'), p('PE1', dt(D1, 5, 10), 'out')],
                        [fts('E1', D1, 4, 30, 'OUT')])['E1']
        self.assertEqual(r['status'], 'UNDERTIME')
        self.assertEqual(r['minutes_early'], 30)
        self.assertEqual(r['out_source'], 'FTS')
        self.assertEqual(r['time_out'], dt(D1, 4, 30))

    def test_fts_out_only_no_punch_out(self):
        r = self.run_ut([sched('E1', D, '9am-6pm')],
                        [p('PE1', dt(D, 8, 55), 'in')],
                        [fts('E1', D, 17, 0, 'OUT')])['E1']
        self.assertEqual(r['status'], 'UNDERTIME')
        self.assertEqual(r['minutes_early'], 60)
        self.assertEqual(r['out_source'], 'FTS')

    def test_fts_in_used_as_anchor_when_no_raw_in(self):
        r = self.run_ut([sched('E1', D, '9am-6pm')],
                        [p('PE1', dt(D, 17, 30), 'out')],
                        [fts('E1', D, 8, 58, 'IN')])['E1']
        self.assertEqual(r['status'], 'UNDERTIME')
        self.assertEqual(r['time_in'], dt(D, 8, 58))
        self.assertEqual(r['minutes_early'], 30)
        self.assertEqual(r['out_source'], 'PUNCH')

    def test_raw_in_preferred_over_fts_in(self):
        r = self.run_ut([sched('E1', D, '9am-6pm')],
                        [p('PE1', dt(D, 8, 40), 'in'), p('PE1', dt(D, 17, 30), 'out')],
                        [fts('E1', D, 8, 58, 'IN')])['E1']
        self.assertEqual(r['time_in'], dt(D, 8, 40))

    def test_no_in_at_all_is_no_in(self):
        r = self.run_ut([sched('E1', D, '9am-6pm')],
                        [p('PE1', dt(D, 17, 30), 'out')])['E1']
        self.assertEqual(r['status'], 'NO_IN')
        self.assertIsNone(r['minutes_early'])
        self.assertIsNone(r['time_out'])

    def test_in_outside_window_is_no_in(self):
        # IN more than 4h before start -> not an anchor.
        r = self.run_ut([sched('E1', D, '9am-6pm')],
                        [p('PE1', dt(D, 4, 59), 'in'), p('PE1', dt(D, 17, 30), 'out')])['E1']
        self.assertEqual(r['status'], 'NO_IN')

    def test_in_but_no_out_is_no_out(self):
        r = self.run_ut([sched('E1', D, '9am-6pm')],
                        [p('PE1', dt(D, 8, 55), 'in')])['E1']
        self.assertEqual(r['status'], 'NO_OUT')
        self.assertEqual(r['time_in'], dt(D, 8, 55))
        self.assertIsNone(r['minutes_early'])


# ──────────────────────────────────────────────────────────────────
# 6. Exclusions / settle
# ──────────────────────────────────────────────────────────────────
class TestUndertimeExclusions(UndertimeCase):

    PUNCHES = [p('PE1', dt(D, 8, 55), 'in'), p('PE1', dt(D, 17, 0), 'out')]

    def test_leave_status_1_2_3_excludes(self):
        for status in (1, 2, 3):
            with self.subTest(status=status):
                res = self.run_ut([sched('E1', D, '9am-6pm')], self.PUNCHES,
                                  leaves=[{'employee_id': 'E1', 'date': D, 'status': status}])
                self.assertNotIn('E1', res)

    def test_rejected_leave_or_other_day_does_not_exclude(self):
        for leave in ({'employee_id': 'E1', 'date': D, 'status': -1},
                      {'employee_id': 'E1', 'date': D1, 'status': 3}):
            with self.subTest(leave=leave):
                res = self.run_ut([sched('E1', D, '9am-6pm')], self.PUNCHES, leaves=[leave])
                self.assertEqual(res['E1']['status'], 'UNDERTIME')

    def test_rest_day_excluded(self):
        res = self.run_ut([sched('E1', D, '9am-6pm', rest=1)], self.PUNCHES)
        self.assertEqual(res, {})

    def test_unparseable_shift_excluded(self):
        for bad in ('#REF!', '', None, 'OFF', '9am'):
            with self.subTest(shift=bad):
                res = self.run_ut([sched('E1', D, bad)], self.PUNCHES)
                self.assertNotIn('E1', res)

    def test_not_settled_before_end_plus_6h(self):
        # SETTLE_AFTER_END == OUT_WINDOW_AFTER_END (6h): end 18:00 -> settles at 00:00.
        r = self.run_ut([sched('E1', D, '9am-6pm')], self.PUNCHES,
                        now=dt(D, 23, 59, 59))['E1']
        self.assertEqual(r['status'], 'NOT_SETTLED')
        self.assertIsNone(r['minutes_early'])

    def test_settled_exactly_at_end_plus_6h(self):
        r = self.run_ut([sched('E1', D, '9am-6pm')], self.PUNCHES, now=dt(D1, 0, 0))['E1']
        self.assertEqual(r['status'], 'UNDERTIME')

    def test_cws_moved_out_is_dropped_and_moved_in_overrides_rest_day(self):
        scheds = [sched('E1', D, '9am-6pm'), sched('E2', D, '9am-6pm', rest=1)]
        punches = self.PUNCHES + [p('PE2', dt(D, 9, 55), 'in'), p('PE2', dt(D, 18, 0), 'out')]
        res = self.run_ut(scheds, punches, cws=({D: {'E1'}}, {D: {'E2': '10am-7pm'}}))
        self.assertNotIn('E1', res)
        self.assertEqual(res['E2']['shift_time_raw'], '10am-7pm')
        self.assertEqual(res['E2']['status'], 'UNDERTIME')
        self.assertEqual(res['E2']['minutes_early'], 60)

    def test_cws_moved_in_without_schedule_row_is_synthesized(self):
        synth = {'employee_id': 'E3', 'personid': 'PE3', 'fname': 'F3', 'lname': 'L3',
                 'team_lead': 'TL', 'department': 'Ops', 'batch': None, 'account': None,
                 'email': None}
        res = self.run_ut([], [p('PE3', dt(D, 8, 55), 'in'), p('PE3', dt(D, 17, 45), 'out')],
                          cws=({}, {D: {'E3': '9am-6pm'}}), synth_users=[synth])
        self.assertEqual(res['E3']['status'], 'UNDERTIME')
        self.assertEqual(res['E3']['minutes_early'], 15)


# ──────────────────────────────────────────────────────────────────
# Post-review regressions (_final_out walk, window cap, FTS fetch)
# ──────────────────────────────────────────────────────────────────
D0 = date(2026, 9, 19)


class TestUndertimePostReviewRegressions(UndertimeCase):

    def test_rotation_10pm_7am_then_10am_7pm_double_tap_is_on_time(self):
        # Window = max(07:00 + 1h, min(13:00, next START 10:00)) = 10:00, so the
        # normal 07:02 OUT is seen and the stray 22:05 OUT is not the final one.
        scheds = [sched('E1', D, '10pm-7am'), sched('E1', D1, '10am-7pm')]
        punches = [p('PE1', dt(D, 21, 55), 'in'), p('PE1', dt(D, 22, 5), 'out'),
                   p('PE1', dt(D1, 7, 2), 'out'),
                   p('PE1', dt(D1, 9, 55), 'in'), p('PE1', dt(D1, 19, 1), 'out')]
        r = self.run_ut(scheds, punches)['E1']
        self.assertEqual(r['status'], 'ON_TIME')
        self.assertEqual(r['time_out'], dt(D1, 7, 2))
        self.assertEqual(r['minutes_early'], 0)

    def test_break_out_in_without_final_out_is_no_out(self):
        r = self.run_ut([sched('E1', D, '8am-5pm')],
                        [p('PE1', dt(D, 7, 55), 'in'), p('PE1', dt(D, 12, 0), 'out'),
                         p('PE1', dt(D, 13, 0), 'in')])['E1']
        self.assertEqual(r['status'], 'NO_OUT')
        self.assertIsNone(r['time_out'])
        self.assertEqual(r['time_in'], dt(D, 7, 55))

    def test_break_out_in_then_final_out_is_undertime_30(self):
        r = self.run_ut([sched('E1', D, '8am-5pm')],
                        [p('PE1', dt(D, 7, 55), 'in'), p('PE1', dt(D, 12, 0), 'out'),
                         p('PE1', dt(D, 13, 0), 'in'), p('PE1', dt(D, 16, 30), 'out')])['E1']
        self.assertEqual(r['status'], 'UNDERTIME')
        self.assertEqual(r['minutes_early'], 30)
        self.assertEqual(r['time_out'], dt(D, 16, 30))

    def test_early_in_for_next_shift_ends_walk(self):
        # 2pm-11pm then 5am-2pm: window = max(00:00, min(05:00, 05:00)) = 05:00.
        # The 04:30 IN (>= shift_end) belongs to the next shift and ends the
        # walk, so the 04:45 OUT after it isn't used.
        scheds = [sched('E1', D, '2pm-11pm'), sched('E1', D1, '5am-2pm')]
        punches = [p('PE1', dt(D, 13, 55), 'in'), p('PE1', dt(D, 22, 0), 'out'),
                   p('PE1', dt(D1, 4, 30), 'in'), p('PE1', dt(D1, 4, 45), 'out')]
        r = self.run_ut(scheds, punches)['E1']
        self.assertEqual(r['status'], 'UNDERTIME')
        self.assertEqual(r['minutes_early'], 60)
        self.assertEqual(r['time_out'], dt(D, 22, 0))

    def test_final_out_helper_directly(self):
        end = dt(D, 17)
        ins = [dt(D, 8), dt(D, 13), dt(D, 17, 30)]
        outs = [dt(D, 12), dt(D, 16), dt(D, 18)]
        # IN at 17:30 (>= end) stops the walk -> 16:00, not 18:00.
        self.assertEqual(ut._final_out(ins, outs, dt(D, 8), end, dt(D, 23)), dt(D, 16))
        # break with no return OUT -> None
        self.assertIsNone(ut._final_out([dt(D, 8), dt(D, 13)], [dt(D, 12)], dt(D, 8), end, dt(D, 23)))
        # nothing after time_in -> None
        self.assertIsNone(ut._final_out([dt(D, 8)], [], dt(D, 8), end, dt(D, 23)))
        # OUT beyond window_end ignored
        self.assertIsNone(ut._final_out([dt(D, 8)], [dt(D, 23, 1)], dt(D, 8), end, dt(D, 23)))

    def test_pre_4am_shift_uses_fts_in_from_previous_day(self):
        # 2am-11am on 9/20: IN window starts 9/19 22:00; FTS IN filed 9/19 23:30.
        r = self.run_ut([sched('E1', D, '2am-11am')],
                        [p('PE1', dt(D, 9, 0), 'out')],
                        [fts('E1', D0, 23, 30, 'IN')])['E1']
        self.assertEqual(r['status'], 'UNDERTIME')
        self.assertEqual(r['time_in'], dt(D0, 23, 30))
        self.assertEqual(r['minutes_early'], 120)
        self.assertEqual(self.cur.fts_date_range, (D0, D1))

    def test_fts_row_with_null_time_is_ignored(self):
        r = self.run_ut([sched('E1', D, '9am-6pm')],
                        [p('PE1', dt(D, 8, 55), 'in'), p('PE1', dt(D, 18, 5), 'out')],
                        [dict(fts('E1', D, 0, 0, 'OUT'), fts_time=None)])['E1']
        self.assertEqual(r['status'], 'ON_TIME')
        self.assertEqual(r['out_source'], 'PUNCH')

    def test_fts_employee_id_and_type_case_insensitive(self):
        r = self.run_ut([sched('E1', D, '9am-6pm')],
                        [p('PE1', dt(D, 8, 55), 'in'), p('PE1', dt(D, 18, 5), 'out')],
                        [fts('e1', D, 17, 40, 'out')])['E1']
        self.assertEqual(r['status'], 'UNDERTIME')
        self.assertEqual(r['out_source'], 'FTS')
        self.assertEqual(r['minutes_early'], 20)

    def test_uppercase_punch_types_are_handled(self):
        r = self.run_ut([sched('E1', D, '9am-6pm')],
                        [p('PE1', dt(D, 8, 55), 'IN'), p('PE1', dt(D, 17, 45), 'OUT')])['E1']
        self.assertEqual(r['status'], 'UNDERTIME')
        self.assertEqual(r['time_in'], dt(D, 8, 55))
        self.assertEqual(r['minutes_early'], 15)


# ──────────────────────────────────────────────────────────────────
# undertime_notify helpers
# ──────────────────────────────────────────────────────────────────
PS, PE = date(2026, 9, 8), date(2026, 9, 22)
TL_ROWS = [{'login_email': 'tl@x.com'}]


def ut_record(companyid='E1', minutes=5, d=D, source='PUNCH'):
    end = dt(d, 18)
    return {
        'companyid': companyid, 'personid': f'P{companyid}', 'fname': 'Ana', 'lname': 'Cruz',
        'status': 'UNDERTIME', 'minutes_early': minutes, 'time_in': dt(d, 8, 55),
        'time_out': end - timedelta(minutes=minutes), 'shift_end': end,
        'out_source': source, 'shift_time_raw': '9am-6pm',
    }


def state(**over):
    s = un._new_state('E1', PS)
    s.update(over)
    return s


class NotifyCase(unittest.TestCase):

    def setUp(self):
        patches = [
            patch.object(un, 'file_incident_report', MagicMock(return_value='IR-0001')),
            patch.object(un, 'send_email', MagicMock()),
            patch.object(un.smtplib, 'SMTP', MagicMock(side_effect=AssertionError('no SMTP'))),
            patch.object(un, 'STATIC_RECIPIENTS', ['TL@x.com', 'hr@x.com']),
            patch.object(un, 'BCC_RECIPIENTS', ['HR@X.COM', 'bcc@x.com']),
            # Fixtures use Sep 2026 dates before the real go-live; the
            # go-live guard itself is tested in TestGoLiveGuard.
            patch.object(un, 'LIVE_START_DATE', date.min),
        ]
        self.mocks = [pt.start() for pt in patches]
        for pt in patches:
            self.addCleanup(pt.stop)
        self.fir, self.send_email, self.smtp = self.mocks[0], self.mocks[1], self.mocks[2]
        self.cur = FakeCentralCursor(tl_rows=TL_ROWS)

    def apply(self, s, minutes, d=D, live=False):
        return un.apply_rules(self.cur, s, ut_record(minutes=minutes, d=d), d, PS, PE, live)


# ──────────────────────────────────────────────────────────────────
# 7. Rules
# ──────────────────────────────────────────────────────────────────
class TestNotifyRules(NotifyCase):

    def test_apply_rules_third_undertime_fires_count_and_one_ir(self):
        s = state()
        self.assertEqual(self.apply(s, 1, d=date(2026, 9, 10)), [])
        self.assertEqual(self.apply(s, 1, d=date(2026, 9, 11)), [])
        self.fir.assert_not_called()
        fired = self.apply(s, 1, d=date(2026, 9, 12))
        self.assertEqual([f[0] for f in fired], ['COUNT'])
        self.assertEqual(len(fired[0][1]), 3)
        self.assertEqual(self.fir.call_count, 1)
        self.assertEqual(s['count_since_reset'], 0)
        self.assertEqual(s['count_breakdown'], [])
        self.assertEqual(s['count_triggers_sent'], 1)
        self.assertEqual(s['total_count_in_cycle'], 3)
        self.assertEqual(s['minutes_since_reset'], 3)  # minutes counter untouched

    def test_apply_rules_31_minutes_fires_minutes(self):
        s = state()
        fired = self.apply(s, 31)
        self.assertEqual([f[0] for f in fired], ['MINUTES'])
        self.assertEqual(self.fir.call_count, 1)
        self.assertEqual(s['minutes_since_reset'], 0)
        self.assertEqual(s['minutes_triggers_sent'], 1)
        self.assertEqual(s['total_minutes_in_cycle'], 31)
        self.assertEqual(s['count_since_reset'], 1)

    def test_apply_rules_30_minutes_does_not_fire(self):
        s = state()
        self.assertEqual(self.apply(s, 30), [])
        self.fir.assert_not_called()

    def test_apply_rules_accumulated_minutes_fire(self):
        s = state()
        self.apply(s, 20, d=date(2026, 9, 10))
        fired = self.apply(s, 11, d=date(2026, 9, 11))
        self.assertEqual([f[0] for f in fired], ['MINUTES'])
        self.assertEqual([e['minutes'] for e in fired[0][1]], [20, 11])

    def test_apply_rules_both_same_day_files_exactly_one_ir(self):
        s = state(count_since_reset=2, minutes_since_reset=25, total_count_in_cycle=2,
                  total_minutes_in_cycle=25)
        fired = self.apply(s, 10)
        self.assertEqual([f[0] for f in fired], ['COUNT', 'MINUTES'])
        self.assertEqual(self.fir.call_count, 1)
        self.assertEqual(s['count_since_reset'], 0)
        self.assertEqual(s['minutes_since_reset'], 0)

    def test_apply_rules_second_ir_possible_in_same_cycle(self):
        s = state()
        for i in range(6):
            self.apply(s, 1, d=date(2026, 9, 10 + i))
        self.assertEqual(self.fir.call_count, 2)
        self.assertEqual(s['count_triggers_sent'], 2)
        self.assertEqual(s['total_count_in_cycle'], 6)

    def test_ir_summary_and_args(self):
        s = state(count_since_reset=2)
        self.apply(s, 7)
        args, kwargs = self.fir.call_args
        self.assertIs(args[0], self.cur)
        self.assertEqual(args[1], 'E1')
        self.assertEqual(args[2], 'Ana Cruz')
        self.assertEqual(args[3], D)
        self.assertIn('three instances of undertime', args[4])
        self.assertTrue(kwargs['dry_run'])

    def test_process_day_idempotency_guard_skips_already_processed(self):
        row = dict(state(last_processed_date=D, count_since_reset=2, minutes_since_reset=40),
                   count_breakdown='[]', minutes_breakdown='[]')
        cur = FakeCentralCursor(state_rows=[row], tl_rows=TL_ROWS)
        conn = FakeConn(cur)
        with patch.object(un, 'get_undertime_for_date', return_value=[ut_record(minutes=30)]), \
             patch.object(un, 'get_db_connection', return_value=conn), \
             patch.object(un, 'apply_rules', wraps=un.apply_rules) as ar:
            un.process_day(D, live=True)
        ar.assert_not_called()
        self.fir.assert_not_called()
        self.send_email.assert_not_called()
        self.assertFalse(any('insert' in q.lower() for q, _ in cur.queries))

    def test_process_day_idempotency_skips_when_last_processed_is_later(self):
        row = dict(state(last_processed_date=D1, count_since_reset=2, minutes_since_reset=40),
                   count_breakdown='[]', minutes_breakdown='[]')
        cur = FakeCentralCursor(state_rows=[row], tl_rows=TL_ROWS)
        conn = FakeConn(cur)
        with patch.object(un, 'get_undertime_for_date', return_value=[ut_record(minutes=30)]), \
             patch.object(un, 'get_db_connection', return_value=conn), \
             patch.object(un, 'apply_rules', wraps=un.apply_rules) as ar:
            un.process_day(D, live=True)
        ar.assert_not_called()
        self.fir.assert_not_called()
        conn.commit.assert_not_called()
        self.assertFalse(any('insert' in q.lower() for q, _ in cur.queries))

    def test_process_day_only_acts_on_undertime_status(self):
        recs = [dict(ut_record(companyid=c), status=st) for c, st in
                (('A', 'ON_TIME'), ('B', 'NO_IN'), ('C', 'NO_OUT'), ('D', 'NOT_SETTLED'))]
        cur = FakeCentralCursor(tl_rows=TL_ROWS)
        with patch.object(un, 'get_undertime_for_date', return_value=recs), \
             patch.object(un, 'get_db_connection', return_value=FakeConn(cur)):
            un.process_day(D, live=True)
        self.assertEqual(cur.queries, [])


# ──────────────────────────────────────────────────────────────────
# 8. Dry-run default vs live
# ──────────────────────────────────────────────────────────────────
class TestNotifyDryRunVsLive(NotifyCase):

    def _run(self, live, minutes=5, prior_count=2):
        row = dict(state(count_since_reset=prior_count, total_count_in_cycle=prior_count,
                         last_processed_date=date(2026, 9, 19)),
                   count_breakdown='[]', minutes_breakdown='[]')
        self.cur = FakeCentralCursor(state_rows=[row], tl_rows=TL_ROWS)
        self.conn = FakeConn(self.cur)
        with patch.object(un, 'get_undertime_for_date', return_value=[ut_record(minutes=minutes)]), \
             patch.object(un, 'get_db_connection', return_value=self.conn):
            un.process_day(D, live=live)

    def test_dry_run_writes_nothing_and_sends_nothing(self):
        self._run(live=False)
        self.assertFalse(any('insert' in q.lower() for q, _ in self.cur.queries))
        self.conn.commit.assert_not_called()
        self.conn.rollback.assert_called_once()
        self.assertEqual(self.fir.call_count, 1)
        self.assertTrue(self.fir.call_args.kwargs['dry_run'])
        self.send_email.assert_not_called()
        self.smtp.assert_not_called()
        self.assertTrue(self.conn.closed)

    def test_live_saves_commits_and_emails_each_recipient_once(self):
        self._run(live=True)
        inserts = [prm for q, prm in self.cur.queries if 'insert into undertime_cycle_state' in q.lower()]
        self.assertEqual(len(inserts), 1)
        self.assertEqual(inserts[0][:2], ('E1', PS))
        self.assertEqual(inserts[0][6], D)  # last_processed_date
        self.conn.commit.assert_called_once()
        self.conn.rollback.assert_not_called()
        self.assertFalse(self.fir.call_args.kwargs['dry_run'])
        # TL first, then static, then BCC; deduped case-insensitively.
        sent_to = [c.args[0] for c in self.send_email.call_args_list]
        self.assertEqual(sent_to, ['tl@x.com', 'hr@x.com', 'bcc@x.com'])
        for addr in sent_to:
            self.assertIsInstance(addr, str)
            self.assertNotIn(',', addr)
        subj = self.send_email.call_args_list[0].args[1]
        self.assertIn('3 Undertimes', subj)
        self.assertIn('IR-0001', self.send_email.call_args_list[0].args[2])

    def test_live_no_trigger_saves_but_sends_nothing(self):
        self._run(live=True, prior_count=0)
        self.assertEqual(sum('insert' in q.lower() for q, _ in self.cur.queries), 1)
        self.fir.assert_not_called()
        self.send_email.assert_not_called()

    def test_live_failure_for_one_employee_rolls_back_and_continues(self):
        cur = FakeCentralCursor(tl_rows=TL_ROWS)
        conn = FakeConn(cur)
        real_apply = un.apply_rules
        calls = []

        def flaky(cur_, s, r, *a, **kw):
            calls.append(r['companyid'])
            if r['companyid'] == 'E1':
                raise RuntimeError('boom')
            return real_apply(cur_, s, r, *a, **kw)

        recs = [ut_record(companyid='E1'), ut_record(companyid='E2')]
        with patch.object(un, 'get_undertime_for_date', return_value=recs), \
             patch.object(un, 'get_db_connection', return_value=conn), \
             patch.object(un, 'apply_rules', side_effect=flaky):
            un.process_day(D, live=True)
        self.assertEqual(calls, ['E1', 'E2'])
        conn.rollback.assert_called_once()
        conn.commit.assert_called_once()
        inserts = [prm for q, prm in cur.queries if 'insert into undertime_cycle_state' in q.lower()]
        self.assertEqual([prm[0] for prm in inserts], ['E2'])
        # rollback happened before E2's save+commit
        self.assertTrue(conn.closed)

    def test_live_commits_once_per_employee(self):
        cur = FakeCentralCursor(tl_rows=TL_ROWS)
        conn = FakeConn(cur)
        recs = [ut_record(companyid='E1'), ut_record(companyid='E2')]
        with patch.object(un, 'get_undertime_for_date', return_value=recs), \
             patch.object(un, 'get_db_connection', return_value=conn):
            un.process_day(D, live=True)
        self.assertEqual(conn.commit.call_count, 2)
        conn.rollback.assert_not_called()

    def test_dry_run_multiple_employees_never_commits(self):
        cur = FakeCentralCursor(tl_rows=TL_ROWS)
        conn = FakeConn(cur)
        recs = [ut_record(companyid='E1', minutes=40), ut_record(companyid='E2', minutes=40)]
        with patch.object(un, 'get_undertime_for_date', return_value=recs), \
             patch.object(un, 'get_db_connection', return_value=conn):
            un.process_day(D, live=False)
        conn.commit.assert_not_called()
        conn.rollback.assert_called_once()
        self.assertEqual(self.fir.call_count, 2)
        self.assertTrue(all(c.kwargs['dry_run'] for c in self.fir.call_args_list))
        self.send_email.assert_not_called()

    def test_build_recipient_list_without_tl(self):
        cur = FakeCentralCursor(tl_rows=[])
        recipients, tl = un.build_recipient_list(cur, 'E1')
        self.assertIsNone(tl)
        self.assertEqual(recipients, ['TL@x.com', 'hr@x.com', 'bcc@x.com'])
        q, params = cur.queries[0]
        self.assertEqual(params, ('E1',))

    def test_send_to_all_continues_after_one_failure(self):
        self.send_email.side_effect = [RuntimeError('boom'), None]
        un.send_to_all(['a@x.com', 'b@x.com'], 'S', 'B', live=True)
        self.assertEqual([c.args[0] for c in self.send_email.call_args_list], ['a@x.com', 'b@x.com'])

    def test_send_email_uses_single_recipient_envelope(self):
        smtp_inst = MagicMock()
        smtp_cls = MagicMock()
        smtp_cls.return_value.__enter__.return_value = smtp_inst
        # un.send_email is patched in setUp; call the real function directly.
        with patch.object(un.smtplib, 'SMTP', smtp_cls):
            NotifyCase.real_send_email('one@x.com', 'Subj', '<p>x</p>')
        smtp_inst.sendmail.assert_called_once()
        _, to_list, raw = smtp_inst.sendmail.call_args.args
        self.assertEqual(to_list, ['one@x.com'])
        self.assertIn('To: one@x.com', raw)


NotifyCase.real_send_email = staticmethod(un.send_email)


# ──────────────────────────────────────────────────────────────────
# 9. State serialization
# ──────────────────────────────────────────────────────────────────
class TestNotifyStateSerialization(unittest.TestCase):

    def test_breakdown_round_trip(self):
        bd = [
            {'date': date(2026, 9, 10), 'minutes': 12, 'time_out': '05:48 PM',
             'shift_end': '06:00 PM', 'out_source': 'PUNCH'},
            {'date': date(2026, 9, 12), 'minutes': 5, 'time_out': None,
             'shift_end': '06:00 PM', 'out_source': 'FTS'},
        ]
        self.assertEqual(un._deserialize_breakdown(un._serialize_breakdown(bd)), bd)

    def test_serialize_drops_unknown_keys(self):
        bd = [{'date': date(2026, 9, 10), 'minutes': 1, 'junk': 'x'}]
        out = un._deserialize_breakdown(un._serialize_breakdown(bd))
        self.assertNotIn('junk', out[0])
        self.assertEqual(out[0]['minutes'], 1)

    def test_deserialize_bad_input(self):
        for raw in (None, '', 'not json', '[{"minutes": 3}]', '[{"date": "bad"}]'):
            with self.subTest(raw=raw):
                self.assertEqual(un._deserialize_breakdown(raw), [])

    def test_get_or_create_state_deserializes_row(self):
        raw = un._serialize_breakdown([{'date': D, 'minutes': 4}])
        row = dict(state(count_since_reset=1), count_breakdown=raw, minutes_breakdown=None)
        cur = FakeCentralCursor(state_rows=[row])
        s = un.get_or_create_state(cur, 'E1', PS)
        self.assertEqual(s['count_breakdown'][0]['date'], D)
        self.assertEqual(s['minutes_breakdown'], [])

    def test_get_or_create_state_new(self):
        s = un.get_or_create_state(FakeCentralCursor(), 'E9', PS)
        self.assertEqual(s, un._new_state('E9', PS))

    def test_save_state_params(self):
        cur = FakeCentralCursor()
        s = state(count_since_reset=1, count_breakdown=[{'date': D, 'minutes': 2}])
        un.save_state(cur, s)
        q, params = cur.queries[0]
        self.assertEqual(len(params), 2 + 9 + 9)
        self.assertEqual(q.count('%s'), len(params))
        self.assertEqual(params[2:11], params[11:])


if __name__ == '__main__':
    unittest.main()


class TestGoLiveGuard(NotifyCase):
    """Live runs never process a date before LIVE_START_DATE; dry runs do."""

    def _run(self, live, d):
        self.cur = FakeCentralCursor(state_rows=[], tl_rows=TL_ROWS)
        self.conn = FakeConn(self.cur)
        get_ut = MagicMock(return_value=[ut_record(minutes=40, d=d)])
        with patch.object(un, 'LIVE_START_DATE', date(2026, 9, 28)), \
             patch.object(un, 'get_undertime_for_date', get_ut), \
             patch.object(un, 'get_db_connection', return_value=self.conn):
            un.process_day(d, live=live)
        return get_ut

    def test_live_before_go_live_does_nothing(self):
        get_ut = self._run(live=True, d=date(2026, 9, 27))
        get_ut.assert_not_called()
        self.fir.assert_not_called()
        self.send_email.assert_not_called()
        self.conn.commit.assert_not_called()

    def test_live_on_go_live_date_is_processed(self):
        get_ut = self._run(live=True, d=date(2026, 9, 28))
        get_ut.assert_called_once()
        self.assertEqual(self.fir.call_count, 1)
        self.assertFalse(self.fir.call_args.kwargs['dry_run'])

    def test_dry_run_before_go_live_still_previews(self):
        get_ut = self._run(live=False, d=date(2026, 9, 27))
        get_ut.assert_called_once()
        self.assertTrue(self.fir.call_args.kwargs['dry_run'])
        self.send_email.assert_not_called()
