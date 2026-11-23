"""
System health checks, shared by Admin > System Health (modules/system_health.py)
and the daily alert (scripts/system_health_check.py).

Everything here is read-only: it reads root's crontab, the cron journal, log
files, the backup status file, systemd/docker state and SSL certificates. It
never changes anything.

Each check returns dicts with a `status` of 'ok' | 'info' | 'warn' | 'error'.

Cron jobs
---------
Jobs are parsed from `crontab -l`, so new jobs show up without code changes.
For every job we work out when it should last have run (a small cron-expression
evaluator below -- croniter isn't installed) and compare that with:
  * the cron journal (`journalctl -u cron`) -- did cron fire it?
  * the log file it appends to (`>> file`) -- did it write anything since?
  * the last lines of that log -- does it end in a Traceback / ERROR?
A job whose log folder does not exist can never run: cron's shell fails the
`>>` redirect and skips the command. That is an error. (The OT/RDW/CWS/FTS
payroll emails sat like that from May 2025 until 2026-11-23.)
"""
import json
import os
import re
import shutil
import subprocess
from datetime import date, datetime, timedelta, time

STATUS_ORDER = {'ok': 0, 'info': 1, 'warn': 2, 'error': 3}

BACKUP_STATUS_FILE = '/var/backups/mysql/last_status.json'
BACKUP_MAX_AGE_HOURS = 26

SERVICES = [
    ('leavesystem', 'HR portal (Flask)'),
    ('apache2', 'Apache web server'),
    ('mysql', 'MySQL'),
    ('cron', 'Cron scheduler'),
    ('snap.docker.dockerd', 'Docker'),
    ('pm2-root', 'PM2 (Node apps)'),
]
CONTAINERS = [
    ('keycloak', 'Keycloak login'),
    ('keycloak-postgres', 'Keycloak database'),
]

LETSENCRYPT_LIVE = '/etc/letsencrypt/live'
CERT_WARN_DAYS = 20   # certbot renews at 30 days left; under 20 means renewal is failing
CERT_ERROR_DAYS = 7

DISK_WARN_PCT = 80
DISK_ERROR_PCT = 90

LOG_TAIL_LINES = 25
LOG_BIG_BYTES = 200 * 1024 * 1024
JOB_GRACE = timedelta(minutes=10)    # time a job gets to run before we expect output
JOURNAL_HOURS = 50                   # cron journal window we read

LOG_ERROR_RE = re.compile(
    r'Traceback \(most recent call last\)'
    r'|\bERROR\b'
    r'|\bFAILED\b'
    r'|\bFatal error\b'
    r'|\b[A-Z]\w*(?:Error|Exception): '
    r'|: command not found'
    r'|No such file or directory'
)


def worst(statuses):
    statuses = list(statuses)
    if not statuses:
        return 'ok'
    return max(statuses, key=lambda s: STATUS_ORDER.get(s, 0))


# leavesystem.service runs with PATH=venv/bin only, so look tools up ourselves.
_TOOL_PATH = '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin'


def _bin(name):
    return shutil.which(name, path=_TOOL_PATH) or name


def _run(cmd, timeout=10):
    try:
        p = subprocess.run([_bin(cmd[0])] + list(cmd[1:]), capture_output=True,
                           text=True, timeout=timeout)
        return p.returncode, p.stdout
    except (OSError, subprocess.SubprocessError):
        return -1, ''


# ──────────────────────────────────────────────────────────────────
# Cron expressions
# ──────────────────────────────────────────────────────────────────
MACROS = {
    '@hourly': '0 * * * *', '@daily': '0 0 * * *', '@midnight': '0 0 * * *',
    '@weekly': '0 0 * * 0', '@monthly': '0 0 1 * *',
    '@yearly': '0 0 1 1 *', '@annually': '0 0 1 1 *',
}


def _field(spec, lo, hi):
    vals = set()
    for part in spec.split(','):
        step = 1
        if '/' in part:
            part, s = part.split('/', 1)
            step = int(s)
        if part == '*':
            a, b = lo, hi
        elif '-' in part:
            a, b = (int(x) for x in part.split('-', 1))
        else:
            a = int(part)
            b = hi if step > 1 else a
        vals.update(range(a, b + 1, step))
    return vals


