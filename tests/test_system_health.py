"""Tests for helpers/system_health.py and the /admin/system-health page.

Fully offline: no subprocesses, journal or database. Run with
  ./venv/bin/python -m unittest tests.test_system_health -v
"""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from helpers import system_health as sh


class CronScheduleTests(unittest.TestCase):

    def test_every_five_minutes(self):
        s = sh.CronSchedule('*/5 * * * *')
        self.assertEqual(s.prev_run(datetime(2026, 11, 23, 21, 58, 30)),
                         datetime(2026, 11, 23, 21, 55))

    def test_daily_rolls_back_to_yesterday(self):
        s = sh.CronSchedule('0 3 * * *')
        self.assertEqual(s.prev_run(datetime(2026, 11, 23, 2, 59)),
                         datetime(2026, 11, 22, 3, 0))

    def test_exact_minute_counts(self):
        s = sh.CronSchedule('30 12 * * *')
        self.assertEqual(s.prev_run(datetime(2026, 11, 23, 12, 30, 59)),
                         datetime(2026, 11, 23, 12, 30))

    def test_hour_step(self):
        s = sh.CronSchedule('15 */2 * * *')
        self.assertEqual(s.prev_run(datetime(2026, 11, 23, 21, 0)),
                         datetime(2026, 11, 23, 20, 15))

    def test_day_list(self):
        s = sh.CronSchedule('0 17 8,9,10,23 * *')
        self.assertEqual(s.prev_run(datetime(2026, 11, 22, 12, 0)),
                         datetime(2026, 11, 10, 17, 0))

    def test_dow_sunday_is_0_and_7(self):
        # 2026-11-22 is a Sunday
        for expr in ('0 4 * * 0', '0 4 * * 7'):
            self.assertEqual(sh.CronSchedule(expr).prev_run(datetime(2026, 11, 24, 0, 0)),
                             datetime(2026, 11, 22, 4, 0), expr)

    def test_dom_and_dow_are_ored(self):
        # day 1 OR Monday; 2026-11-23 is a Monday
        s = sh.CronSchedule('0 6 1 * 1')
        self.assertEqual(s.prev_run(datetime(2026, 11, 23, 7, 0)),
                         datetime(2026, 11, 23, 6, 0))
        self.assertEqual(s.prev_run(datetime(2026, 11, 2, 7, 0)),   # Monday Nov 2
                         datetime(2026, 11, 2, 6, 0))
        self.assertEqual(s.prev_run(datetime(2026, 11, 1, 7, 0)),   # Sunday Nov 1, day 1
                         datetime(2026, 11, 1, 6, 0))

    def test_yearly_month_field(self):
        s = sh.CronSchedule('30 0 1 1 *')
        self.assertEqual(s.prev_run(datetime(2026, 11, 23)), datetime(2026, 1, 1, 0, 30))

    def test_next_run(self):
        self.assertEqual(sh.CronSchedule('0 3 * * *').next_run(datetime(2026, 11, 23, 3, 0)),
                         datetime(2026, 11, 24, 3, 0))
        self.assertEqual(sh.CronSchedule('*/5 * * * *').next_run(datetime(2026, 11, 23, 21, 58)),
                         datetime(2026, 11, 23, 22, 0))
        self.assertEqual(sh.CronSchedule('30 0 1 1 *').next_run(datetime(2026, 11, 23)),
                         datetime(2027, 1, 1, 0, 30))

    def test_bad_expression(self):
        with self.assertRaises(ValueError):
            sh.CronSchedule('0 3 * *')

    def test_describe(self):
        self.assertEqual(sh.CronSchedule('*/30 * * * *').describe(), 'every 30 min')
        self.assertEqual(sh.CronSchedule('5 * * * *').describe(), 'hourly at :05')
        self.assertEqual(sh.CronSchedule('0 3 * * *').describe(), 'daily 03:00')
        self.assertEqual(sh.CronSchedule('0 9,17 * * *').describe(), 'daily at 09:00, 17:00')
        self.assertEqual(sh.CronSchedule('30 0 1 1 *').describe(), 'yearly, Jan 1 00:30')
        self.assertEqual(sh.CronSchedule('0 6 1 * *').describe(), '06:00 on day 1 of each month')


CRONTAB = """
SHELL=/bin/bash
# Payroll emails
0 17 8,23 * * php /srv/app/send.php >> /var/www/htm/logs/send.log 2>&1

0 8 * * * killall -u root node 2>/dev/null
5 * * * * cd /srv/repo && venv/bin/python scripts/sync.py --live >> sync.log 2>&1
# disabled: 0 1 * * * /bin/true
@reboot /usr/bin/thing.sh
@daily /usr/bin/php /srv/x.php > /dev/null 2>&1
"""


