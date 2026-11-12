# Handoff Log — leavesystem

## Project overview
- **App:** Cohere HR portal ("Leave4Day" / hrportal), Flask monolith [app.py](app.py) plus blueprints in `modules/`, `blueprints/`, `break_log/`, `coaching.py`, `memos.py`. Frontend: Jinja + Bootstrap 5 + vanilla JS, Lucide icons.
- **DBs:** `orangehrm2` via `get_db()` (OrangeHRM tables, leave balances) and `central_db` via `get_db_connection()` / `get_central_db()` (portal data, `gsheet_employees`). Raw PyMySQL with DictCursor.
- **Deploy:** this checkout *is* production (`hrportal.cohere.ph`). systemd `leavesystem` runs `venv/bin/python app.py` on 127.0.0.1:5001 behind Apache. Restart with `sudo systemctl restart leavesystem`. `webhook.cohere.ph` (Apps Script sheet sync, biometric queue) also proxies to it (`modules/sheet_webhooks.py`); the old employee-sync service is retired.
- **Cron:** tardiness/overbreak/undertime notify scripts and others under root's crontab (see CLAUDE.md).

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

## 2026-11-05 — Absence Report FTS filing check + reminder emails; coaching draft 500 fixed; admin guide
Commits: `c9fc071`, `959308e` · Branch: `main`

### What was built / changed
- Coaching: saving a Draft with blank Topic/Discussion Notes gave a 500 (`IntegrityError 1048`, both columns NOT NULL). `_parse_form` now stores `''` — `coaching.py`.
- Absence Report (`/admin/absences`, `admin_absences` in `app.py`, `templates/admin/absences.html`):
  - **Status** checkbox filter (repeated `?status=`) via `templates/supervisor/_multi_filter.html`. Pills still count the whole range; "N of M record(s) shown" when filtered.
  - FTS IN/OUT rows show **FTS filed (status) / FTS rejected / FTS not filed** + "reminded <date>"; **FTS filing** filter (`?fts=filed|rejected|not_filed`).
  - **Email FTS reminders** button: modal preview (agents with unfiled/rejected FTS in the range), tick, Send. One email per agent, Reply-To = sender, "disregard if already filed".
- New `modules/fts_reminders.py` (blueprint `fts_reminders`, registered in `app.py`): `fts_filing_map(records)`, `POST /admin/absences/fts-reminders/preview` and `/send` (JSON, `X-CSRF-Token`, admin or `can_absences`).
- Admin guide + FAQ for Jovin/Jericho as a Claude Doc: https://claude.ai/artifact/JW1aoFqHfNCjacDUmZ5tsb

### Why / decisions
- FTS match: same employee + type, not Deleted/Cancelled; IN = shift date; OUT = shift date or next day when the shift crosses midnight (e.g. 7pm-6am filed under the next date; 1pm-12mn filed as next day 00:00). Unparseable shift: next-day OUT only if time <= 12:00. Best status wins: Approved > Pending > Rejected.
- Rejected counts as needing a reminder. Emails go to the agent only (TLs already get the 16:00 `fts_notify.py` digest from `/home/cohere/fts_pathb`). Send re-computes server-side and skips agents who filed meanwhile; agents reminded in the last 24h start unticked.
- Send is synchronous SMTP (per-recipient results), not the threaded `send_email`.

### Database changes
```sql
-- central_db, created on first use by modules/fts_reminders.py (_ensure_log_table)
CREATE TABLE IF NOT EXISTS fts_reminder_log (
  id INT AUTO_INCREMENT PRIMARY KEY, employee_id VARCHAR(50) NOT NULL, shift_date DATE NOT NULL,
  fts_type ENUM('IN','OUT') NOT NULL, email VARCHAR(255) NOT NULL, sent_by VARCHAR(255) NOT NULL,
  sent_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, KEY idx_emp_date (employee_id, shift_date));
```

### Deploy / run notes
- Backups `app.py.bak.20261105221912`, `templates/admin/absences.html.bak.2026110522*`. Service restarted 22:27, clean.

### How to verify
- `/admin/absences?date_from=2026-10-08&date_to=2026-10-22&status=FTS+IN&status=FTS+OUT` -> 14 rows, 11 filed, 3 not filed (Rachel Marikit, Honey Mae Tradio, Kimberly Rose Cobol). Test send used a mocked SMTP; ZZTEST log row deleted. One sample email sent to the user only.