class CronSchedule:
    """Five-field cron expression. Supports *, lists, ranges and steps."""

    def __init__(self, expr):
        f = expr.split()
        if len(f) != 5:
            raise ValueError(f'bad cron expression: {expr!r}')
        self.expr = expr
        self.minutes = sorted(_field(f[0], 0, 59), reverse=True)
        self.hours = sorted(_field(f[1], 0, 23), reverse=True)
        self.dom = _field(f[2], 1, 31)
        self.months = _field(f[3], 1, 12)
        self.dow = {d % 7 for d in _field(f[4], 0, 7)}   # 7 is Sunday too
        self.dom_any = f[2] == '*'
        self.dow_any = f[4] == '*'

    def _day_ok(self, d):
        if d.month not in self.months:
            return False
        dom_ok = d.day in self.dom
        dow_ok = (d.weekday() + 1) % 7 in self.dow   # cron: Sunday = 0
        if self.dom_any or self.dow_any:
            return dom_ok and dow_ok
        return dom_ok or dow_ok                       # cron ORs them when both are set

    def prev_run(self, now, max_days=400):
        """Latest scheduled minute <= now, or None."""
        now = now.replace(second=0, microsecond=0)
        for i in range(max_days):
            day = now.date() - timedelta(days=i)
            if not self._day_ok(day):
                continue
            for h in self.hours:
                if i == 0 and h > now.hour:
                    continue
                for m in self.minutes:
                    if i == 0 and h == now.hour and m > now.minute:
                        continue
                    return datetime.combine(day, time(h, m))
        return None

    def next_run(self, now, max_days=400):
        """First scheduled minute strictly after now, or None."""
        now = now.replace(second=0, microsecond=0)
        for i in range(max_days):
            day = now.date() + timedelta(days=i)
            if not self._day_ok(day):
                continue
            for h in reversed(self.hours):
                if i == 0 and h < now.hour:
                    continue
                for m in reversed(self.minutes):
                    if i == 0 and h == now.hour and m <= now.minute:
                        continue
                    return datetime.combine(day, time(h, m))
        return None

    def describe(self):
        f = self.expr.split()
        if self.expr == '* * * * *':
            return 'every minute'
        if f[0].startswith('*/') and f[1:] == ['*', '*', '*', '*']:
            return f'every {f[0][2:]} min'
        if f[0].isdigit() and f[1].startswith('*/') and f[2:] == ['*', '*', '*']:
            return f'every {f[1][2:]} h at :{int(f[0]):02d}'
        if f[0].isdigit() and f[1:] == ['*', '*', '*', '*']:
            return f'hourly at :{int(f[0]):02d}'
        if f[0].isdigit() and f[1].isdigit():
            at = f'{int(f[1]):02d}:{int(f[0]):02d}'
            if f[2:] == ['*', '*', '*']:
                return f'daily {at}'
            if f[2] == '*' and f[3] == '*':
                return f'{at} on weekdays {f[4]}'
            if f[2].isdigit() and f[3].isdigit() and f[4] == '*':
                return f'yearly, {date(2000, int(f[3]), 1):%b} {int(f[2])} {at}'
            day_word = 'day' if f[2].isdigit() else 'days'
            if f[3] == '*':
                return f'{at} on {day_word} {f[2]} of each month'
            return f'{at} on {day_word} {f[2]} of month {f[3]}'
        if f[0].isdigit() and ',' in f[1] and f[2:] == ['*', '*', '*']:
            return 'daily at ' + ', '.join(f'{int(h):02d}:{int(f[0]):02d}' for h in f[1].split(','))
        return self.expr