class ParseCrontabTests(unittest.TestCase):

    def setUp(self):
        self.jobs = sh.parse_crontab(CRONTAB)

    def test_skips_comments_env_and_blank(self):
        self.assertEqual(len(self.jobs), 5)

    def test_note_from_comment_above(self):
        self.assertEqual(self.jobs[0]['note'], 'Payroll emails')
        self.assertEqual(self.jobs[1]['note'], '')

    def test_log_and_script(self):
        j = self.jobs[0]
        self.assertEqual(j['log'], '/var/www/htm/logs/send.log')
        self.assertEqual(j['script'], '/srv/app/send.php')
        self.assertEqual(j['name'], 'send.php')

    def test_relative_paths_resolve_against_cd(self):
        j = self.jobs[2]
        self.assertEqual(j['log'], '/srv/repo/sync.log')
        self.assertEqual(j['script'], '/srv/repo/scripts/sync.py')

    def test_stderr_to_devnull_is_not_a_log(self):
        j = self.jobs[1]
        self.assertIsNone(j['log'])
        self.assertTrue(j['discards_output'])
        self.assertEqual(j['name'], 'killall')

    def test_stock_header_and_commented_jobs_are_not_notes(self):
        text = ("# Edit this file to introduce tasks to be run by cron.\n"
                "# m h  dom mon dow   command\n"
                "0 8 * * * killall -u root node 2>/dev/null\n"
                "\n"
                "#*/5 * * * * /usr/bin/php /srv/old.php >> /var/log/old.log 2>&1\n"
                "*/5 * * * * php /srv/new.php\n"
                "# Sync CSAT\n"
                "#0 * * * * php /srv/older.php\n"
                "0 * * * * php /srv/csat.php\n")
        jobs = sh.parse_crontab(text)
        self.assertEqual([j['note'] for j in jobs], ['', '', 'Sync CSAT'])

    def test_descriptions_beat_crontab_comments(self):
        text = ("# Weekly Timestamp (runs at 3:00 AM daily)\n"
                "30 12 * * * /usr/bin/python3 /x/weeklytimestamp.py >> /tmp/w.log\n"
                "*/30 * * * * cd /r && venv/bin/python scripts/mailing_list_pending_check.py --store >> m.log\n"
                "0 8 * * * cd /r && venv/bin/python scripts/mailing_list_pending_check.py --send >> m.log\n")
        jobs = sh.parse_crontab(text)
        self.assertIn('weekly timestamp Google Sheet', jobs[0]['note'])   # not the stale "3:00 AM" comment
        self.assertIn('pending badge', jobs[1]['note'])
        self.assertIn('Morning email', jobs[2]['note'])

    def test_macros(self):
        self.assertEqual(self.jobs[3]['schedule'], '@reboot')
        self.assertEqual(self.jobs[4]['schedule'], '0 0 * * *')


class CheckJobTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.script = os.path.join(self.dir, 'job.py')
        open(self.script, 'w').close()
        self.now = datetime(2026, 11, 23, 12, 0)

    def tearDown(self):
        self.tmp.cleanup()

    def _job(self, log, expr='0 3 * * *'):
        line = f"{expr} python {self.script} >> {log} 2>&1"
        return sh.parse_crontab(line)[0]

    def _write_log(self, path, text, mtime):
        with open(path, 'w') as fh:
            fh.write(text)
        ts = mtime.timestamp()
        os.utime(path, (ts, ts))

    def test_missing_log_dir_is_error(self):
        j = sh.check_job(self._job('/nonexistent-dir-xyz/a.log'), self.now, {})
        self.assertEqual(j['status'], 'error')
        self.assertIn('does not exist', j['problems'][0])

    def test_missing_script_is_error(self):
        job = sh.parse_crontab(f"0 3 * * * python {self.dir}/gone.py >> {self.dir}/a.log")[0]
        j = sh.check_job(job, self.now, {})
        self.assertEqual(j['status'], 'error')

    def test_healthy_job(self):
        log = os.path.join(self.dir, 'a.log')
        self._write_log(log, 'Done. 12 rows\n', datetime(2026, 11, 23, 3, 1))
        job = self._job(log)
        j = sh.check_job(job, self.now, {job['command']: datetime(2026, 11, 23, 3, 0, 1)})
        self.assertEqual(j['status'], 'ok', j['problems'])

    def test_traceback_in_tail_is_warn(self):
        log = os.path.join(self.dir, 'a.log')
        self._write_log(log, 'start\nTraceback (most recent call last):\n  File "x"\nKeyError: \'unit\'\n',
                        datetime(2026, 11, 23, 3, 1))
        job = self._job(log)
        j = sh.check_job(job, self.now, {job['command']: datetime(2026, 11, 23, 3, 0, 1)})
        self.assertEqual(j['status'], 'warn')
        self.assertIn('KeyError', j['error_line'])

    def test_not_fired_is_warn(self):
        log = os.path.join(self.dir, 'a.log')
        self._write_log(log, 'ok\n', datetime(2026, 11, 22, 3, 1))
        j = sh.check_job(self._job(log), self.now, {})
        self.assertEqual(j['status'], 'warn')
        self.assertTrue(any('did not start' in p for p in j['problems']))

    def test_job_added_after_its_last_slot_is_waiting(self):
        # Daily 03:00 job added to the crontab at 11:00: its first run is tomorrow.
        j = sh.check_job(self._job(os.path.join(self.dir, 'new.log')), self.now, {},
                         crontab_mtime=datetime(2026, 11, 23, 11, 0))
        self.assertEqual(j['status'], 'ok', j['problems'])
        self.assertEqual(j['problems'], ['Waiting for its first run: Nov 24, 2026 03:00'])

    def test_slot_after_crontab_edit_but_never_ran_is_warn(self):
        # Crontab saved at 01:00, the 03:00 slot came and went with no run.
        j = sh.check_job(self._job(os.path.join(self.dir, 'new.log')), self.now, {},
                         crontab_mtime=datetime(2026, 11, 23, 1, 0))
        self.assertEqual(j['status'], 'warn')
        self.assertTrue(any('did not start' in p for p in j['problems']))

    def test_yearly_job_before_journal_window_is_waiting(self):
        j = sh.check_job(self._job(os.path.join(self.dir, 'y.log'), '30 0 1 1 *'), self.now, {},
                         crontab_mtime=datetime(2026, 11, 1))
        self.assertEqual(j['status'], 'ok')
        self.assertEqual(j['problems'], ['Waiting for its first run: Jan 01, 2027 00:30'])

    def test_mailto_off_wording(self):
        jobs = sh.parse_crontab('MAILTO=""\n0 1 * * * python /x/y.py\n')
        self.assertTrue(jobs[0]['mail_off'])
        j = sh.check_job(jobs[0], self.now, {})
        self.assertIn('No log file and MAILTO is off, so its output is lost', j['problems'])
        self.assertFalse(sh.parse_crontab('0 1 * * * python /x/y.py\n')[0]['mail_off'])

    def test_grace_period_judges_previous_run(self):
        # Every 5 min; at 12:02 the 12:00 run is still within grace, so judge 11:55.
        log = os.path.join(self.dir, 'a.log')
        self._write_log(log, 'ok\n', datetime(2026, 11, 23, 11, 55, 5))
        job = self._job(log, '*/5 * * * *')
        now = datetime(2026, 11, 23, 12, 2)
        j = sh.check_job(job, now, {job['command']: datetime(2026, 11, 23, 11, 55, 1)})
        self.assertEqual(j['expected_run'], datetime(2026, 11, 23, 11, 55))
        self.assertEqual(j['status'], 'ok', j['problems'])

    def test_quiet_run_is_info(self):
        log = os.path.join(self.dir, 'a.log')
        self._write_log(log, 'ok\n', datetime(2026, 11, 20, 3, 1))
        job = self._job(log)
        j = sh.check_job(job, self.now, {job['command']: datetime(2026, 11, 23, 3, 0, 1)})
        self.assertEqual(j['status'], 'info')


class BackupCheckTests(unittest.TestCase):

    def _status(self, **kw):
        fd, path = tempfile.mkstemp()
        with os.fdopen(fd, 'w') as fh:
            json.dump(kw, fh)
        self.addCleanup(os.unlink, path)
        return path

    def test_missing_file(self):
        self.assertEqual(sh.check_backup(status_file='/nonexistent/x.json')['status'], 'error')

    def test_fresh_ok(self):
        now = datetime(2026, 11, 24, 8, 0)
        p = self._status(ok=True, message='All 3', finished='2026-11-24T03:01:10+08:00', files=[])
        self.assertEqual(sh.check_backup(now, p)['status'], 'ok')

    def test_stale(self):
        now = datetime(2026, 11, 26, 8, 0)
        p = self._status(ok=True, message='All 3', finished='2026-11-24T03:01:10+08:00', files=[])
        r = sh.check_backup(now, p)
        self.assertEqual(r['status'], 'error')
        self.assertIn('hours old', r['message'])

    def test_failed_run(self):
        now = datetime(2026, 11, 24, 8, 0)
        p = self._status(ok=False, message='Failed: central_db', finished='2026-11-24T03:01:10+08:00')
        self.assertEqual(sh.check_backup(now, p)['status'], 'error')


