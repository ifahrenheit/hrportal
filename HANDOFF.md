# Handoff Log — leavesystem

## Project overview
- **App:** Cohere HR portal ("Leave4Day" / hrportal), Flask monolith [app.py](app.py) plus blueprints in `modules/`, `blueprints/`, `break_log/`, `coaching.py`, `memos.py`. Frontend: Jinja + Bootstrap 5 + vanilla JS, Lucide icons.
- **DBs:** `orangehrm2` via `get_db()` (OrangeHRM tables, leave balances) and `central_db` via `get_db_connection()` / `get_central_db()` (portal data, `gsheet_employees`). Raw PyMySQL with DictCursor.
- **Deploy:** this checkout *is* production (`hrportal.cohere.ph`). systemd `leavesystem` runs `venv/bin/python app.py` on 127.0.0.1:5001 behind Apache. Restart with `sudo systemctl restart leavesystem`. `webhook.cohere.ph` (Apps Script sheet sync, biometric queue) also proxies to it (`modules/sheet_webhooks.py`); the old employee-sync service is retired.
- **Cron:** tardiness/overbreak/undertime notify scripts and others under root's crontab (see CLAUDE.md).

---

## 2026-11-24 — Payroll Readiness page (Analytics), nav-parity checker repaired
Commit: `b21674d` (this work uncommitted) · Branch: `main`

### What was built / changed
- **Analytics › Payroll Readiness** `/admin/payroll-readiness` (blueprint `payroll_readiness`). For one payroll period it lists what is still open, with who it's waiting on and days open: pending OT/FTS/CWS/RDW/Magic CWS, missing punches with no FTS filed (`fts_pathb_incidents`), and unsettled leave (pending, SL pending HR, `credit_pending`). It also notes requests pending from earlier periods. CSV export and "Copy table". — `modules/payroll_readiness.py`, `templates/payroll_readiness/index.html`, `docs/payroll-readiness.md`; registered in `app.py` after `system_health_bp`; nav links in `_sidebar.html` + `_topnav.html` (Analytics, next to OT Hours, plus the Attendance subnav).
- Tests: `tests/test_payroll_readiness.py` (15). Full suite 589 OK.
- `/admin/nav-parity` was broken for every profile (`break_log_user_access`, `docs_can_access`, `ml_can_view` had no stand-ins). Stand-ins added in `app.py`; it runs again.

### Why / decisions
- Access: admins plus `can_reports` or `can_ot_hours`, with no new permission column. Renalyn Comeda (Reports + OT Hours + File Requests) looks like the payroll user; 12 sub-admins have Reports.
- Default period = the most recently closed one (payroll is run for it). "Waiting on" reuses `get_approver_names()`, so it matches File Requests › Approvals.
- Missing punches already covered by a Pending/Approved FTS are dropped, using the FTS engine's matching rule (IN same day; OUT same or next day). Without this, 6 of 15 rows for Nov 8–22 were double-counted.
- First real run (Nov 8–22): 14 open items (2 FTS, 3 OT pending, 9 missing punches). The current period showed one LWOP pending 43 days.

### Open items / next steps
- [ ] Commit + push this work (asked the user).
- [ ] `/admin/nav-parity` now shows real, older differences: top nav only has On Shift, Overbreak Review, Inventory Medicine, Documents library, Requirements; sidebar only has Night Diff and Coaching index for some roles. Not touched.
- [ ] Next in the user's list: #10 approval reminders, #11 TL home page, #9 Employee 360.

---

## 2026-11-23 — System Health page, nightly DB backups, dead payroll-email cron removed
Commit: `6215f29` · Branch: `main`

### What was built / changed
- Earlier the same day the pending work was committed and pushed as 4 commits: Numa Stats `64223da`, AWOL CWS skip `367d6ff`, IR visibility `1e09654`, SL conversion tier filter `fa0cbae`.
- **Removed 4 dead cron lines**: `cohere_dashboard/send_{ot,rdw,cws,fts}_email.php` (cutoff-day lists to payroll@cohere.ph). The log path was typo'd as `/var/www/htm/...`; that folder doesn't exist, so cron's shell failed the redirect and **never ran them since May 2025**. No one missed them, so the user said to delete them. PHP files left on disk. Crontab before the change: `/root/crontab-backups/root.crontab.20261123215339`.
- **Nightly MySQL backup**: `scripts/backup_databases.sh`, cron `0 3 * * *` → `backup_databases.log`. Dumps `central_db`, `orangehrm2`, `orangehrm` (`--single-transaction`, all InnoDB) to `/var/backups/mysql/daily/<db>-YYYYmmdd-HHMM.sql.gz`, Sunday hard-link to `weekly/`, keeps 7 daily + 4 weekly. Verifies gzip + "Dump completed", skips if under 3 GB free, writes `/var/backups/mysql/last_status.json`. Restore steps in the script header.
- **Admin › System Health** `/admin/system-health` (blueprint `system_health`, full admins only): overall/cron/backup/disk/services/errors tiles, "Needs attention" list, cron job table (Not OK / All), backup files, services + Keycloak containers, portal errors (24 h, grouped), DB sizes, SSL certs. Cached 2 min, **Check now** forces a run. — `helpers/system_health.py` (checks), `modules/system_health.py`, `templates/admin/system_health.html`; registered in `app.py` after `leave_hold_bp`; nav links in `_sidebar.html` + `_topnav.html` (Administration, admin-only; also in the Admin Tools subnav).
- **Daily alert script** `scripts/system_health_check.py` (dry run by default, `--send`, `--always`): emails only when something is at Problem level. Not in cron yet.
- Tests: `tests/test_system_health.py` (35, offline; page tests patch `get_report`). Backup: `app.py.bak.20261123220454`.