# ──────────────────────────────────────────────────────────────────
# What each cron job is for (shown on the page). Matched by a piece of the
# command, first match wins. A job not listed here falls back to the comment
# written above it in the crontab. Add a line here when you add a cron job.
# ──────────────────────────────────────────────────────────────────
JOB_DESCRIPTIONS = [
    # Server housekeeping
    ("vscode-server", "Ends every VS Code remote session (editor, extensions, terminals) at 8 AM so "
                      "idle ones don't pile up and slow the server. Leaves calendar-app alone."),
    ("backup_databases.sh", "Nightly backup of central_db, orangehrm2 and orangehrm to /var/backups/mysql "
                            "(7 daily + 4 weekly). Status shows in the Database backup card."),
    ("system_health_check.py", "Emails HEALTH_ALERT_TO (.env) when something on this page is at Problem "
                               "level. Nothing is sent on a clean day."),
    # Attendance pipeline
    ("process_timerecords.php", "Copies new biometric IN/OUT punches from dailytimerecord into Timerecords "
                                "(the old cloud-leave attendance table)."),
    ("sync_filtered.php", "Rebuilds dailytimerecordsfiltered for the last 3 days: groups raw punches into "
                          "shifts. AWOL, tardiness, undertime and On Shift all read this table."),
    ("weeklytimestamp.py", "Copies recent Timerecords punches to the weekly timestamp Google Sheet "
                           "(clears and rewrites it)."),
    ("daily_fts.py", "Failure-to-swipe engine: recalculates missing punches for the current payroll "
                     "period up to yesterday, so late-arriving punches correct themselves."),
    ("fts_notify.py", "Emails each TL (CC their SOM) their agents' unfiled FTS from yesterday."),
    ("break_autofix.py", "Moves overnight-shift breaks that were dated to the calendar day back to the "
                         "shift they belong to, so Break Logs show the right totals."),
    # Violations -> memos / IRs
    ("tardiness_notify.py", "Counts lates per payroll period, files IRs automatically at the thresholds "
                            "and emails the memo."),
    ("overbreak_notify.py", "Counts overbreaks per payroll period, files IRs automatically at the "
                            "thresholds and emails the memo."),
    ("undertime_notify.py", "Checks yesterday's early logouts, counts undertimes per payroll period, "
                            "files IRs and emails the memo."),
    ("absence_notify.py", "Emails yesterday's absences (same data as Admin > Absences). Replaced the "
                          "old sheet-based absence email on 2026-10-27."),
    ("cron_incident_reminder.py", "Reminds reviewers about incident reports left pending or reviewed "
                                  "with no activity for 72+ hours."),
    # Leave
    ("auto_convert_scheduled_leaves.py", "Marks past Scheduled leave as Approved/Taken in the portal "
                                         "and in OrangeHRM."),
    ("update_status.py", "Midnight: marks yesterday's Scheduled leave as Approved."),
    ("apply_pending_leave_credits.py", "Charges leave filed for a year that had no balance yet "
                                       "(credit_pending) once that year's entitlement exists."),
    ("prorated_leave_grants.py", "Adds pro-rated VL on the 1st deployment anniversary and SL at "
                                 "regularization (leave policy 3.2/3.3)."),
    ("year_end_rollover.py", "Jan 1 only: creates the new year's leave balances (VL/SL/LWOP plus "
                             "Carry-Over from unused VL) per leave policy 3.1-3.3."),
    ("leave_to_gsheet.py", "Copies all OrangeHRM leave records to the \"Raw Leave Data\" Google Sheet "
                           "(clears and rewrites it)."),
    # Employee data sync
    ("sync_userdata.py", "Fills missing emails in userdata from the employee sheet and maps each "
                         "employee's supervisor from the TL column."),
    ("sync_ohrm_emails.py", "Copies work emails from the employee sheet into OrangeHRM where they're "
                            "missing."),
    ("sync_supervisor_from_gsheet.py", "Sets each employee's supervisor in central_db.Employees from "
                                       "the sheet's Approver column."),
    ("mailing_list_sheet_sync.py", "Makes the cPanel email groups match the employee sheet's "
                                   "\"Mailing List\" column."),
    ("mailing_list_pending_check.py --store", "Reads the Mailman moderation queues and updates the "
                                              "pending badge on Admin > Mailing Lists."),
    ("mailing_list_pending_check.py --send", "Morning email listing Mailman posts and requests "
                                             "waiting for approval."),
    # Other
    ("sync_csat.php", "Pulls new CSAT ratings from the Google Sheet into the CSAT dashboard (only rows "
                      "added since the last run)."),
    ("sync_numa_stats.py", "Refreshes the Numa Stats page from the NUMA Stat Dashboard sheet."),
    ("coaching_reminders.py", "Reminds agents (CC TL) to finish coaching action plans: from 2 days "
                              "after the session, then daily until done."),
    ("coe_auto_release.py", "Marks CoE requests as released if the employee never confirmed pickup "
                            "within the allowed days."),
]


def describe_job(command, fallback=''):
    for needle, text in JOB_DESCRIPTIONS:
        if needle in command:
            return text
    return fallback