### Open items / next steps
- [ ] Bug: Sub-Admins **IT Ticket Queue** / **HR Ticket Queue** checkboxes are never saved or loaded (`can_it_tickets`, `can_hr_tickets` missing from the save SQL and `get_sub_admin_permissions` in `app.py`). Only full admins see the Ticket Queue/Routine Tracker. Noted in the admin guide FAQ.
- [ ] Option offered, not built: CC the TL on FTS reminder emails.
- [ ] `.claude/agents/lead-researcher.md` is untracked (not part of this work).

---

## 2026-10-30 — Admin handover: successors, webhooks moved into the portal, credentials rotated
Commit: `8596b93` · Branch: `main`

### What was built / changed
- Documents library: company folders, employee A–Z tree, bulk move (`modules/documents.py`, `templates/documents/*`) — committed `fc85877` (was pending work).
- Inventory: Max Stock column; Copy Table includes min/max (`templates/admin/inventory.html`) — `c336776`.
- Login-as keeps the target's admin status (Employees.role Admin or `PORTAL_ADMIN_EMAILS`) and keeps the first `original_admin` on a nested login-as (`app.py` `login_as`) — `c9b7539`.
- IR emails: BCC comes from new `IR_EMAIL_BCC`; `helpers/ir_autofile.py` splits `OVERBREAK_NOTIFY_BCC` into a list (`modules/incident_reports.py`, `scripts/cron_incident_reminder.py`) — `4ccd454`.
- **employee-sync retired:** its live endpoints now live in `modules/sheet_webhooks.py` (registered in `app.py`): `POST /webhook/employee-sync`, `/webhook/schedule-sync`, `/webhook/break-autofix`, `GET /api/biometric/pending`, `POST /api/biometric/complete`, `GET /health`. Same requests/responses as the old UMMS-Core app. Logs to `sheet_webhooks.log`. Tests `tests/test_sheet_webhooks.py`; doc `docs/sheet-webhooks.md` — `f7bbea1`, `8596b93`.

### Why / decisions
- Successors: Jovin Lumapat and Jericho Garcia (full admins via `PORTAL_ADMIN_EMAILS`). The user keeps both own Admin accounts (consultant later); don't offer to remove them.
- `TARDINESS_NOTIFY_BCC`, `OVERBREAK_NOTIFY_BCC`, `SL_VERIFY_CC`, `IR_EMAIL_BCC` are **empty on purpose** (user had added themselves for testing; successors already get IRs). Keep the keys present: deleted, the notify scripts fall back to the user's Gmail and SL_VERIFY_CC to som@/wfm@/hr@.
- `MATERIAL_FINAL_EMAIL` left as the user: single-address check, admins pass anyway.
- Webhooks moved instead of re-pointing callers: Apps Script and the office biometric PCs keep calling `webhook.cohere.ph`; only the vhost changed.
- Old `cohere_dashboard` PHP site is ignored per user. Its 4 cron mailers (OT/RDW/CWS/FTS) never ran: their log path is `/var/www/htm/...` (typo, dir missing), so the shell aborts before PHP.

### Database changes
```sql
-- MySQL (as root): employee_sync password changed on all three accounts
ALTER USER 'employee_sync'@'localhost'     IDENTIFIED BY '<new>';
ALTER USER 'employee_sync'@'113.19.42.130' IDENTIFIED BY '<new>';
ALTER USER 'employee_sync'@'203.28.65.222' IDENTIFIED BY '<new>';
-- validate_password.policy = MEDIUM: needs mixed case, digit and a special char
```

### Config / environment
- `.env` new keys: `IR_EMAIL_BCC` (empty), `WEBHOOK_SECRET_KEY`. Changed: `MAILING_LIST_ALERT_EMAILS` (both successors), `SMTP_PASSWORD`, `KEYCLOAK_ADMIN_CLIENT_SECRET`, `MAIN_DB_PASSWORD`, `WEBHOOK_SECRET_KEY`.
- Rotated 2026-10-30: send_email@ SMTP password (also `calendar-app/.env` `MAIL_PASS`, `pm2 restart calendar-app`), Keycloak client `employee-management-service` secret, MySQL `employee_sync`, webhook secret (Apps Script "CSAT and MasterList sheet" > `EmployeeSync.gs` line 9 `SECRET_KEY`).
- Apache `webhook.cohere.ph` (`/etc/apache2/sites-available/employee-sync.conf` and `-le-ssl.conf`): proxies only `/webhook/`, `/api/biometric/`, `/health` to :5001; everything else 302 to hrportal. Backups `*.bak.20261030*`.
- systemd `employee-sync` stopped + disabled (files kept for rollback, steps in `docs/sheet-webhooks.md`).
- Moved public user-import files out of `cohere_dashboard/uploads/` to `/root/private/dashboard_uploads_20251113/`.

