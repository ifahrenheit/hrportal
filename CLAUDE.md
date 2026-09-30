# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Internal HR portal ("Leave4Day" / hrportal) for Cohere Outsourcing Philippines, live at `hrportal.cohere.ph`. **This checkout is production**: the systemd service runs directly from `/var/www/html/leavesystem`, and it uses real MySQL data. There is no staging copy and no test database.

## Commands

```bash
# Restart the live app after any Python change (Apache reverse-proxies to 127.0.0.1:5001)
sudo systemctl restart leavesystem
journalctl -u leavesystem -n 50 --no-pager      # confirm a clean start

# Tests use stdlib unittest, not pytest. Run them from the repo root with .env loaded
set -a; . ./.env; set +a
./venv/bin/python -m unittest discover tests -v
./venv/bin/python -m unittest tests.test_routines -v                                  # one file
./venv/bin/python -m unittest tests.test_routines.WindowLogicTests.test_window_starts  # one test

# Syntax gate before restarting (app.py is ~16k lines; a syntax error takes the whole portal down)
./venv/bin/python -c "import ast,sys; ast.parse(open('app.py').read())"
```

There's no build step, linter or formatter. The frontend is Jinja + Bootstrap 5 + vanilla JS.

Some tests import `app` and hit the real `central_db`. Those tests must create rows with a `ZZTEST-` style prefix and delete them in `tearDownClass` (see [tests/test_routines.py](tests/test_routines.py)). Other tests are fully mocked with fake cursors (see [tests/test_undertime_notify.py](tests/test_undertime_notify.py)).

## Architecture

### One process, a monolith plus blueprints
- [app.py](app.py) is the Flask app, served by `venv/bin/python app.py` under `leavesystem.service`. Most legacy routes live in it directly: leave, file requests (OT/FTS/CWS/RDW/Magic CWS), material and facilities requests, PIM, admin and reports.
- Newer features are blueprints in [modules/](modules/), [blueprints/](blueprints/), [break_log/](break_log/), [coaching.py](coaching.py) and [memos.py](memos.py). They're all imported and registered near the top of `app.py`, except `survey_bp`, which is registered further down. New features should be a new blueprint module registered there, not more code in `app.py`.
- [helpers/](helpers/) holds shared logic that the web app and cron scripts both use:
  - `payroll_period.py`
  - `ir_autofile.py`, which files incident reports automatically
  - `orangehrm_db.py`

### Two MySQL databases, no ORM
Access is raw PyMySQL with `DictCursor`, so rows are dicts. Placeholders are `%s`. Inside f-string SQL, a literal `%` has to be written `%%`.

| Helper | Database | Holds |
|---|---|---|
| `get_db()` (app.py; `DB_*` env) | `orangehrm2` | Legacy OrangeHRM tables: `hs_hr_employee`, `ohrm_user`, `ohrm_leave`, `ohrm_leave_entitlement` (leave balances, the payroll source of truth), plus `leave4day_*` tables |
| `get_db_connection()` ([db_core.py](db_core.py); `MAIN_DB_*` env) | `central_db` | Portal data: `gsheet_employees` (employee master, synced from Google Sheets via Apps Script), file requests, incident reports, and so on |

- `get_central_db()` in `app.py` also reaches `central_db`, but through the orangehrm credentials.
- Cross-database joins such as `central_db.gsheet_employees` ⋈ `hs_hr_employee` need `COLLATE utf8mb4_unicode_ci` on the join key.
- Employees are identified by `employee_id` (a string, shared by both databases) or by `emp_number` (OrangeHRM's integer key). Don't confuse the two.

### Auth and permissions
- Login is Keycloak OIDC through authlib, with a portal-password fallback.
- Session keys:
  - `session['user']`
  - `session['is_admin']`
  - `session['permissions']`: a dict of `can_*` flags for sub-admins
- The decorators in `app.py` are `login_required`, `admin_required` and `permission_required('can_x')`, plus `has_permission()`. The permission catalog lives in [permission.py](permission.py).
- Approval routing is in `get_supervisor_email()`. It checks three sources in order:
  1. the `leave4day_supervisor_assignments` override
  2. `gsheet_employees.approver`, the SOM email
  3. OrangeHRM `hs_hr_emp_reportto`
- CSRF is manual through [csrf.py](csrf.py): the form field is `csrf_token`, or the `X-CSRF-Token` header for fetch calls.
- Email goes out through `send_email()` / `send_email_as()` (SMTP on a background thread). Recipients can be passed as a comma-joined string.

### Templates
Pages `{% extends "base.html" %}`. The blocks are `title`, `page_title`, `content` and `scripts`. Templates are grouped in subfolders by feature under [templates/](templates/). Icons are **Lucide** (they were migrated from Bootstrap Icons, but `permission.py` still stores the old `bi-*` names).

### Cron scripts
These run as root's crontab using `venv/bin/python`. Each is a standalone process that doesn't go through the Flask app:
- [tardiness_notify.py](tardiness_notify.py), [overbreak_notify.py](overbreak_notify.py) and [undertime_notify.py](undertime_notify.py):
  - They count violations per payroll period in `*_cycle_state` tables, file IRs through `helpers/ir_autofile.py`, and email memos.
  - Each writes a `*.log` in the repo root.
  - `undertime_notify.py` does a dry run unless you pass `--live`.
- `scripts/sync_supervisor_from_gsheet.py --live`, `scripts/update_status.py`, `scripts/sync_ohrm_emails.py`, `scripts/coaching_reminders.py --send`, `coe_auto_release.py` and `auto_convert_scheduled_leaves.py`.
- Run cron-style scripts **without** `--live`/`--send` first. Live runs file real IRs and send real email.

## House workflow

- **Editing `app.py`:**
  1. Keep a timestamped backup (`app.py.bak.YYYYMMDDHHMMSS`). These backups are gitignored.
  2. Pass the `ast.parse` gate.
  3. Restart the service and check `journalctl`.
- **Before any destructive SQL:** run a `SELECT`/count of the rows it will touch first.
- `/ship <feature>` ([.claude/commands/ship.md](.claude/commands/ship.md)) runs the full pipeline through the agents in `.claude/agents/`: developer → tester → reviewer → security → database → apply and restart.
- At the start of each session, read `HANDOFF.md` and follow the `session-handoff` skill's resume mode. At the end of a task, run it in write mode.
- Feature docs live in [docs/](docs/).