# ──────────────────────────────────────────────────────────────────
# Crontab parsing
# ──────────────────────────────────────────────────────────────────
_ENV_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*\s*=')
_REDIR_RE = re.compile(r'(?<![0-9&])>>?\s*([^\s;&|]+)')
_CD_RE = re.compile(r'^\s*cd\s+(\S+)\s*&&')
_SCRIPT_RE = re.compile(r'(\S+\.(?:py|php|sh))(?:\s|$)')


# A commented-out job ("#*/5 * * * * php ...") is not a description.
_COMMENTED_JOB_RE = re.compile(r'^(@\w+|[\d*/,-]+(\s+[\d*/,-]+){4})\s+\S')
# Debian's stock crontab header sits right above the first job.
_STOCK_HEADER_MARKERS = ('Edit this file to introduce tasks', 'm h  dom mon dow')


def _note_text(lines):
    if any(any(mk in ln for mk in _STOCK_HEADER_MARKERS) for ln in lines):
        return ''
    kept = [ln for ln in lines if ln and not _COMMENTED_JOB_RE.match(ln)]
    return ' '.join(kept)[:200]


def _resolve(path, base):
    if not path or os.path.isabs(path) or not base:
        return path
    return os.path.normpath(os.path.join(base, path))


def parse_crontab(text):
    """Active jobs in a crontab. Comment lines directly above a job become its note."""
    jobs, note = [], []
    mail_off = False   # MAILTO="" applies to the jobs below it
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            note = []
            continue
        if line.startswith('#'):
            note.append(line.lstrip('#').strip())
            continue
        if _ENV_RE.match(line):
            key, _, val = line.partition('=')
            if key.strip() == 'MAILTO':
                mail_off = val.strip().strip('"\'') == ''
            note = []
            continue

        reboot = False
        if line.startswith('@'):
            macro, _, cmd = line.partition(' ')
            if macro == '@reboot':
                reboot, expr = True, None
            elif macro in MACROS:
                expr = MACROS[macro]
            else:
                note = []
                continue
        else:
            parts = line.split(None, 5)
            if len(parts) < 6:
                note = []
                continue
            expr, cmd = ' '.join(parts[:5]), parts[5]
        cmd = cmd.strip()

        m = _CD_RE.match(cmd)
        base = m.group(1) if m else None
        log = None
        for target in _REDIR_RE.findall(cmd):
            if target != '/dev/null':
                log = _resolve(target, base)
                break
        sm = _SCRIPT_RE.search(cmd)
        script = _resolve(sm.group(1), base) if sm else None

        try:
            sched = CronSchedule(expr) if expr else None
        except ValueError:
            sched = None

        jobs.append({
            'line_no': n,
            'schedule': '@reboot' if reboot else expr,
            'schedule_text': 'at boot' if reboot else (sched.describe() if sched else expr),
            'command': cmd,
            'name': os.path.basename(script) if script else cmd.split()[0],
            'script': script,
            'log': log,
            'discards_output': log is None and '/dev/null' in cmd,
            'note': describe_job(cmd, _note_text(note)),
            'mail_off': mail_off,
            '_sched': sched,
        })
        note = []
    return jobs


def read_crontab():
    code, out = _run(['crontab', '-l'])
    return out if code == 0 else ''


def cron_fired_times(hours=JOURNAL_HOURS):
    """{command: last datetime cron started it} from the cron journal."""
    # By syslog tag, not `-u cron`: a short job's CMD line can lose its unit
    # field (the process is gone before journald looks it up), and -u drops it.
    code, out = _run(['journalctl', '-t', 'CRON', '--since', f'-{hours}h',
                      '-o', 'short-iso', '--no-pager', '-q', '-g', r'CMD \('], timeout=20)
    fired = {}
    for line in out.splitlines():
        m = re.match(r'^(\S+) \S+ CRON\[\d+\]: \(\w+\) CMD \((.*)\)$', line)
        if not m:
            continue
        try:
            ts = datetime.fromisoformat(m.group(1)).replace(tzinfo=None)
        except ValueError:
            continue
        fired[m.group(2)] = ts
    return fired


def tail_lines(path, n=LOG_TAIL_LINES, max_bytes=32768):
    try:
        with open(path, 'rb') as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            data = fh.read().decode('utf-8', 'replace')
    except OSError:
        return []
    return data.splitlines()[-n:]


def crontab_mtime(path='/var/spool/cron/crontabs/root'):
    """When root's crontab was last saved (any `crontab` edit)."""
    try:
        return datetime.fromtimestamp(os.stat(path).st_mtime)
    except OSError:
        return None