### How to verify
- `curl -s https://webhook.cohere.ph/health` → healthy; `tail sheet_webhooks.log` shows `Batch sync: N synced, 0 errors` after each Apps Script run (00/06/12/18h). First real run 2026-10-30 04:20: 972 synced, 0 errors.
- Full suite: 403 tests, only the 3 known pre-existing failures (awol/undertime boundary cases).

### Open items / next steps
- [ ] Re-rotate `WEBHOOK_SECRET_KEY` once more: the user's screenshot showed the full new key (and part of `MEMO_SECRET_KEY` = `.env` `MEMO_SYNC_SECRET`). Have the user copy from `.env`; never print secrets in chat.
- [x] 2026-11-02: dropped `employee_sync`@113.19.42.130 and @203.28.65.222; MySQL `bind-address` and `mysqlx-bind-address` set to 127.0.0.1 in `/etc/mysql/mysql.conf.d/mysql.cnf` + `mysqld.cnf` (backups in `/root/private/mysql_conf_bak_20261102204958/`), MySQL restarted. Before: 178 outside IPs had hit 3306. Verified portal, webhooks, calendar app, cron DB users all on localhost. `rehire_fix.py` (manual scripts) still point at the public IP and would need `localhost` if run again.
- [ ] MySQL root password is hardcoded in several PHP/cron files (e.g. `cloud-leave/process_timerecords.php`) and appeared in this session's chat. Lower risk now that 3306 is local-only; rotate when the files using it are moved to a shared config.
- [x] December 2026 added to Apps Script `SCHEDULE_SHEETS` (2026-11-02); 2026-12 rows arriving. Each new month still needs its tab added before it starts.
- [x] First schedule-sync through the portal 2026-11-02 16:49: 7433 rows, 0 errors.
- [ ] Watch the first break-autofix through the portal in `sheet_webhooks.log`.
- [x] 2026-11-03 (`14645e3`): Admin > File Leave for employee (`admin_file_leave`) now saves leave as `pending` (ohrm status 1, SL `sl_hr_stage='pending_hr'`) instead of approved/scheduled; `_notify_leave_filed_for_employee` emails the employee, approver and (SL) HR verifiers. No "file as approved" option (Separated employees aren't in that form's list). Watch the first real one reach an approver.
- [x] 2026-11-02 (`fc56acd`): leave filing checks each date against its own year's balance (`helpers/leave_credits.available_by_year` / `year_shortfalls`, used by `file_leave` and file-for-employee in `app.py`). Unloaded year = current year's full allowance minus already filed for that year. Use Remaining Balance refused across Jan 1. Display-only leftovers: `templates/file_leave.html` labels and the custom-hours max still show the current year's balance. Closes the "balance-checked against the current year" items in the 2026-10-22 entries.
- [x] 2026-11-02: held-mail queues on managers@, sga@, qns_agents@, gygcc5@ are empty; `max_message_size` is 0 (unlimited) on all four (managers@ changed from 40 KB by Claude; not in `mailing_list_audit`, whose `action` enum has no settings type).
- [ ] After 1–2 weeks of clean syncs, ask before deleting the old employee-sync unit file and UMMS-Core webapp.
- [ ] Handover note for Jovin/Jericho published as an artifact (private until the user shares it): https://claude.ai/artifact/KcfeqkNmzPtT8exzBCtmdX
- [x] From 2026-10-29: mailing-list sync cron line added by the user; that session's code committed.

---

## 2026-10-29 — Mailing lists follow the employee sheet; admins via .env
Commit: `239367b` (changes below uncommitted) · Branch: `main`

