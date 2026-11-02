# Handoff Log — leavesystem

## Project overview
- **App:** Cohere HR portal ("Leave4Day" / hrportal), Flask monolith [app.py](app.py) plus blueprints in `modules/`, `blueprints/`, `break_log/`, `coaching.py`, `memos.py`. Frontend: Jinja + Bootstrap 5 + vanilla JS, Lucide icons.
- **DBs:** `orangehrm2` via `get_db()` (OrangeHRM tables, leave balances) and `central_db` via `get_db_connection()` / `get_central_db()` (portal data, `gsheet_employees`). Raw PyMySQL with DictCursor.
- **Deploy:** this checkout *is* production (`hrportal.cohere.ph`). systemd `leavesystem` runs `venv/bin/python app.py` on 127.0.0.1:5001 behind Apache. Restart with `sudo systemctl restart leavesystem`. `webhook.cohere.ph` (Apps Script sheet sync, biometric queue) also proxies to it (`modules/sheet_webhooks.py`); the old employee-sync service is retired.
- **Cron:** tardiness/overbreak/undertime notify scripts and others under root's crontab (see CLAUDE.md).

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

## 2026-10-22 (3) — Timestamp Records page (raw punches from Google Drive)
Commit: `c5141e3` · Branch: `main`

### What was built / changed
- Read-only page `/admin/timestamp-records` + JSON `/admin/timestamp-records/data?from=YYYY-MM-DD&to=YYYY-MM-DD[&refresh=1]` — `modules/timestamp_records.py`, `templates/timestamp_records/index.html`.
- Drive reader `helpers/drive_timestamps.py`: Cohere Raw Logs / YYYY / YYYYMM / YYYYMMDD-.csv; paginated listing; range loading across month/year folders; duplicate file names → newest upload only (yellow notice); bad rows/files reported, never dropped silently; memory + disk cache (`instance/drive_timestamps/`, gitignored) keyed on file id + modifiedTime/md5/size.
- UI: payroll-period dropdown (portal cut-offs 8–22 / 23–7, defaults to last completed) + custom From/To (max 62 days), search, IN/OUT filter, pagination, Copy table (TSV of all matching rows), loading/empty/error states.
- New sub-admin permission `can_timestamp_records` (Analytics & Reports): `permission.py`, `templates/admin/sub_admins.html`, app.py session dict + both sub-admin saves + SELECT; nav links in `nav/_attendance_tabs.html`, `_sidebar.html`, `_topnav.html`.
- Tests: `tests/test_timestamp_records.py` (51, incl. one live Drive read).