def check_job(job, now, fired, crontab_mtime=None):
    """Adds status, problems and timing fields to one parsed job.
    Problems are (status, text); an 'ok' entry is just an informative line."""
    problems = []   # (status, text)
    sched = job['_sched']
    expected = None
    if sched:
        prev = sched.prev_run(now)
        # A job gets JOB_GRACE to run; inside that window judge the run before.
        if prev and now - prev < JOB_GRACE:
            prev = sched.prev_run(prev - timedelta(minutes=1))
        expected = prev
    job['expected_run'] = expected
    job['fired_at'] = fired.get(job['command'])
    job['log_mtime'] = None
    job['log_size'] = None
    job['log_tail'] = []
    job['error_line'] = None

    if job['script'] and not os.path.exists(job['script']):
        problems.append(('error', f"Script not found: {job['script']}"))

    if job['log']:
        log_dir = os.path.dirname(job['log']) or '.'
        if not os.path.isdir(log_dir):
            problems.append(('error', f"Log folder {log_dir} does not exist, so cron "
                                      f"skips this job every time"))
        elif os.path.exists(job['log']):
            st = os.stat(job['log'])
            job['log_mtime'] = datetime.fromtimestamp(st.st_mtime)
            job['log_size'] = st.st_size
            job['log_tail'] = tail_lines(job['log'])
            for line in reversed(job['log_tail']):
                if LOG_ERROR_RE.search(line):
                    job['error_line'] = line.strip()[:300]
                    break
            if job['error_line']:
                problems.append(('warn', 'Last log lines contain an error'))
            if st.st_size > LOG_BIG_BYTES:
                problems.append(('warn', f"Log is {st.st_size // (1024 * 1024)} MB, needs rotating"))
    elif job['discards_output']:
        problems.append(('info', 'Output goes to /dev/null, so failures are invisible'))
    elif job.get('mail_off'):
        problems.append(('info', 'No log file and MAILTO is off, so its output is lost'))
    else:
        problems.append(('info', 'No log file; output is mailed to root'))

    job['next_run'] = sched.next_run(now) if sched else None
    journal_start = now - timedelta(hours=JOURNAL_HOURS)
    never_ran = job['log'] and not job['log_mtime'] and not job['fired_at']
    if expected and never_ran and not any(s == 'error' for s, _ in problems):
        # No log yet and no start in the journal. If its last slot came before the
        # crontab was last edited (a new or changed job) or before the journal
        # window (a yearly job), it simply hasn't had a turn yet. Otherwise cron
        # skipped a slot it should have run.
        if expected < max(crontab_mtime or journal_start, journal_start):
            nxt = f"{job['next_run']:%b %d, %Y %H:%M}" if job['next_run'] else 'unknown'
            problems.append(('ok', f'Waiting for its first run: {nxt}'))
        else:
            problems.append(('warn', f"Cron did not start it at {expected:%b %d %H:%M}"))
    elif expected and expected > journal_start + timedelta(minutes=5):
        if not job['fired_at'] or job['fired_at'] < expected - timedelta(minutes=1):
            problems.append(('warn', f"Cron did not start it at {expected:%b %d %H:%M}"))
        elif job['log_mtime'] and job['log_mtime'] < expected - timedelta(minutes=1) \
                and not any(s == 'error' for s, _ in problems):
            problems.append(('info', 'Ran, but wrote nothing to its log since'))

    job['problems'] = [p for _, p in sorted(problems, key=lambda x: -STATUS_ORDER[x[0]])]
    job['status'] = worst(s for s, _ in problems) if problems else 'ok'
    job.pop('_sched', None)
    return job


def check_cron_jobs(now=None, crontab_text=None, fired=None):
    now = now or datetime.now()
    text = read_crontab() if crontab_text is None else crontab_text
    fired = cron_fired_times() if fired is None else fired
    edited = crontab_mtime() if crontab_text is None else None
    return [check_job(j, now, fired, edited) for j in parse_crontab(text)]


# ──────────────────────────────────────────────────────────────────
# Server checks
# ──────────────────────────────────────────────────────────────────
def check_disk(path='/'):
    du = shutil.disk_usage(path)
    pct = round(du.used * 100 / du.total, 1)
    status = 'error' if pct >= DISK_ERROR_PCT else 'warn' if pct >= DISK_WARN_PCT else 'ok'
    return {'path': path, 'total': du.total, 'used': du.used, 'free': du.free,
            'pct': pct, 'status': status}