### Why / decisions
- Cron jobs are read from `crontab -l`, so new jobs show up by themselves. Each job's last scheduled run comes from a small built-in cron evaluator (croniter isn't installed). It's compared against the cron journal (did it fire?), the log's mtime (did it write anything?) and the last 25 log lines (Traceback/ERROR). A missing log folder is an error, which would have caught the payroll-email bug.
- `leavesystem.service` runs with `PATH=venv/bin`, so `_bin()` finds tools under the system paths. Docker is read from `/var/run/docker.sock`, because the snap CLI hangs without a login environment.
- Journal reads use `-g` (about 3x faster). The checks run in threads; a full run takes about 5–10 s, hence the cache.
- DB password for backups lives in `/root/.config/db-backup.cnf` (600, outside the repo — the repo is public).

### Config / environment
- New `.env` key (not set yet): `HEALTH_ALERT_TO` (comma-separated).
- Cron added: `0 3 * * * scripts/backup_databases.sh`. Removed: the 4 `send_*_email.php` lines.

### How to verify
- `/admin/system-health` as an admin. `cat /var/backups/mysql/last_status.json` after 03:00. `venv/bin/python scripts/system_health_check.py` (dry run).

### Later the same day: cleanup round
- Cron: removed `/var/www/html/automation/check_mysql_disk.sh` (script gone) and `/root/check_disk.sh` (only echoed; the health page covers disk). Crontab copies are in `/root/crontab-backups/`.
- Cron: the 08:00 `killall -u root node` had **never matched anything**, because VS Code's processes aren't named `node`. It's now `pkill -f '/root/[.]vscode-server/'` and logs to `/var/log/vscode_cleanup.log`. Purpose per the user: end VS Code remote sessions daily so they don't pile up. It doesn't touch `calendar-app` (PM2).
- Disk 75% → 58%: removed 7 old VS Code server versions (kept the 2 running), npm/pip/composer/typescript/playwright caches, and ran `journalctl --vacuum-size=1G`. Kept `~/.cache/chroma` (RAG embeddings).
- `.env`: quoted 8 values that had spaces (SMTP_FROM_NAME, IR_SGA, IR_OVERHEAD, IR_ALL_ACCESS, IR_FROM_NAME, TARDINESS_SENDER_NAME, ONBOARDING_FROM_NAME, COE_FROM_NAME). Checked that all 81 keys parse the same before and after. Shell `. ./.env` is now clean. `.env` and its backups are now `chmod 600` (only the root-run portal reads them).
- 61 `*.bak*` files moved into `backups/` (mirrored paths; `.backups/` merged in). `backups/` is gitignored, and CLAUDE.md's workflow now says to put backups there.
- Tests: three stale tests fixed. AWOL and undertime "empty schedule" now check which tables are queried instead of counting connections. The undertime leave-covered test now expects the row shown with a leave badge (behavior changed on purpose in `1022f01`). Full suite: 563 OK.
- User decisions: the schedule sheet's old months (`#N/A` rows in March/July, Glane listed twice in June) are to be ignored. Uploaded files aren't backed up (users keep local copies). No off-server backup for now (Drive is nearly full).