### What was built / changed
- **`PORTAL_ADMIN_EMAILS`:** a comma-separated list in `.env` that grants full admin at login, on top of `central_db.Employees.role = 'Admin'`. Listed emails are also exempt from the Separated-status login block. Code: `_env_admin_emails()` and `_is_separated_non_admin()`, plus the `session['is_admin']` line in `auth_callback` in `app.py`.
- **Mailing list sheet sync:** `scripts/mailing_list_sheet_sync.py` makes the cPanel/Mailman groups match `gsheet_employees.mailing_list`. Tests are in `tests/test_mailing_list_sheet_sync.py`, docs in the "Sync from the employee sheet" section of `docs/mailing-lists.md`.
- **`modules/mailing_lists.py`:** `_actor()` returns `system:sheet-sync` / "Sheet sync" outside a request, so cron changes are audited.
- **Webhook (outside this repo):** `/var/www/html/UMMS-Core/webapp/app.py`, run by systemd `employee-sync` (gunicorn :5000, behind `webhook.cohere.ph`). Its `batch_sync` now saves `'Mailing List'` into `mailing_list`, and it logs a warning when that field is missing from a batch. Backup: `app.py.bak.20261028225000`; the folder is not under git. `/var/www/html/automation/webhook_server.py` is an old copy that is not running.
- **cPanel:** deleted the `gygbo1@` forwarder (21 targets, recorded in mailing list History). The `gygbo1@` mailbox remains. First live sync run: 17 added, 31 removed, all verified.

### Why / decisions
- The user is leaving and handing the portal to a new full admin. Sub-admins can't manage sub-admins, supervisors or login-as, so the successor needs full admin.
- Sheet data chain: Cohere Employee List → "Employee Master List" (IMPORTRANGE `A:AF`, widened from `A:AD`) → "CSAT and MasterList sheet", which holds the Apps Script `scheduledSync` that sends every header to the webhook. Pushes happen about every 6h (00/06/12/18), or on demand with "Sync Employees Now".
- Sync rules agreed with the user:
  - Add Active employees the sheet assigns to a list.
  - Remove sheet employees who are Separated, or whose filled cell no longer names the list.
  - Leave blank and `#N/A` cells alone.
  - Never touch addresses that aren't in the sheet.
