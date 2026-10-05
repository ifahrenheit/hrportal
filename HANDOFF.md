# Handoff Log — leavesystem

## Project overview
- **App:** Cohere HR portal ("Leave4Day" / hrportal), Flask monolith [app.py](app.py) plus blueprints in `modules/`, `blueprints/`, `break_log/`, `coaching.py`, `memos.py`. Frontend: Jinja + Bootstrap 5 + vanilla JS, Lucide icons.
- **DBs:** `orangehrm2` via `get_db()` (OrangeHRM tables, leave balances) and `central_db` via `get_db_connection()` / `get_central_db()` (portal data, `gsheet_employees`). Raw PyMySQL with DictCursor.
- **Deploy:** this checkout *is* production (`hrportal.cohere.ph`). systemd `leavesystem` runs `venv/bin/python app.py` on 127.0.0.1:5001 behind Apache. Restart with `sudo systemctl restart leavesystem`.
- **Cron:** tardiness/overbreak/undertime notify scripts and others under root's crontab (see CLAUDE.md).

---

## 2026-10-05 (3) — /admin/file-leave redesigned to match File Request for Employee
Commit: `4c96c89` (changes below committed later in `c99caee` / `cc608d8` / `1085e16`) · Branch: `main`

### What was built / changed
- `templates/admin/file_leave_admin.html` rewritten. It now has:
  - a searchable employee picker (GET `?employee_id=`) and an employee card with a PIM link;
  - clickable balance cards that pick the leave type;
  - half-day mode that locks End Date to Start Date;
  - a range summary line and a confirm dialog;
  - a Recent leaves table (last 120 days and upcoming);
  - a notice that leaves filed here are **auto-approved** (taken/scheduled) and deducted immediately.
- `app.py` `admin_file_leave()`: display-only changes. A nested `_render()` replaces the 6 `render_template` calls; it passes `selected_emp`, `emp`, `balance_data` (from `_pim_leave_balance_data`), `recent` and `form`. GET with `?employee_id=` now loads balances. **The filing logic is untouched.**
- Fixed a bug: validation errors used to re-render with no employee selected and the form cleared. Both the selection and the form values are now kept.
- The employee list still excludes Separated employees (unchanged).
- **Follow-up (same day): Custom Hours + Use Remaining Balance + shift_half** added to `admin_file_leave()`, matching the regular `file_leave()` rules.
  - Custom Hours: single day, whole hours, not AWOL, at most the remaining hours. It overrides that day's deduction.
  - Use Remaining Balance: only offered when 0 < remaining < 10h. It scales the deduction to exactly the remaining hours.
  - Inserts now also write `leave4day_requests.shift_half` ('1st Half' / '2nd Half'), which admin filing used to drop.
  - Tested against the real DB with commits swapped for rollbacks: 3h custom, 1st half, and remaining 8h on 210528-05 all inserted correctly and were rolled back. Row counts and days_used were unchanged. Backup: `app.py.bak.20261005220954`. Restarted at 22:12.

### Config / environment
- Backup: `app.py.bak.20261005220538`. Original template copied to the session scratchpad.
- `leavesystem` restarted 2026-10-05 22:06 with a clean start.

### How to verify
- Open `https://hrportal.cohere.ph/admin/file-leave?employee_id=220525-01`. Validation errors (start > end, multi-day half-day) should keep the form filled in. These paths were tested with no rows written.

---

## 2026-10-05 (2) — File Request for Employee page (FTS / CWS / OT-RDW, incl. Separated)
Commit: `2621cd4` (changes below committed later in `c99caee` / `cc608d8` / `1085e16`) · Branch: `main`

### What was built / changed
- New page `/admin/file-request` (endpoint `file_for_emp.index`). Blueprint in `modules/file_for_employee.py`, template `templates/admin/file_request_for_employee.html`.
  - Pick any employee, including Separated ones, then file FTS, CWS or OT/RDW on their behalf. Recent requests (last 120 days) show underneath.
  - Admin-only "File as approved" checkbox. It writes `status='Approved'`, `approver_name` and `approved_at`, the same three columns `/api/approvals/action` sets.
  - Blocks request dates after the exit date, and the date pickers are capped at it.
  - Warns when there's no approver on file, which is typical for Separated employees.
- Nav: "File Request for Employee" added next to "File for Employee" in `templates/nav/_topnav.html` (menu + People subnav) and `templates/nav/_sidebar.html`.
- `app.py` registers the blueprint right after `survey_bp`.
- Tests: `tests/test_file_for_employee.py`, 19 tests. POSTs are fully mocked; the GET render tests are read-only against the real DB.
- One-off before the page existed: filed FTS OUT #839 for Lornelyn Sancover (260618-12), 2026-09-29 21:00, Pending.