class MiscTests(unittest.TestCase):

    def test_worst(self):
        self.assertEqual(sh.worst(['ok', 'info', 'warn']), 'warn')
        self.assertEqual(sh.worst([]), 'ok')

    def test_problems_list_orders_errors_first(self):
        report = {
            'disk': {'status': 'warn', 'pct': 85.0, 'free': 8 * 2**30},
            'backup': {'status': 'ok'},
            'services': [{'status': 'error', 'label': 'MySQL', 'name': 'mysql', 'state': 'failed'}],
            'certs': [],
            'jobs': [{'status': 'info', 'name': 'x'}],
            'app_errors': {'status': 'ok'},
        }
        probs = sh.problems_list(report)
        self.assertEqual([p[1] for p in probs], ['Service', 'Disk'])

    def test_log_error_regex(self):
        for line in ('Traceback (most recent call last):', 'ERROR: boom', 'pymysql.err.OperationalError: (2003',
                     'KeyError: unit', 'FAILED central_db', 'sh: 1: foo: command not found'):
            self.assertTrue(sh.LOG_ERROR_RE.search(line), line)
        for line in ('errors: 0', 'Done, 0 failed', 'Errored rows skipped: 0', 'no error'):
            self.assertFalse(sh.LOG_ERROR_RE.search(line), line)


class PageAccessTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import app as appmod
        cls.app = appmod.app
        cls.app.config['TESTING'] = True

    def _fake_report(self):
        now = datetime.now()
        return {
            'now': now, 'overall': 'warn', 'problems': [('warn', 'Disk', '/ is 85% full')],
            'sections': {'jobs': 'ok', 'disk': 'warn', 'backup': 'ok', 'services': 'ok',
                         'certs': 'ok', 'app_errors': 'ok'},
            'jobs': [{'status': 'ok', 'name': 'sync.py', 'note': '', 'schedule_text': 'every 5 min',
                      'fired_at': now - timedelta(minutes=3), 'log_mtime': now, 'log_size': 2048,
                      'problems': [], 'error_line': None, 'command': 'python sync.py', 'log': '/x.log',
                      'log_tail': ['ok']}],
            'disk': {'status': 'warn', 'pct': 85.0, 'free': 8 * 2**30, 'total': 58 * 2**30},
            'backup': {'status': 'ok', 'message': 'All 3', 'finished': now, 'files': [
                {'db': 'central_db', 'file': 'c.sql.gz', 'bytes': 5 * 2**20}], 'total_bytes': 5 * 2**20},
            'services': [{'name': 'mysql', 'label': 'MySQL', 'state': 'active', 'status': 'ok', 'kind': 'service'}],
            'portal': {'started': now, 'restarts': 0, 'memory': 30 * 2**20},
            'certs': [{'domain': 'hrportal.cohere.ph', 'expires': now + timedelta(days=60),
                       'days_left': 60, 'status': 'ok'}],
            'app_errors': {'total': 3, 'hours': 24, 'status': 'warn',
                           'groups': [{'pattern': 'ERROR:x', 'count': 3, 'sample': 'ERROR:x 1', 'last': now}]},
            'db_sizes': [{'name': 'central_db', 'bytes': 600 * 2**20, 'tables': 128}],
        }

    def _client(self, **sess):
        c = self.app.test_client()
        with c.session_transaction() as s:
            s['user'] = {'email': 'zztest-health@cohere.ph', 'name': 'T', 'employee_id': '000000-00',
                         'emp_number': 9999}
            s.update(sess)
        return c

    def test_anonymous_redirects_to_login(self):
        resp = self.app.test_client().get('/admin/system-health')
        self.assertEqual(resp.status_code, 302)

    def test_sub_admin_is_refused(self):
        c = self._client(is_admin=False, permissions={'can_settings': True})
        with patch('modules.system_health.get_report') as gr:
            resp = c.get('/admin/system-health')
        self.assertEqual(resp.status_code, 302)
        gr.assert_not_called()

    def test_admin_sees_page(self):
        c = self._client(is_admin=True)
        with patch('modules.system_health.get_report', return_value=self._fake_report()):
            resp = c.get('/admin/system-health?jobs=all')
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode()
        self.assertIn('System Health', html)
        self.assertIn('sync.py', html)
        self.assertIn('/ is 85% full', html)
        self.assertIn('hrportal.cohere.ph', html)

    def test_issues_filter_hides_ok_jobs(self):
        c = self._client(is_admin=True)
        with patch('modules.system_health.get_report', return_value=self._fake_report()):
            resp = c.get('/admin/system-health')
        self.assertIn('Every cron job is OK.', resp.data.decode())


if __name__ == '__main__':
    unittest.main()