- Aliases and exclusions:
  - `gygL2@` is treated as `l2-agents@`, and `gygbo1@` as `bo-cohere@`.
  - `soms@` is not managed (it isn't a cPanel group).
  - `hr@` is not managed: it forwards HR's mailbox, and the sheet maps all of SGA to it.
- The Finest group (managers) is add-only.
- `gygccall@` is synced as its Mailman list. Its forwarder targets are other groups, not people.
- Removal guard: more than 20 removals, or more than 25% of a list, in one run skips that list. `--allow-big` was used once for the catch-up run.
- The user accepts that manual adds of sheet employees with a filled cell get undone by the next sync.

### Database changes
```sql
-- central_db
ALTER TABLE gsheet_employees ADD COLUMN mailing_list VARCHAR(500) NULL DEFAULT NULL AFTER call_email_mode, ALGORITHM=INSTANT;
```

### Config / environment
- New `.env` key `PORTAL_ADMIN_EMAILS`, currently empty. Changes need a `leavesystem` restart, then the person logs out and back in.
- `.gitignore` now includes `mailing_list_sheet_sync.state`, which holds the last applied push timestamp.
- Services restarted: `leavesystem` and `employee-sync`.

### How to verify
- `venv/bin/python scripts/mailing_list_sheet_sync.py` (dry run): every list should say "in sync".
- Portal → Admin → Mailing Lists → History, filtered by "Sheet sync".

### Open items / next steps
- [ ] **The user must add the cron line**, because Claude was blocked from editing root's crontab: `*/30 * * * * cd /var/www/html/leavesystem && venv/bin/python scripts/mailing_list_sheet_sync.py --live >> mailing_list_sheet_sync.log 2>&1`
- [ ] Name the successor admin, add them to `PORTAL_ADMIN_EMAILS`, then remove `andrewvincentt@gmail.com` from Admin and rotate credentials. The secrets in the `employee-sync` unit file and the Apps Script were exposed in chat.
- [ ] Run Separated cleanup for lists the sheet doesn't cover (gygcs@ has 7).
- [ ] Fix the one `#N/A` Mailing List formula in the sheet. Arctic CS Adelfa Maderazo and Jessamae Salazar are still on bo-cohere@ because their cells are blank.
- [ ] Commit this session's code: `.gitignore`, the `app.py` admin change, `modules/mailing_lists.py`, `docs/mailing-lists.md`, the new script and its tests. The `app.py` diff also contains the pending documents-module work.

---

## 2026-10-27 — Absences page now skips SUS days; absence sheet syncs every 2h
Commit: `fd68361` (app.py change uncommitted) · Branch: `main`

### What was built / changed
- `get_absence_records()` in `app.py` now loads SUS dates from `central_db.absence_records` (code `'SUS'`, within the date range) and skips those scheduled days. Same rule as `admin_attendance_grid()` and `admin_awol_report()`. Fixes `/admin/absences` listing suspended agents as Absent (e.g. Jayne Cuajao 220404-14, Oct 24–25).
- Apps Script (user's Google account, outside repo): "Untitled project" → `syncAbsenceRecords`, bound to the user's sheet that IMPORTRANGEs Jovin's "Absence Data" `AbsenceRecord` tab. `createTrigger()` was rewritten to delete the old 6 AM/6 PM triggers and create one every-2-hours trigger. Added an `onOpen` "Portal Sync → Sync absences now" menu.

### Why / decisions
- SUS is usually entered late (schedule balancing). The page is computed live, so a fresh sync is enough. Every 2h instead of hourly keeps runtime (~63 s/run, ~17k rows) inside the consumer trigger quota.
- `absence_notify.py` already skipped SUS itself, so its emails are unchanged. Its `skipped_sus` count will now read 0.

### Deploy / run notes
- Backup `app.py.bak.20261027*`, ast gate passed, `leavesystem` restarted and active.

### How to verify
- `/admin/absences?date_from=2026-10-24&date_to=2026-10-25`: Cuajao not listed.
- script.google.com/home/executions: `syncAbsenceRecords` runs about every 2h.

### Open items / next steps
- [ ] Commit the `app.py` SUS change (left uncommitted; unrelated documents-module edits are also pending).
- [ ] The sync never deletes rows and keys on `sheet_row`, so deleted or shifted sheet rows can leave stale SUS in `absence_records`.
- [ ] Late SUS can't retract a 1:05 PM absence email that was already sent.

---

## 2026-10-27 — Absence alerts: false "RD" absences fixed; absence_notify.py now live
Commit: `f7378ec` · Branch: `main`

### What was built / changed
- Diagnosed false REPEATED ABSENCE email for Aileen Perez (230411-29), Oct 23–25. The sender was the sheet-based `/var/www/html/automation/send_absence_report.php` (outside this repo), not the portal. The sheet's Scheduled IN was `RD` and its `Possible Absent` tab only treats the exact text "Rest Day" as a day off, so it read RD as a shift with no punches. The portal (`employee_schedules.is_rest_day=1`) and the `absence_notify.py` dry run were correct.
- `send_absence_report.php`: added `isRestDaySchedule()` (RD / Rest Day / Rest / Off / Day off, case-insensitive). Skips the alert when the reported day is a rest day (logged as "Rest-day suppression") and drops rest-day rows from the 7-day window list. Backup: `send_absence_report.php.bak_20261027_051922`.
- Switched to the portal-based notifier: the root crontab now runs `absence_notify.py --live` at 13:05, and the PHP 13:00 line is commented out (user installed it; the auto-mode classifier blocks `crontab` edits from Claude).

### Why / decisions
- User chose "guard the PHP script, then go live": the guard is a safety net in case the PHP job is re-enabled. The portal notifier also covers the ~60 scheduled employees the sheet misses.
- Dry run Oct 7–26 had no FATAL/errors; ~86 would-be emails over 20 days.

### Config / environment
- Root crontab: PHP absence line disabled, `absence_notify.py` gets `--live`. Pre-change crontab saved to that session's scratchpad (`crontab.bak`).

### How to verify
- `grep "$(date +%F) 13:05" absence_notify.log`: expect "Sent '...'" lines and a summary without "[DRY RUN]".
- `crontab -l | grep -i absen`

### Open items / next steps
- [ ] After 13:05 on 2026-10-27 (first live run, for 2026-10-26), check absence_notify.log for "Sent" / "Email error" / "FATAL".
- [ ] `tl_view_map` is missing some TLs (e.g. Khristofer Mozo, Magdaleno II Arong, Rachel Marikit, Lee Anne Borgonia logged "no TL email"); those alerts go to the SOM + OM only.
- [ ] Rollback: uncomment the PHP line and drop `--live`.

---

Older entries: [docs/handoff-archive.md](docs/handoff-archive.md)