### Open items / next steps
- [x] Alert live: `HEALTH_ALERT_TO='andrew.tacdoro@cohere.ph'` in `.env` (comma-separated, add more there, no restart needed). Cron `30 7 * * *` runs `scripts/system_health_check.py --send` → `system_health_check.log`. A test email was sent 2026-11-23 23:07 and the mail server accepted it.
- [x] Committed and pushed: `a14b257` System Health + backup + alert, `101a36b` sheet webhook fixes, `672396b` test fixes, `6215f29` backups/ folder.
- [ ] Optional, needs the user's OK (blocked by the permission check): cap the journal for good with `/etc/systemd/journald.conf.d/size.conf` → `SystemMaxUse=1G`.
- [x] `/webhook/schedule-sync` now skips `#N/A`/blank IDs and duplicate employee+date rows, with one info log line per sync (`_clean_schedule_rows` in `modules/sheet_webhooks.py`, doc in `docs/sheet-webhooks.md`). The Portal errors warning clears once the old errors age out of the 24-hour window.
- [x] System Health shows a plain-English description for every cron job: `JOB_DESCRIPTIONS` in `helpers/system_health.py`, matched on part of the command. **Add a line there when adding a cron job**, otherwise the page shows the crontab comment.
- [x] Crontab now has `MAILTO=""` under the header: cron mail to root had been undeliverable since Nov 2025. `sync_userdata.py`, `process_timerecords.php` and `sync_csat.php` now append to `/var/log/<name>.log`. The 08:00 VS Code cleanup always logs (`pkill exit=0/1`) to `/var/log/vscode_cleanup.log`.
- [x] System Health: a job with no log and no start yet, whose last slot came before the last crontab save (or before the 50-hour journal window, e.g. the Jan 1 rollover), shows OK with "Waiting for its first run: <next run>". If a slot after the save passes with no start, that's a warning. `CronSchedule.next_run()` was added.
- [x] Deleted ~274k undeliverable cron mails (1.1 GB) from `/var/spool/mqueue-client` with the user's OK, and recreated the folder (smmsp:smmsp, 2770). Disk is at 56%. New mail can't pile up there: root's crontab has `MAILTO=""`.
- [x] Employee-sync deadlocks fixed: `batch_sync` now commits per row and retries MySQL 1213/1205 up to 3 times (`_write_row` in `modules/sheet_webhooks.py`). Before, a deadlock (~00:47, when the employee and schedule syncs overlap) silently rolled back the earlier rows of its batch; they self-healed on the next sync because their `last_synced_at` was rolled back too. Tests: 574 OK.
- [ ] Check after the 00:47 sync on 2026-11-24: `sheet_webhooks.log` should show `Skipped N row(s)...` and no `#N/A`, duplicate or deadlock errors.
- [ ] Next, per the user: payroll cutoff readiness, Employee 360, approval reminders, TL home page (#8–11).

---

## 2026-11-18 — Numa Stats page (NUMA Stat Dashboard sheet → portal)
Commit: `1750a75` (code uncommitted) · Branch: `main`

### What was built / changed
- New page `/numa-stats/` (blueprint `numa_stats`): Monthly / Weekly views with a period picker, plus Daily once the sheet has it. It has team tiles, an agent table (conversations, WhatsApp CSAT, email CSAT, email SLA, median first reply, median response) with ▲/▼ arrows against the previous period and a team-median row, and a month-by-month grid like the sheet's Stat Board. Admin **Sync now** button. — `modules/numa_stats.py`, `templates/numa_stats/index.html`
- Sync from the sheet: `helpers/numa_stats.py`, cron script `scripts/sync_numa_stats.py` (dry run by default, `--live`, `--force`).
- Nav link "Numa Stats" (My section) in `templates/nav/_sidebar.html` and `templates/nav/_topnav.html`. It's gated by the template global `numa_stats_can_view()`, which is registered in `app.py` after `csr_bp` and also mocked in `/admin/nav-parity`.
- Tests: `tests/test_numa_stats.py` (20, fully mocked). Doc: `docs/numa-stats.md`.
- Backups: `app.py.bak.20261118212042`, `templates/nav/_sidebar.html.bak.20261118212042` and `_topnav.html.bak.20261118212042`.

### Why / decisions
- The sheet (owned by kim.bolongaita@numastays.com, ID `1fewDc0GG1GDZuvyMLJ5U8K_aupnHC_-ZXp87Z_HGK8s`) has "MoM Numa CS" / "WoW Numa CS" tabs that are just C1-dropdown FILTER() views. The portal therefore reads the **raw** tabs (`Raw`, `Raw-Weekly`, future `Raw-Daily`) and matches columns by header name. Reading the views would only ever give whichever month is currently selected. The sheet also matches on month name or week number only, which will collide in 2027; the raw tabs carry full dates.
- Reads go through the existing Drive service account (`GOOGLE_DRIVE_SA_KEY_FILE`, `timestamp-drive-reader@…`). Kim shared the sheet with it as Viewer, and the user enabled the Google Sheets API on the GCP project.
- Each sync replaces one period type's rows in a single transaction. A tab missing its headers, with no rows, or shrinking below 50% of what's stored is skipped, so the data is kept. A MySQL `GET_LOCK` stops the cron and the button from running a sync at the same time.
- Access is decided by the user: **whole-team view** for admins, Active/Training `gsheet_employees.group_name` IN ('Numa CS','Numa BO'), and the `approver` of such an employee (Jareth, group Finest). It's cached in the session for 10 minutes. No new `can_*` permission, which avoids altering `leave4day_sub_admins`.
- Roster: email local part with `.` → `_` gives the Numa username (checked against all 17 CS agents). Agents with no data for the selected period type are hidden, so BO agents and the 5 GYG-account Numa CS agents don't show yet. Numa's own staff in the raw tabs are stored but never shown.

- Later the same day:
  - Team CSAT figures switched from median to a plain average of the agents. The user's call.
  - Team split added: **GX Team** / **GX Night Shift**. It comes with a Team filter, per-team sections and team rows, and an admin **Edit teams** dialog backed by `numa_stats_teams`. Agents without a row are GX Team. Seeded night shift: clavil_mancao, april_dequino, peter_inson, elen_lumbab.
  - The user expected a team WhatsApp CSAT of 91.8 for November, but the portal's average is 89.82. No calculation from the sheet's columns gives 91.8: the per-agent percentages are rounded and there are no response counts. It most likely comes from Looker across all responses, and needs a response-count column in the raw tabs.

- Later still:
  - Team rows show **total** conversations, and CSAT **weighted by conversations**. Team CSAT shows 2 decimals.
  - New **Teams · Looker** page `/numa-stats/teams` (tabs link it with the Agents page). It's fed from Jareth's "Jareth SLA Copy" sheet: `helpers/numa_team_stats.py` → `numa_team_stats`, hooked into the same sync. It holds Looker's own team figures for Team Cohere (= GX Team) and GX Team Night Shift, plus a Looker vs Agents comparison. See `docs/numa-stats.md`. Tests: `tests/test_numa_team_stats.py`.
  - This explained the user's 34.9 (running email SLA, Nov, Team Cohere = 655/1878) and 91.8 (Agent CSAT, Nov). The user's "4,110 GX conversations" is still not found in either sheet.

### Database changes
```sql
-- central_db (scripts/numa_stats_schema.sql)
CREATE TABLE IF NOT EXISTS numa_stats (... UNIQUE (period_type, period_start, username));
CREATE TABLE IF NOT EXISTS numa_stats_sync_log (...);
CREATE TABLE IF NOT EXISTS numa_stats_teams (username PK, team, updated_by, updated_at);  -- + 4 night-shift rows
CREATE TABLE IF NOT EXISTS numa_team_stats (block, period_type, period_start, team, metric, value; UNIQUE block+period+team+metric);
```
- First live load: month 304 rows, week 1087 rows.

### Config / environment
- Optional `.env` keys (with defaults in code): `NUMA_STATS_SHEET_ID`, `NUMA_STATS_MONTH_TAB`, `NUMA_STATS_WEEK_TAB`, `NUMA_STATS_DAY_TAB`.
- Root cron added: `5 * * * * cd /var/www/html/leavesystem && venv/bin/python scripts/sync_numa_stats.py --live >> numa_stats_sync.log 2>&1`
- `leavesystem` restarted at about 21:23 and came up clean.

### How to verify
- Test client against the live DB: CS agent, BO agent, Jareth and an admin each get 200 and see the nav link. A non-Numa user is redirected and the API returns 403. November (17 agents, median 339) and week 46 match the sheet's MoM/WoW tabs, and the trend medians match the Stat Board median row.
- `./venv/bin/python -m unittest tests.test_numa_stats -v`

### Open items / next steps
- [ ] Ask the user where "4,110 GX conversations (Nov)" comes from. The per-agent sum is 3,993 and Looker's Team Cohere unique count is 3,023.
- [ ] (Probably superseded by the Teams page) Ask Kim or Jareth for a **CSAT response count** column in Raw/Raw-Weekly, so the team CSAT can be weighted by responses and match Looker (user expects 91.8 for November WhatsApp).
- [ ] Ask Kim or Jareth what "Email SLA %" now means (about 65% in January, about 5% since mid-year). The page shows it neutrally.
- [ ] When `Raw-Daily` is added, check it uses the same headers and a full date in a `Created …` column. The Daily button appears after the next sync.
- [ ] Real browser check by a Numa agent (phone width, dark mode).
- [ ] Commit the Numa Stats files: `helpers/numa_stats.py`, `modules/numa_stats.py`, `templates/numa_stats/`, `scripts/numa_stats_schema.sql`, `scripts/sync_numa_stats.py`, `tests/test_numa_stats.py`, `docs/numa-stats.md`, plus the Numa hunks in `app.py` and both nav templates. The other uncommitted files belong to earlier work.
- [ ] Existing test failures, not caused by this work (they still fail with the Numa nav check switched off): `test_awol_report` / `test_undertime_report` `test_empty_scheduled_renders_empty_state_no_crash`, and `test_undertime_report.test_leave_covered_short_shift_is_excluded`.

---

## 2026-11-17 — Supervisor Break Logs: "Overbreaks only" filter
Commit: `be8dece` (code uncommitted) · Branch: `main`

### What was built / changed
- `/supervisor-break-logs`: **Overbreaks only** toggle (URL `?over=1`; applies immediately, no Apply needed). It keeps only the agent + shift days whose total break is over 1:30, and only active breaks already over 1:30 including the running one. Totals over 1:30 are red with "+N min", and the Completed heading shows "· N overbreaks (over 1:30)". — `templates/break_log/supervisor.html`
- `/api/supervisor/breaks?over=1` filters completed rows server side, so the stats cards and the CSV (`overbreaks.csv`) match the table. — `break_log/routes.py`
- Fix: the Completed table used to group by agent only, so "Total Today" summed the whole date range. It now groups by agent + shift_date.
- Backups: `*.bak.20261117230515` for both files.
- Later the same day:
  - Fixed filter alignment. Date panels now show as `block`; the toggle CSS uses a `.filter-group .over-toggle` selector.
  - Added **Group** (`gsheet_employees.group_name`) and **Team (TL)** (`gsheet_employees.tl`) filters, sent as API params `group` / `team` (EXISTS on the agent's *current* sheet row).
  - The Agent list narrows by account + group + team.
  - A TL whose email maps to exactly one `tl_view_map.tl_name` opens on their own team, marked "(my team)". `team=all` overrides it.
  - Backups: `*.bak.20261117232826`.
  - Tested: team/group rows match the sheet. Flor's login preselects Flor. Only 5 TLs currently have Break Log data this cycle (GYG has barely started).

### Why / decisions
- The threshold is the shift total over 90 min (strictly greater), the same as `/overbreak-review` (`OVERBREAK_LIMIT_SEC`).
- This page isn't scoped by TL. Anyone with supervisor/monitor access sees all `can_log` agents. Not changed.
- Explained to user: automatic overbreak IRs (`overbreak_notify.py`, cron every 2 h at :15) read the **Google-Sheet `overbreak_records`**, not `break_logs`. Rule 1 is 3 valid instances per payroll cycle, Rule 2 is 2 instances over 3.5 h total, Rule 3 is a single instance over 2 h. The memo goes to the TL via `gsheet_employees.tl` → `tl_view_map`. `/overbreak-review` is the portal-based manual Create IR / Waive (GYG from 2026-12-01).

### How to verify
- Test client, Nov 8–17: 130 agent-days in total, 7 over 1:30, and `over=1` returns exactly those 7 (17 rows). The CSV is named overbreaks.csv. The page JS passes `node --check`. Service restarted clean ~23:06.

### Open items / next steps
- [ ] Real browser check of the toggle by a TL.
- [ ] Commit `break_log/routes.py` + `templates/break_log/supervisor.html` (other uncommitted files belong to earlier work).
- [ ] Possible follow-up: scope the supervisor page to the TL's own team (`gsheet_employees.tl`).

---

## 2026-11-16 — Break Log: false "119 minutes" overbreak alert fixed (page race)
Commit: `4b5c54f` (fix) · `d3f5bc2` (Break Log Access) · Branch: `main`

### What happened
- Jessa Barcastigue (230306-37) got a "Break Time Exceeded, 119 minutes" browser notification at 01:06 on 2026-11-16 with only 59:41 used. Then a banner wrongly said her break was "automatically closed at the 2-hour limit".
- Cause: a `check_active` poll (fired when she returned to the tab) was read by the server just before her End click committed, and its reply arrived after the page had stopped the timer. The page restarted the ended break, so completed (59:41) + "running" (59:41) = 119 min. Her next End click failed (`END FAIL` 01:06:22) and the page assumed the 2-hour cap. Her click after that created a 3-second break (id 7426 is real; **7432 is the 3s artifact**, left in place). Server data and `overbreak_notify.py` were never affected (shift total 59:44).

### What was built / changed
- `templates/break_log/break_log.html` (script rewritten):
  - `syncSeq` drops poll replies that were sent before a Start/End click; `endedHere` never restarts a break this page ended; if the running break also appears in the completed list, it isn't counted twice and the page re-syncs.
  - 85/89-minute warnings and the overtime alert/notification now fire only after `verifyThen()` re-reads check_active + get_logs from the server.
  - End shows "Break ended: mm:ss recorded. Today's total … Remaining …" (15 s). A **Remaining / Over by** label sits under Total. Auto-closed rows are tagged in the logs table.
  - The ended-elsewhere banner now uses the server's `lastBreak.auto_closed`: it says "2-hour limit" only when true, otherwise "already ended at HH:MM (mm:ss recorded), no action needed".
- `break_log/routes.py`:
  - `_last_ended_break()` helper. `check_active` returns `lastBreak` when there's no active break. A failed End returns `already_ended` + `lastBreak`. A successful End returns `break_id` + the shift `total_break_time`. `get_logs` returns `auto_closed`.
  - Start/End timestamps are whole seconds, which fixes stored `duration` being 1 s off the table.
- Backups: `*.bak.20261116205022` for both files.

### How to verify
- Race reproduced in a Node harness with mocked fetch: the old page gives the exact "119 minutes" notification, the new page doesn't, and a genuine 95-minute break still alerts. Test suite: same 3 pre-existing failures. Service restarted clean 2026-11-16 ~20:55.

### Open items / next steps
- [ ] Watch for agent reports. `grep "END FAIL" break_log.log` should drop now that the page re-syncs instead of showing End.
- [ ] Optional: delete row 7432 (3 s, 230306-37), pending user OK.
- [x] Committed: `d3f5bc2` Break Log Access, `4b5c54f` this fix.

---

## 2026-11-12 — Requirements page: completion filters + counts; earlier work committed
Commit: `f894c1f` · Branch: `main` (pushed; commits `4948352`..`f894c1f`)

### What was built / changed
- `/requirements`: **Completion** filter (incomplete / complete) and **Missing** filter (one requirement), plus a summary strip: Total / Complete / Incomplete cards (click to filter), overall completion bar, clickable missing-per-requirement chips — `templates/requirements/requirements.html` (template only, client-side JS).
  - Counts follow search / account / group / TL filters (not the completion/missing filters). Ticking a box recounts without hiding the row.
  - 2026-11-12 numbers: 176 employees, 45 complete, 131 incomplete; most missing NBI 104, UB# 71.
  - Later: **Employee status** filter (All / Active only / Training only, built from the statuses present on the page; 173 Active + 3 Training today) plus an amber badge next to non-Active names. Filter feeds the cards and chips. Default is All. Template only, uncommitted.
- Committed earlier uncommitted work: SL conversion active-only + Copy table (`578ba6d`), break auto-fix helper `fix_misdated_breaks` + `scripts/break_autofix.py` (`cf19daf`, cron already live hourly at :20), Break Log re-sync / sticky auto-close notice (`99286d7`), lead-researcher agent (`f894c1f`).

### IR video attachments (later 2026-11-12, uncommitted)
- IR filing form and IR comments accept **mp4 / webm / mov up to 50 MB each** (still max 4 files). Checked client-side and server-side (`check_uploads`) before the report/comment row is created; requests over 205 MB get 413 (`_limit_upload_size`). XHR upload with % progress. Videos render as `<video controls>` + Download; `.mov` served as video/mp4 so Chrome plays H.264 iPhone clips (HEVC won't play in Chrome on Windows; download still works). Range requests work (seeking).
- **Attachment access now checked**: `/incident-reports/uploads/<file>` allows admin, filer, the employee, `ir_role` (HR/SGA/TL), or anyone whose IR dashboard lists the report (`report_scope_conds`, extracted from `dashboard()`). Denials log `[IR] attachment denied`. The view page shows a note instead of files when denied. The report page itself is still viewable by any logged-in user with the link (not changed).
- Files: `modules/incident_reports.py`, `templates/incident_form.html`, `templates/incident_view.html` (+ `.bak.*` copies). Test suite: 3 failures, same as before the change.
- [ ] Try a real phone video upload in the browser; watch `journalctl -u leavesystem | grep "attachment denied"` for legit users being blocked.

### IR attachment editing (later 2026-11-12) — LIVE
- Schema `scripts/ir_attachment_edit_schema.sql` applied to central_db 22:35 with user's go-ahead (adds `removed_at/removed_by_id/removed_by_name/remove_reason` to both attachment tables + new `incident_attachment_log`); service restarted clean. End-to-end test on a ZZTEST IR passed (add, over-limit, reason required, double remove, comment remove, outsider 403, removed file admin-only, history shown); test rows and files deleted.
- Filer (or admin) can Remove (soft: hidden, row + file kept, reason required) and Add files on the IR page; comment authors can Remove their comment files. New routes `/incident-reports/attachments/add` and `/attachments/remove`. Every add (including at filing and in comments) and remove is logged with actor, time, original filename, size, reason → "Attachment history" on the IR page; admins also get "Removed files" with links (removed files are admin-only in `serve_upload`).
- `incident_view.html` hides the edit UI unless the view passes `attachment_log`, and uses literal URLs, so it renders fine under the still-running old code.
- [ ] Real browser check of Remove / Add files on an IR (test client only so far).

### Break Log Access page (2026-11-13, uncommitted) — replaces the hard-coded lists
- **Admin → Break Log Access** (`/admin/break-log-access`, admin or `can_settings`): per account + group checkboxes **Log breaks** / **Monitor** / **Overbreak Review**, change history. Applies immediately (rules cached 60 s in memory, save invalidates; user's account/group cached in session as `bl_emp`).
- Tables (central_db): `break_log_group_access` (PK account+group, blank = ''), `break_log_access_log`. Schema + seed: `scripts/break_log_group_access_schema.sql`, applied 2026-11-13 00:43; seeded with the old hard-coded rules (GYG/Numa/Arctic/Test log; Finest/RTA/QA/IT/TL/Trainer/BO TL monitor; Numa/Arctic overbreak review). Parity checked: 173 active employees same access, Overbreak Review same 39 days in last 60.
- Code: `break_log/access.py` (rules), `break_log/settings.py` (page), `templates/break_log/access_settings.html`; `break_log/routes.py` no longer has BREAK_LOG_ACCOUNTS / OVERHEAD_GROUPS / OVERBREAK_ACCOUNTS; Break Monitoring roster + account dropdown follow "Log breaks"; Overbreak Review filters by current group (`OVERBREAK_EMPLOYEES_SQL`), not `break_logs.account_type`. `/api/break` also checks Log breaks.
- New/unknown groups = everything off, shown as "New". Groups with overbreak_records in last 60 days are flagged "IRs come from the sheet" and ticking Overbreak Review for them asks for confirmation (GYG CS/BO/L2 today).
- **Overbreak Review "from" date** (`overbreak_review_from`): from that shift date the group's Break Log overbreaks show in the portal Overbreak Review (`OVERBREAK_SCOPE_SQL`). NULL = always. Sheet groups ticked without a date get today. All 10 GYG groups are on from **2026-12-01** (logged "Andrew Vincent Tacdoro (via Claude)").
- **Sheet auto IR is untouched (user, 2026-11-13 ~04:20):** "don't touch the auto IR for GYG". The `reviewed_in_portal` skip in `overbreak_notify.py` was removed (file restored to HEAD; `tests/test_notify_commit.py` too). GYG keeps automatic sheet IRs on Dec 1 and later, *and* shows in Overbreak Review.
- **IR mapping in Overbreak Review:** `_attach_existing_irs` in `break_log/routes.py` adds `existing_irs` and `in_sheet` to each pending row. It matches overbreak IRs (summary contains "break") filed for the same agent on the same date, plus SYSTEM sheet IRs whose summary table lists the date as `M/D/YYYY`, dated up to 20 days later (Rule 1/2 IRs cover several dates). Tardiness and absence IRs on the same day are ignored. The page shows a 📄 IR link (auto) or an "In sheet · no IR yet" badge. Waive pre-fills "Covered by IR-…", and Create IR warns when an IR already exists. Verified on real data: 4 auto IRs since Oct 8 list 12 dates, and exactly those 12 sheet rows map. Tests: `ExistingIrMatchTests` in `tests/test_break_log_access.py`.
- **Delegate rule:** the row has an IR link → Waive ("Covered by IR-…"). "In sheet · no IR yet" → the sheet may still file one at its next run (every 2 h); wait, or review normally. No badge → review normally.
- IP allowlist still in code (`ALLOWED_IPS`), on purpose.
- Tests: `tests/test_break_log_access.py` (6), new `test_portal_reviewed_row_marked_processed_without_ir` in `tests/test_notify_commit.py`. Suite: same 3 pre-existing failures.

### Investigation (no changes): holiday absence list Jan 1 / Feb 17
- Melvin Rubas, Khim Jun Mozo (Jan 1 + Feb 17), Fe Edilyn Tinapay: approved CWS in `cws_requests` (#551, #554, #555, #557) but absence sheet still codes LWOP. Frances Jackson: REST / sheet `CWS 2`.
- Jenny Nalangan, Renalyn Comeda, Kristele Hubero, Jeanilyn Gemeras: scheduled to work Jan 1, no CWS filed anywhere, no punches. User says they should be RD (unfiled CWS); left with TLs to confirm and fix the sheets.
- Note: `employee_schedules` rows are re-imported (all re-created 2026-11-11 21:01), so past schedules reflect the sheet now, not as it was.

### Deploy / run notes
- `sudo systemctl restart leavesystem` (clean, twice). Templates are cached (debug off), so template edits need a restart.

### How to verify
- `/requirements` → cards + chips show counts; click Incomplete / a chip to filter.

### Open items / next steps
- [ ] Browser check of the requirements summary strip in light + dark theme (not viewed after deploy; page needs login).
- [ ] Absence sheet: change Melvin / Khim / Fe holiday LWOP rows to CWS; TLs to confirm make-up dates for Jenny / Renalyn / Kristele / Jeanilyn (Renalyn also has a 0-day LWOP in OrangeHRM on Jan 1).
- [ ] Carried over: Reasons text for Qualified SL-conversion rows; optional `can_sl_conversion` permission; 4 pre-existing failing tests.

---

## 2026-11-10 — SL Conversion: active employees only, "Copy table" replaces Export CSV
Commit: `558f1be` (code changes below are **uncommitted**) · Branch: `main`

### What was built / changed
- Report lists **Active employees only** (gsheet `status` = Active, case/space-insensitive); Separated, other statuses and employees with no `gsheet_employees` row are skipped in `build_report` — `modules/sl_conversion.py`. Applies to table, summary cards and CSV. Page note added.
  - 2026: 199 → 149 rows; 2025: 234 → 119.
- "Export CSV" button replaced by **Copy table**: copies the rows shown (current filters + sort order) as tab-separated text with a header, for pasting into Excel/Sheets — `templates/admin/sl_conversion.html` (per-row `data-copy` JSON + script, clipboard API with textarea fallback).
- Tests: `test_no_master_row_excluded_and_name_fallbacks`, new `test_only_active_employees_listed` — `tests/test_sl_conversion.py` (58 pass).

### Why / decisions
- `?format=csv` route kept (only the button removed); tests still cover it.
- Active employees with an exit date (on notice), late regularization or AWOL stay listed as Not qualified with the reason.
- Access unchanged: no dedicated permission; `can_reports` or `can_entitlements` (or admin) opens it. User was fine with that.

### Deploy / run notes
- `sudo systemctl restart leavesystem` (clean, twice).

### How to verify
- `/admin/reports/sl-conversion` → only Active statuses; Copy table → paste into Sheets, 12 columns.

### Open items / next steps
- [ ] Commit `modules/sl_conversion.py`, `templates/admin/sl_conversion.html`, `tests/test_sl_conversion.py` (left uncommitted; unrelated uncommitted work also in tree: `modules/sheet_webhooks.py`, `templates/break_log/break_log.html`, `scripts/break_autofix.py`, `.claude/agents/lead-researcher.md`).
- [ ] Optional, offered: Reasons column text for Qualified ("Meets all rules") and below-80 h rows (12 in 2026 show "-").
- [ ] Optional: dedicated `can_sl_conversion` permission if the report needs sharing without Reports/Entitlements access.

---

## 2026-11-09 (3) — SL Conversion report (policy 3.2.2)
Commit: `122fc53` · Branch: `main` (pushed)

### What was built / changed
- `/admin/reports/sl-conversion` (blueprint `sl_conversion`, `modules/sl_conversion.py`, `templates/admin/sl_conversion.html`): read-only report, filters year / group / status / search, CSV via `?format=csv`. Access: admin, `can_entitlements` or `can_reports`. Nav links in `templates/nav/_sidebar.html` + `_topnav.html` (Analytics gate now also includes `can_entitlements`).
- Shipped via /ship: developer, tester (`tests/test_sl_conversion.py`, 57 tests), reviewer / security / database (no blockers), fixes, restart.
- Also committed in `122fc53`: leave filing hold, leave recalc, and the earlier uncommitted `add_ohrm_leave_to_map` attendance-report change in `app.py`.

### Rules implemented
- Unused SL = SUM(no_of_days - days_used) of the year's SL entitlement x 8 h. >=96 h 100 %, >=88 50 %, >=80 25 %, else not convertible (0.02 h tolerance). Released Mar 15 of year+1; current year shown as a projection.
- Not qualified: reg (operational_date + 6 months) after Jan 1; any AWOL in the year (portal AWOL in ACTIVE_STATUSES + `absence_records` code `AWOL`, approval not Disapproved/Exempted, dates de-duplicated); gsheet status not `Active`; any `exit_date` unless a later `promotion_date`.
- Needs review: sickness keywords in absence reasons / leave notes on VL/LWOP/AWOL (fuzzy; also matches family illness).
- CSV: cells starting with = + - @ get a leading `'`; `Cache-Control: no-store`.

### How to verify
- 2026 projection on 2026-11-09: 199 rows, 61 qualified (703.19 days), 6 needs review, 44 not qualified, 88 not convertible.

### Open items / next steps
- [ ] HR to confirm: Pending AWOL counts; `AWOL Half day` code not counted; Training not eligible; who may see sick-reason free text (now can_entitlements / can_reports).
- [ ] 2 Active employees (IDs 190601-01, -02) have no `operational_date`, so they show Not qualified until the sheet is fixed.
- [ ] Earlier years: AWOL entered only in `ohrm_leave` is not read (fine for 2026).
- [ ] 4 test failures that also fail on a clean HEAD: test_awol_report / test_undertime_report empty-state + leave-covered, test_documents full_flow.

---

## 2026-11-09 (2) — Leave Filing Hold switch (VL / Carry-Over / SL by group)
Commit: `a03d90a` (feature files uncommitted) · Branch: `main`

### What was built / changed
- New card **Admin › Settings › Leave Filing Hold** (`#leave-hold`, `can_settings` or admin): on/off switch, held leave types, held groups (checkboxes from active `gsheet_employees.group_name` with headcounts, All/None), message. POST `/admin/settings/leave-hold` — blueprint `leave_hold`, `modules/leave_hold.py`, registered next to `leave_recalc_bp`.
- `app.py` `file_leave`: `mark_balances()` flags held types (`on_hold`, `hold_message`); POST of a held type is refused with a flash (server-side, not only the greyed-out option). `admin_settings` passes `leave_hold=settings_context()`.
- `templates/file_leave.html`: held options `disabled` + "(on hold)", warning banner. `templates/admin/settings.html`: the card + confirm on save.
- `tests/test_leave_hold.py` (pure logic, 7 tests).

### Why / decisions
- User wants VL/CO/SL filing paused for everyone except Numa, SGA and Finest, to be turned back on later; groups editable.
- Held = **all groups except Numa CS, Numa BO, SGA, Finest** (TL, QA, RTA, IT included). Matching is by `group_name` (case-insensitive); employees with no gsheet row are never held. New groups are not held until ticked.
- Only the agent's own `/file-leave`. Admin › File Leave for Employee (`admin_file_leave`) is deliberately not blocked, so HR can file exceptions. Already-filed leave untouched.
- If the lookup errors, filing is allowed (fail-open). Don't add `COLLATE utf8mb4_unicode_ci` to the `hs_hr_employee` ⋈ `gsheet_employees` join: `hs_hr_employee.employee_id` is utf8mb3 and it errors (caught in testing, would have silently disabled the hold).

### Database changes
```sql
-- orangehrm2.leave4day_settings, new keys (varchar(255) values)
INSERT INTO leave4day_settings (setting_key, setting_value) VALUES
 ('leave_hold_enabled','0'),
 ('leave_hold_groups','["Arctic CS","BO","BO TL","CS","IT","L2","L2 TL","QA","RTA","Test","TL"]'),
 ('leave_hold_types','[1,2,4]'),   -- Vacation, Sick, Carry-Over
 ('leave_hold_message','Filing for this leave type is on hold for now. Please contact HR if you need to file an urgent leave.');
```

### Deploy / run notes
- Backups `*.bak.20261109205847` (app.py, settings.html, file_leave.html). ast gate passed, service restarted clean.

### How to verify
- Test client with hold forced on: CS employee sees VL/SL/CO disabled + banner, Numa CS sees none; POST VL refused, no row inserted.

### Open items / next steps
- Hold turned **ON** by user 2026-11-09 21:04 (11 groups). Fixed: the AWOL holiday JS in `file_leave.html` re-enabled held options on date change; options now carry `data-hold="1"` and stay disabled.
- Committed in `122fc53`.

---

## 2026-11-09 — Cogonon parental leave fixed; "Recalculate Filed Leave" tool
Commit: `68bf5ea` (feature files uncommitted) · Branch: `main`

### What was built / changed
- New `/admin/emp-settings/recalc` (blueprint `leave_recalc`, `modules/leave_recalc.py`, `templates/admin/leave_recalc.html`, `can_work_mode` or admin). Enter employee ID + "applies from" date → preview old vs new hours → Apply. Re-runs each filed day (pending/approved/scheduled) through `calculate_deduction` with the current work mode + schedule; updates `leave4day_requests`, `ohrm_leave`, charges/refunds `ohrm_leave_entitlement.days_used` by the difference (skips credit_pending rows), logs to `central_db.employee_audit_log` (`change_source='leave_recalc'`).
- `app.py` `admin_emp_settings`: single-employee save redirects to the recalc preview (from 1st of month) when the employee has leave filed this month or later; bulk save flashes a reminder. Blueprint registered next to `file_for_emp_bp` with injected helpers.
- `templates/admin/emp_settings.html`: "Recalculate Filed Leave" button + note.
- `tests/test_leave_recalc.py` (mocked, 5 tests).

### Why / decisions
- Leave hours are frozen at filing; work mode / gsheet schedule changes never touched filed leave (Cogonon filed 2026-11-04 20:29, switched to 5-day 20:30).
- Never adds/removes rows (5-day rest days have no row; 4-day rest days are 0-h rows). Skips holidays, AWOL, custom-hours rows (hours not in 0/4/5/8/10).
- The from-date matters: an earlier date would "fix" leave that was correct under the old mode (e.g. Cogonon's Oct VL at 10 h).

### Database changes
```sql
-- orangehrm2, manual fix, emp_number 227 (Mary Joy Cogonon), leave_request_id 4128, Parental leave Nov 3-6
UPDATE leave4day_requests SET hours_deducted=8, days_deducted=1, is_off_day=0 WHERE id IN (3010,3011,3012,3013);
UPDATE ohrm_leave SET length_hours=8, length_days=1 WHERE id IN (5798,5799,5800,5801);
UPDATE ohrm_leave_entitlement SET days_used=4.0 WHERE id=2499;  -- was 3.75
```

### Deploy / run notes
- Backups `app.py.bak.20261109204827`, `templates/admin/emp_settings.html.bak.*`. ast gate passed, service restarted clean.

### How to verify
- `/admin/emp-settings/recalc?employee_id=250303-03&from=2026-11-01` → "already matches" (0 changes).

### Open items / next steps
- [ ] Commit `modules/leave_recalc.py`, template, test and the `app.py`/emp_settings edits (app.py also has other uncommitted changes from before this session).
- [ ] No Apply has been run through the tool on real data yet.

---

Older entries: [docs/handoff-archive.md](docs/handoff-archive.md)