def check_backup(now=None, status_file=BACKUP_STATUS_FILE):
    now = now or datetime.now()
    try:
        with open(status_file) as fh:
            st = json.load(fh)
    except (OSError, ValueError):
        return {'status': 'error', 'message': 'No backup has run yet (no status file)',
                'finished': None, 'files': [], 'total_bytes': 0, 'age_hours': None}
    finished = None
    try:
        finished = datetime.fromisoformat(st.get('finished', '')).replace(tzinfo=None)
    except ValueError:
        pass
    age = (now - finished).total_seconds() / 3600 if finished else None
    status = 'ok'
    message = st.get('message', '')
    if not st.get('ok'):
        status = 'error'
    elif age is None or age > BACKUP_MAX_AGE_HOURS:
        status = 'error'
        message = f'Last backup is {age:.0f} hours old' if age else 'Backup time unknown'
    return {'status': status, 'message': message, 'finished': finished,
            'age_hours': age, 'files': st.get('files', []),
            'total_bytes': st.get('total_bytes', 0)}


def check_services():
    rows = []
    for unit, label in SERVICES:
        _, out = _run(['systemctl', 'is-active', unit])
        state = out.strip() or 'unknown'
        rows.append({'name': unit, 'label': label, 'state': state, 'kind': 'service',
                     'status': 'ok' if state == 'active' else 'error'})
    states = docker_states()
    for cname, label in CONTAINERS:
        state = states.get(cname, 'not found')
        ok = state.startswith('running') and 'unhealthy' not in state
        rows.append({'name': cname, 'label': label, 'state': state, 'kind': 'container',
                     'status': 'ok' if ok else 'error'})
    return rows