### Why / decisions
- Permission is `can_file_for_emp`, the same flag as the leave page. "File as approved" is limited to `is_admin`, at the user's request.
- The validation and duplicate checks copy `file_fts` / `file_cws` / `file_ot` in app.py, without touching those live routes. **If self-service filing rules change, update `modules/file_for_employee.py` too.** OT also checks ot_type, rate, category and time against the form's option lists.
- The blueprint doesn't `from app import`, because app.py runs as `__main__` and that would re-execute the whole file. Instead `init_file_for_emp(...)` hands it `get_central_db`, `get_db`, `send_email`, `get_supervisor_email`, `_fetch_pim_requests` and `TICKET_REQUIRED_TYPES`. That's why it's registered down by `survey_bp`.
- Audit trail: the request tables have no "filed by" column, so each filing writes a `central_db.employee_audit_log` row (`field_name='filed_request'`, `change_source='file_for_employee'`) plus an app log line.
- Email:
  - Active employee: gets a "filed on your behalf" notice.
  - Pending request: also goes to the approver via `get_supervisor_email()`.
  - Separated employee: gets no email.

### Config / environment
- No new .env keys and no schema changes. All INSERTs were checked with `EXPLAIN` against the live tables. Backup: `app.py.bak.20261005215620`.
- `leavesystem` restarted 2026-10-05 21:58 with a clean start.

### How to verify
- `./venv/bin/python -m unittest tests.test_file_for_employee -v` (with .env loaded).
- Open `https://hrportal.cohere.ph/admin/file-request?employee_id=260618-12`; #839 shows in Recent requests.

### Open items / next steps
- [ ] Someone with All Requests access needs to approve FTS #839. Lornelyn has no approver.
- [ ] Possibly show `filed_request` audit rows somewhere in the UI (only in the DB for now).

---

## 2026-10-05 — PIM profile: Leave Balance + Attendance Grid tabs
Commit: `b8dbb0e` (changes below committed later in `c99caee` / `cc608d8` / `1085e16`) · Branch: `main`

### What was built / changed
- **Leave Balance tab** on `/pim/<employee_id>`. New helper `_pim_leave_balance_data()` in `app.py`.
  - One card per leave type showing entitled / used / remaining in days and hours, with a progress bar. A card turns red when 1 day (8h) or less remains.
  - Used is split into approved, scheduled and pending.
  - A Portal Entitlements table appears only when `leave4day_entitlements` has rows for the employee.
  - Always shows the current year. It ignores the viewed month and year.
- **Work Schedule card → "Work Schedule & Attendance"**, using new helper `_pim_attendance_grid()` in `app.py`. It started as a separate Attendance Grid tab, but the user found that redundant with the Work Schedule calendar, so the grid was merged into the calendar.
  - Evaluated days show P/A/FI/FO/SUS/RD/leave codes and pending `*`, with the admin grid's colours. Days not yet evaluated keep the old schedule look (blue with the shift, or "Rest Day").
  - Holidays and attendance notes show on the day. Hovering a day shows shift, punches, leave and note.
  - The header shows the attendance rate, a best-case forecast and per-code counts.
  - With no userdata personid, the card shows the schedule only.
- `templates/pim/profile.html`: a Leave Balance tab button and pane before Memos, the merged calendar card, and a small script that opens the tab named in the URL hash (e.g. `#tab-balance`).

### Why / decisions
- The balances reuse `get_leave_balances()`, which is the same source as the employee's `/dashboard`. The used-hours split uses its exact filter, so approved + scheduled + pending always equals Used.
- The grid copies `admin_attendance_grid()`'s day rules for a single employee instead of refactoring that live route. Verified against `/admin/attendance-grid` for 220525-01: Sep 14/15 = 93.3% and Aug 18/18 = 100% on both pages. **If the admin grid rules change, update `_pim_attendance_grid()` too.**
- One deliberate difference: the 1 PM safe-cutoff (`perf_safe_last_day`) applies to every month. The admin grid applies it only to the current month, so future months there show A's.
- Access: no new permission. Anyone with `is_admin` or `can_pim` (the existing PIM gate) sees both tabs, even without `can_absences`.

### Config / environment
- No new .env keys and no DB changes. Backup: `app.py.bak.20261005212425`.
- `leavesystem` restarted 2026-10-05 at 21:27 and again at 21:32 (after the merge). Both starts were clean.

### How to verify
- Open `https://hrportal.cohere.ph/pim/220525-01#tab-balance`, then step through months on the Work Schedule & Attendance card.

### Open items / next steps
- [ ] Get user feedback on the layout and on whether the PIM grid should honour `can_absences`.
- [ ] 220525-01 shows LWOP at 196.38 days left. That is real `ohrm_leave_entitlement` data, and `/dashboard` shows the same, but it is worth checking with HR.

---

## 2026-10-02 — Stale IR reminder email moved from old PHP dashboard to portal script
Commit: `8ef4bb4` · Branch: `main`