### Why / decisions
- CSV header is `companyid,Day,Time,Type` — no names; First/Last Name come from OrangeHRM `hs_hr_employee` by employee_id (active row wins; unmatched → blank + notice).
- Each daily file = one shift date; overnight clock-outs carry the next Day. Ranges select by **file (shift) date**, so the last shift's overnight out is included.
- Auth switched from API key to a service account: Google throttled API-key downloads of link-shared files (403 "Sorry…" HTML page) after ~150 test downloads. Auth mode is chosen by config only, never auto-fallback.
- Year folders reached via hardcoded `YEAR_FOLDERS` (2026, 2025) because the service account is shared on the 2026 folder, not the root. Folder access is checked first (files.list on an unshared folder returns empty, not an error).
- Dedicated permission (user's choice) instead of reusing can_absences/can_tardiness/can_reports.

### Database changes
```sql
-- orangehrm2 (36 rows, all 0 after)
ALTER TABLE leave4day_sub_admins ADD COLUMN can_timestamp_records TINYINT(1) NOT NULL DEFAULT 0;
```

### Config / environment
- `.env`: `GOOGLE_DRIVE_SA_KEY_FILE` (path to service-account JSON in `/etc/leavesystem/`, root-only 600), `GOOGLE_DRIVE_API_KEY` (fallback only, unused while SA is set).
- Service account `timestamp-drive-reader@wpforms-smtp-501221.iam.gserviceaccount.com`, scope drive.readonly; Drive API enabled in that project.
- app.py backup: `app.py.bak.20261022231254`. Service restarted by the user at 23:28.

### How to verify
- `https://hrportal.cohere.ph/admin/timestamp-records` (admin, or sub-admin with the new checkbox) → Sep 23 – Oct 7 2026 = 15 files, 3,913 punches.
- `venv/bin/python -m unittest tests.test_timestamp_records -v`

### Open items / next steps
- [x] Service-account key rotated 2026-10-22: new key `898730ea…` in `/etc/leavesystem/`, `.env` points at it, service restarted 23:53; old key `fd194495…` deleted in Google (sign-in now rejected) and its file removed. Old `GOOGLE_DRIVE_API_KEY` deleted in Google and removed from `.env`.
- [x] 2025 dropped (owner has left, can't be shared): page covers 2026-01-01 onward (`FIRST_DATE`), 2025 removed from `YEAR_FOLDERS`.
- [ ] Continuity risk: root "Cohere Raw Logs" is owned by the departed gmail.com account and is reached only via anyone-with-link; 2026 folder is owned by a getyourguide.com account. Before 2027: create a company-owned home (ideally a Shared Drive), share it with the service account, add the 2027 folder id to `YEAR_FOLDERS` in `helpers/drive_timestamps.py`.
- [ ] Tick "Timestamp Records" on the Sub-Admins page for whoever needs it.
- [ ] Confirm payroll period start (portal says 23rd; user mentioned "Sept 22").
- [ ] Pre-existing failing tests (fail on HEAD too): test_awol_report / test_undertime_report empty-state + leave-covered cases.

---

## 2026-10-22 (2) — 2027 balances loaded; leave cycle automated per policy
Commit: `ae19f18` · Branch: `main` (commits `f92d855`..`ae19f18`)

### What was built / changed
- `helpers/leave_policy.py`: policy 3.1–3.3 rules (VL from 1st anniversary of `operational_date`, SL from operational date + 6 months, pro-rated 1/month with a 15th-of-month cutoff, Carry-Over = min(unused VL, 6)).
- `scripts/year_end_rollover.py <year>`: VL/SL/LWOP + Carry-Over for a year; `--skip-co`, `--only-co`, `--reg-file`, `--csv`; dry run by default; skips existing rows.
- `scripts/prorated_leave_grants.py`: daily pro-rated VL/SL grants; never changes existing rows; lists overdue (pre-2026-10-22) grants, rows that differ from policy, next 31 days.
- Ran 2027 rollover with `--skip-co --live`: 544 rows (VL/SL/LWOP); then charged the pending 2027 VL.
- Dashboard: LWOP card removed. VL/COL notice is 7 **calendar** days (HR's choice; working-day version reverted).
- Agents: `year-end-rollover.md` follows the policy; leave-auditor / employee-sync-auditor added earlier.
- `ae19f18`: a move to Numa restarts the VL anniversary (`gsheet_employees.moved_to_numa`, `vl_deployment()`); Arctic moves don't; SL unchanged. Column is empty — only two Numa staff were moved (HR counts from 2025-07-08), everyone else was hired directly.
- First HR email cross-check: the two Numa staff's 6 VL was already credited (2026-07-28); nothing to do.

### Config / environment (root crontab)
- `15 1 * * *` prorated_leave_grants.py --live >> prorated_leave_grants.log
- `30 1 * * *` apply_pending_leave_credits.py --live (existing)
- `30 0 1 1 *` year_end_rollover.py $(date +%Y) --live >> year_end_rollover.log (full rollover every Jan 1; on Jan 1, 2027 only Carry-Over is left: 136 rows in the dry run)

### Why / decisions
- Entitlement corrections wait for HR's email; automated grants are cross-checked against those emails.
- `promotion_date` in the sheet is promotions, not regularization.

### How to verify
- `venv/bin/python scripts/prorated_leave_grants.py` (dry run) · `/admin/entitlements?year=2027`
- `venv/bin/python -m unittest tests.test_leave_policy tests.test_advance_notice tests.test_work_mode_lwop tests.test_leave_credits`

### Open items / next steps
- [ ] Check the first nightly logs: `prorated_leave_grants.log`, `apply_pending_leave_credits.log`.
- [ ] HR cross-check list (names in chat/logs, not here): 3 SL grants overdue before automation, 4 allowances below policy, first automatic SL grants on Nov 1 and Nov 4.
- [x] ~~New next-year filings are still balance-checked against the current year's balance.~~ Fixed 2026-11-02 (`fc56acd`).
- [ ] 2025 balances not reviewed; 2025 report still in git history (`e072dfe`).
- [ ] Dashboard redesign on hold (`lovable_preview/`).

---

## 2026-10-22 — Leave balances: one table, 2026 reconciled, work-mode/LWOP rules, new agents
Commit: `6c3af9b` · Branch: `main` (commits `bea2379`..`6c3af9b`)

### What was built / changed
- **Dashboard:** "Coming up" birthdays card in the side column (`_dashboard_extras`, `templates/dashboard.html`); recent-request dates show the year (`_fmt_range(year=True)`); LWOP balance card removed, LWOP-only staff see "No paid leave balance yet. You can file LWOP."
- **Incident Reports / Memos:** Group filter (whole account e.g. "All Numa", or single group) — `modules/incident_reports.py`, `memos.py` (memos match by employee_id, else email).
- **Leave History** (`/history`): one line per row, notes expand on click; paging 25/50/100 with filters kept; employee search also matches full name / employee ID.
- **One entitlement table:** Admin › Entitlements reads/writes `ohrm_leave_entitlement` directly (current-total hint, confirm before replace, Edit buttons, delete only if unused, change log). `leave4day_entitlements` ("Portal DB") retired — its type IDs (`leave4day_leave_types`) didn't match `ohrm_leave_type`. Entitlement Source toggle and File Leave "[Portal]" options removed.
- **Future-year leave:** filing into a year with no balance marks days `credit_pending`; `helpers/leave_credits.py` + `scripts/apply_pending_leave_credits.py` charge them once the row exists (also on saving a balance). Reject/cancel skip refunds for never-charged days.
- **Work mode** (`get_work_mode()`): individual setting > Numa/SGA/TL are 5-day (`is_five_day_group`) > global default. Magic CWS uses the same rule.
- **LWOP unlimited:** always offered on File Leave / File for Employee ("no limit"), no balance check (`with_unlimited_lwop`).
- **VL/COL notice:** 7 calendar days; error names the earliest date (`advance_notice_min_date`).
- **Agents:** `.claude/agents/leave-auditor.md`, `year-end-rollover.md`, `employee-sync-auditor.md`.
- `lovable_preview/` saved (on-hold top-nav redesign, mock data, README).

### Why / decisions
- Leave day = hours ÷ 8; balances in 8-hour days. Some agents moved to 4×10 h around 2026-03-23; Numa, SGA, TL stay 5-day; parental leave reverts to 5-day. "Use remaining balance" caps at 0 (an 8 h day on a 10 h shift can be correct).
- Policy 3.1–3.3 (VL from 1st deployment anniversary, SL from regularization, pro-rated with a 15th-of-month cutoff; Carry-Over = unused VL up to 6). HR chose **calendar** days for the 7-day notice and leaves the ">3 consecutive days" rule to approvers.
- Repo is public: no employee names/IDs in committed files. 2025 reconciliation report and two name-listing fix scripts are gitignored (report is still in git history from `e072dfe`).

### Database changes
```sql
-- orangehrm2 (run by the user)
ALTER TABLE leave4day_requests
  ADD COLUMN credit_pending TINYINT(1) NOT NULL DEFAULT 0,
  ADD INDEX idx_credit_pending (credit_pending);
CREATE TABLE leave4day_entitlement_log (id, entitlement_id, emp_number, leave_type_id, year,
  action, old_days, new_days, changed_by, changed_at) -- see app.py _log_entitlement_change
```
- Data (2026 only): migrated portal entitlement rows into OrangeHRM (`scripts/manual_fixes/retire_portal_entitlements.py`); reconciled `days_used` to filed leave for all 2026 balances; 1.25→1.0 day fixes for 5-day staff; test-account leave rejected; future-year leave flagged `credit_pending`. Every change is in `leave_balance_sync.log` (gitignored). Only one 2026 balance differs from filings, kept on purpose by the user.

### Config / environment
- `.env`: `IR_SGA`, `IR_OVERHEAD` (SGA added) edited by the user — IR filing access for SGA.
- root cron: `30 1 * * * … scripts/apply_pending_leave_credits.py --live >> apply_pending_leave_credits.log`.
- Work mode: all active Numa/SGA/TL set to 5day in `leave4day_emp_settings`.

### How to verify
- `/admin/entitlements` (one table, hint, Recent changes) · `/admin/emp-settings` · `/file-leave` (LWOP "no limit") · `/history` (paging) · `/incident-reports/` and `/memos` (Group filter) · `/dashboard`.
- `venv/bin/python -m unittest tests.test_leave_credits tests.test_work_mode_lwop tests.test_advance_notice -v`

### Open items / next steps
- [ ] December: load 2027 balances (208 active employees have none) — use the `year-end-rollover` agent; ask HR for regularization dates (no column exists; 2026 data fits deployment + ~6 months). 11.5 days of 2027 leave are waiting in `credit_pending`.
- [x] ~~New 2027 filings are still balance-checked against the current year's balance.~~ Fixed 2026-11-02 (`fc56acd`).
- [ ] 2025 balances not reviewed/corrected (user: leave 2025 for now). Optional: purge the 2025 report from git history.
- [ ] Dashboard redesign (`lovable_preview/`) on hold.

---

## 2026-10-20 (9) — SL Verification: per-date validity, HR remarks, "No medical certificate"
Commit: this one · Branch: `main`

### What was built / changed
- **Per-date Valid/Invalid** on `/leave/sl-verification` (`templates/supervisor/sl_verification.html`). Each absence date has its own Valid/Invalid toggle and a reason (`SL_INVALID_REASONS` in `app.py`: Late notice / Outside consult/fit-to-work range / Med cert not verified / No medical certificate / Other (see remarks)). The "Valid absence" readout now reads e.g. "Partial — 2 of 3 dates valid".
- **Medical certificate submitted? Yes/No** at the top of each card. Selecting No hides the cert fields and marks every date invalid with "No medical certificate".
- **Remarks to approver** textarea. Required when any date uses "Other".
- `sl_verification_submit()` stores a verdict per row (`sl_valid_absence`, `sl_invalid_reason`), plus `sl_no_medcert` and `sl_hr_remarks` on the whole request. HR can only invalidate a date the auto rules pass, never validate one they fail. Unknown reasons are coerced to "Other (see remarks)".
- `send_sl_verification_email()` has a per-date "Valid Absence (per date)" row and an "HR Remarks" row (escaped). For no-medcert it sends a short table plus an "absence is invalid" note instead of the void note.
- `/supervisor/pending` (`templates/supervisor/pending.html`, query in `app.py`): the HR Verification box shows per-date badges, the no-medcert banner and the remarks. "Manage Individual Dates" has an "HR verdict" column, so the SOM can cancel the invalid date and approve the rest.
- No-medcert filings show only **Reject** to the SOM: Approve is hidden and there's no hard-copy checkbox.
- Backups: `app.py.bak.20261020*`, `templates/supervisor/{pending,sl_verification}.html.bak.20261020*`.

### Why / decisions
- The SOM could already cancel single dates, but HR's single Yes/No for the whole request hid which date was the problem (e.g. Oct 9 late notice, Oct 10–11 fine).
- Late notice isn't in the auto rules, which is why HR has a manual Invalid override on top of them.

### Database changes
```sql
-- orangehrm2
ALTER TABLE leave4day_requests
  ADD COLUMN sl_invalid_reason VARCHAR(60) NULL AFTER sl_valid_absence,
  ADD COLUMN sl_no_medcert TINYINT(1) NOT NULL DEFAULT 0 AFTER sl_invalid_reason,
  ADD COLUMN sl_hr_remarks TEXT NULL AFTER sl_hr_verified_at;
```

### Deploy / run notes
- `sudo systemctl restart leavesystem`, clean start.
- Tested with a fake cursor (scratch script, not committed): late-notice override, the 1-day consult allowance, no-medcert, "Other" without remarks (400) and a bogus reason. Both templates were render-tested.

### How to verify
- Verify a multi-day SL on `/leave/sl-verification` with one date set Invalid. Then check the SOM card on `/supervisor/pending` and the VERIFICATION email.

### Open items / next steps
- [ ] Watch the first real multi-day and no-medcert verifications go through.
- [ ] Filings HR-verified before this change have no per-date data, so the approver card falls back to the single Yes/No.

---

## 2026-10-20 (8) — Fixes: Accept + Remember did nothing; last-target forwarder removal reported as error
Commits: `0fc0638`, this one · Branch: `main`

### What was fixed
- **Accept + "Remember these senders"**: Mailman 2 silently drops approvals when the same admindb POST also has `senderfilterp-`/`senderfilter-` fields. On managers@, 37 accepts all stayed queued. `moderate()` now posts decisions only, then adds senders through `update_sender_filter()` (verified, skips duplicates). Discard + remember works the same way.
  - Confirmed live by accepting 1 message alone (IR-20261019-B0088E, id 1651): done. The other 36 are still queued for the user to retry.
- **Forwarder last target**: removing a forwarder's only target makes cPanel delete the address. `change_members()` re-read it via `_current_emails` and raised KeyError, so the cleanup showed it as failed and it wasn't audited. It now re-reads `fetch_forwarders` and marks the result done, with a "cPanel deleted the forwarder" note.
  - This happened in the Separated cleanup to `gygl2@`, `jondel.lerio@`, `juniel.cabantingan@` and `montesa.montebon@`, which are now deleted. Audit rows were backfilled; the removed target address wasn't recorded.
  - **`gygl2@` was a group address**: mail to it now bounces. Recreate it if it's still used.

---

## 2026-10-20 (7) — Mailing Lists 8–12: private by default, privacy switch, owners/moderators, all-lists history, view-only permission
Commit: this one · Branch: `main`

### What was built / changed
- `modules/mailing_lists.py`:
  - `PRIVACY_PRESETS`, `fetch_privacy`, `set_privacy` and `POST /mailman/<addr>/privacy`. New lists default to Private and get the preset after `add_list`.
  - `fetch_list_admins`, `update_list_admins` and `POST /mailman/<addr>/admins` (the last owner is protected).
  - `/history` and `/history.csv` with `_history_filters` (parameterised).
  - `ml_can_view()`; `_access_required` now lets GET through with view or full access, while POST needs full access (403).
- Templates: privacy and owners cards on `detail.html`, the new `history.html`, and change controls hidden for view-only users on every page.
- New sub-admin permission `can_mailing_lists_view` ("Mailing Lists (view only)", Tools & Access). Both sub-admin INSERTs are now 42 cols / 42 values. app.py backup: `app.py.bak.20261020034306`.

### Database changes
```sql
-- orangehrm2
ALTER TABLE leave4day_sub_admins ADD COLUMN can_mailing_lists_view TINYINT(1) NOT NULL DEFAULT 0;
-- central_db
ALTER TABLE mailing_list_audit MODIFY action ENUM('add','remove','create','moderate','delivery','senders','delete','privacy','owners') NOT NULL;
```

### Live use by the user today (from the audit log, no failures)
- Separated cleanup: 72 forwarder removals on 14 groups (first live proof of `delete_forwarder`) and 160 Mailman removals on 11 lists.
- 43 moderation decisions. The cleanup page now shows 0 separated people.

### Open items
- [ ] Per-list access (e.g. a team lead manages only their list), possibly via Mailman owners. Not built; ask the user.
- [ ] Still untested live: forwarder add/create, delete group, delivery toggle, privacy switch, owners edit.

---

---

Older entries: [docs/handoff-archive.md](docs/handoff-archive.md)