def docker_states(sock='/var/run/docker.sock', timeout=8):
    """{name: 'running' | 'running healthy' | ...} straight from the Docker API socket.
    (The snap `docker` CLI is slow and hangs without a login environment.)"""
    import http.client
    import socket

    class _Conn(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(timeout)
            self.sock.connect(sock)

    try:
        conn = _Conn('docker', timeout=timeout)
        conn.request('GET', '/containers/json?all=1')
        data = json.loads(conn.getresponse().read())
        conn.close()
    except (OSError, ValueError, http.client.HTTPException):
        return {}
    states = {}
    for c in data:
        status = c.get('Status', '')
        health = re.search(r'\((healthy|unhealthy|health: starting)\)', status)
        for n in c.get('Names', []):
            states[n.lstrip('/')] = c.get('State', '') + (f' {health.group(1)}' if health else '')
    return states


def portal_service_info():
    _, out = _run(['systemctl', 'show', 'leavesystem', '-p', 'ActiveEnterTimestamp',
                   '-p', 'NRestarts', '-p', 'MemoryCurrent'])
    info = dict(line.split('=', 1) for line in out.splitlines() if '=' in line)
    started = None
    parts = info.get('ActiveEnterTimestamp', '').split()   # "Mon 2026-11-23 16:40:01 PST"
    if len(parts) >= 3:
        try:
            started = datetime.strptime(f'{parts[1]} {parts[2]}', '%Y-%m-%d %H:%M:%S')
        except ValueError:
            pass
    mem = info.get('MemoryCurrent', '')
    return {'started': started,
            'restarts': int(info['NRestarts']) if info.get('NRestarts', '').isdigit() else None,
            'memory': int(mem) if mem.isdigit() else None}


_NORM_RE = [
    (re.compile(r'\d{4}-\d{2}-\d{2}'), '<date>'),
    (re.compile(r'\d{2}:\d{2}:\d{2}'), '<time>'),
    (re.compile(r'\d+'), 'N'),
]


def app_errors(hours=24, top=8):
    """Portal errors from the journal, grouped by message shape."""
    code, out = _run(['journalctl', '-u', 'leavesystem', '--since', f'-{hours}h',
                      '-o', 'short-iso', '--no-pager', '-q',
                      '-g', r'ERROR|Traceback \(most recent'], timeout=30)
    groups = {}
    total = 0
    for line in out.splitlines():
        m = re.match(r'^(\S+) \S+ [^:]+: (.*)$', line)
        if not m:
            continue
        msg = m.group(2)
        if not (re.search(r'\bERROR\b|Traceback \(most recent', msg)):
            continue
        total += 1
        key = msg
        for rx, rep in _NORM_RE:
            key = rx.sub(rep, key)
        key = key[:110]
        g = groups.setdefault(key, {'pattern': key, 'count': 0, 'sample': msg[:400], 'last': None})
        g['count'] += 1
        g['sample'] = msg[:400]
        try:
            g['last'] = datetime.fromisoformat(m.group(1)).replace(tzinfo=None)
        except ValueError:
            pass
    rows = sorted(groups.values(), key=lambda g: -g['count'])[:top]
    status = 'ok' if total == 0 else 'warn' if total < 500 else 'error'
    return {'total': total, 'groups': rows, 'hours': hours, 'status': status}


def check_certs(now=None, live_dir=LETSENCRYPT_LIVE):
    now = now or datetime.now()
    rows = []
    try:
        domains = sorted(d for d in os.listdir(live_dir)
                         if os.path.isdir(os.path.join(live_dir, d)))
    except OSError:
        return rows
    for d in domains:
        _, out = _run(['openssl', 'x509', '-enddate', '-noout', '-in',
                       os.path.join(live_dir, d, 'cert.pem')])
        expires = None
        if out.startswith('notAfter='):
            try:
                expires = datetime.strptime(out.strip()[9:], '%b %d %H:%M:%S %Y %Z')
            except ValueError:
                pass
        days = (expires - now).days if expires else None
        status = ('error' if days is None or days < CERT_ERROR_DAYS
                  else 'warn' if days < CERT_WARN_DAYS else 'ok')
        rows.append({'domain': d, 'expires': expires, 'days_left': days, 'status': status})
    # Soonest first; unreadable certs (days None) at the very top.
    return sorted(rows, key=lambda c: -1 if c['days_left'] is None else c['days_left'])


def collect_all(now=None):
    """Everything except database sizes (those need a DB connection)."""
    from concurrent.futures import ThreadPoolExecutor
    now = now or datetime.now()
    # The slow parts are subprocesses (docker via snap ~5 s, journal ~2 s); run them side by side.
    with ThreadPoolExecutor(max_workers=6) as pool:
        f_jobs = pool.submit(check_cron_jobs, now)
        f_services = pool.submit(check_services)
        f_certs = pool.submit(check_certs, now)
        f_errors = pool.submit(app_errors)
        f_portal = pool.submit(portal_service_info)
        disk = check_disk()
        backup = check_backup(now)
        jobs, services, certs, errors, portal = (
            f_jobs.result(), f_services.result(), f_certs.result(),
            f_errors.result(), f_portal.result())
    sections = {
        'jobs': worst(j['status'] for j in jobs),
        'disk': disk['status'],
        'backup': backup['status'],
        'services': worst(s['status'] for s in services),
        'certs': worst(c['status'] for c in certs),
        'app_errors': errors['status'],
    }
    return {'now': now, 'jobs': jobs, 'disk': disk, 'backup': backup,
            'services': services, 'portal': portal, 'certs': certs,
            'app_errors': errors, 'sections': sections,
            'overall': worst(sections.values())}


def problems_list(report):
    """Flat [(status, area, text)] of everything not ok, worst first (for the alert email)."""
    out = []
    d = report['disk']
    if d['status'] != 'ok':
        out.append((d['status'], 'Disk', f"/ is {d['pct']}% full ({d['free'] // 2**30} GB free)"))
    b = report['backup']
    if b['status'] != 'ok':
        out.append((b['status'], 'Backup', b['message']))
    for s in report['services']:
        if s['status'] != 'ok':
            out.append((s['status'], 'Service', f"{s['label']} ({s['name']}) is {s['state']}"))
    for c in report['certs']:
        if c['status'] != 'ok':
            out.append((c['status'], 'SSL', f"{c['domain']} expires in {c['days_left']} days"))
    for j in report['jobs']:
        if j['status'] in ('warn', 'error'):
            detail = '; '.join(j['problems'])
            if j['error_line']:
                detail += f" -- {j['error_line']}"
            out.append((j['status'], 'Cron', f"{j['name']} ({j['schedule_text']}): {detail}"))
    e = report['app_errors']
    if e['status'] != 'ok':
        top = e['groups'][0]['pattern'] if e['groups'] else ''
        out.append((e['status'], 'Portal errors', f"{e['total']} in {e['hours']}h. Most common: {top}"))
    return sorted(out, key=lambda p: -STATUS_ORDER[p[0]])