### What was built / changed
- The stale-incident reminder ("Incident Report Reminder: N Stale Incident(s) Require Attention") now runs from `scripts/cron_incident_reminder.py`. Before this, it ran from `/var/www/html/cohere_dashboard/incident_report/cron_reminder.php`, whose links pointed to `dashboard.cohere.ph`. The new links go to `hrportal.cohere.ph/incident-reports/<report_number>` and `/incident-reports/`.
- Committed the script's pending edits: a `--send` flag (dry run by default), a `COLLATE utf8mb4_unicode_ci` on the `supervisor_mapping` ⋈ `gsheet_employees` join, and the `import os` moved below the shebang.

### Why / decisions
- Both scripts use the same `central_db.incident_reports` query (72h idle, 24h reminder throttle) and the same `last_reminder_sent` column, so switching over doesn't double-send.
- Only the script was committed. The other modified files are separate unreviewed work (see the 2026-10-01 entry and below).

### Config / environment
- Root crontab line 56 changed to `0 9,17 * * * .../venv/bin/python .../scripts/cron_incident_reminder.py --send >> /var/log/incident_reminders.log 2>&1`. The previous crontab was backed up to the session scratchpad (`crontab.bak.20261002003100`).
- Renamed `/var/www/html/cohere_dashboard/incident_report/cron_reminder.php` to `cron_reminder.php.disabled`. It was publicly reachable with no auth, and opening it would send the old email. The old URL now returns 404. To undo, rename it back.
- Restarted `leavesystem` on 2026-10-02 00:32. It started cleanly with no code changes, because every modified .py file was older than the previous start.

### How to verify
- After 17:00 on 2026-10-02, check `/var/log/incident_reminders.log`. Python lines have `,mmm` milliseconds; the old PHP lines don't.
- In the next reminder email, the IR number links should open the portal.
- Dry run: `./venv/bin/python scripts/cron_incident_reminder.py` (no `--send`).

### Open items / next steps
- [ ] Confirm that the first live reminder email from the Python script arrives with portal links.
- [x] Committed 2026-10-05: IR workflow `1085e16`, tardiness + requirements `cc608d8`, PIM/app.py work `c99caee`.

---

## 2026-10-01 — Uncommitted live work: PIM approve/reject, tardiness period buttons, requirements
Commit: `b20e821` (changes below committed later in `c99caee` / `cc608d8` / `1085e16`) · Branch: `main`

### What was built / changed
- **PIM profile: Leaves + Requests tabs with approver actions.** `app.py` (+273 lines), `templates/pim/profile.html` (+509 lines)
  - New helpers `_fetch_pim_requests`, `_request_scope_data`, `_leave_scope_data`, `_is_pim_approver_of`.
  - New route `POST /pim/<employee_id>/request-action` (`pim_request_action`) approves or rejects one OT / RDW / CWS / FTS / Magic CWS request.
  - Tabs show pending badges, status/type filter pills and summary cards. A confirm modal is `#pimActionModal`.
  - Leaves go through the existing `/api/leave/action`.
- **Tardiness page buttons.** `templates/tardiness.html`
  - "Current period" now shows the period in progress from its start to today. It used to show the last completed period.
  - "Today" now shows the actual current date. The old 1 PM cutoff logic that jumped back 1–2 days is gone.
- **requirements.txt** now lists deps that were installed but missing: bcrypt, reportlab, openpyxl, python-keycloak, mysql-connector-python, and chromadb, python-docx and pypdf for `rag/rag_api.py`.

### Why / decisions
- `pim_request_action` is deliberately narrower than `/api/approvals/action`. The caller must be this employee's approver, the request must belong to the employee, and the `UPDATE ... AND status='Pending'` guard means a double click or a second approver gets a 409.
- Magic CWS approval creates the balance row if it's missing (`_init_magic_cws_balance`) and then adds `deduction_hours` to `magic_cws_balance.used_hrs`.
- Tardiness "Today" can show incomplete counts for the day in progress. Only people who have already punched in late appear.

### Config / environment
- No new .env keys, no DB schema changes.
- Backup: `app.py.bak.20260928231815`.

### Deploy / run notes
- `app.py` was last edited 2026-09-30 21:53 and the service was restarted 2026-09-30 22:04. **The changes are live but not committed.**

### How to verify
- Open `/pim/<employee_id>` as that employee's approver. Check the Leaves and Requests tabs, then approve a pending request and confirm the badge count drops.
- Open `/tardiness` and click Current period and Today. The date range should end today.

### Open items / next steps
- [ ] Review and commit the four modified files (`app.py`, `requirements.txt`, `templates/pim/profile.html`, `templates/tardiness.html`).
- [ ] Check that non-approvers get a 403 on `/pim/<id>/request-action` and that Magic CWS `used_hrs` goes up by exactly one deduction per approval.
- [ ] From memory: recheck inventory ledger reconciliation at the end of Sept 2026 (due now), and confirm the material release sub-unit (Bond Paper) stock-deduction fix with real-world use.
